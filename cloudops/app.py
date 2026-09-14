"""应用装配与运行（把各层拼成可运行的服务）。"""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Any, Dict, List, Optional

from . import __version__
from .bot import (
    ConfirmationStore,
    Dispatcher,
    JobRunner,
    TelegramClient,
    TelegramError,
)
from .cloud import available_provider_names, create_provider
from .commands import register_all
from .config import Settings
from .credentials import CredentialStore
from .crypto import SecretBox
from .db import Database
from .i18n import get_translator
from .logging_setup import setup_logging

logger = logging.getLogger(__name__)

#: 启动时丢弃的历史更新数量上限（避免重启后执行过期指令）
DRAIN_LIMIT = 100


class Application:
    """把配置、数据库、凭证仓库、指令路由与 Bot 客户端组织起来。"""

    def __init__(self, settings: Settings, *, bot: Optional[TelegramClient] = None) -> None:
        self.settings = settings
        #: 允许注入自定义客户端（测试用记录桩 / 未来接入其它 IM 平台）
        self.bot = bot or TelegramClient(settings)
        self.db = Database(settings.database_path)
        self.box = SecretBox(settings.secret_key)
        self.store = CredentialStore(self.db, self.box, settings)
        self.confirms = ConfirmationStore(settings.confirm_ttl_seconds)
        self.jobs = JobRunner(name="cloudops")
        self.dispatcher = Dispatcher(
            settings=settings,
            db=self.db,
            store=self.store,
            bot=self.bot,
            confirms=self.confirms,
            jobs=self.jobs,
        )
        self._stopping = asyncio.Event()
        self._offset: Optional[int] = None

    # ------------------------------------------------------------------ #
    async def setup(self) -> None:
        """启动前的一次性准备。"""
        self.settings.ensure_directories()
        self.db.initialize()
        register_all(self.dispatcher)
        await self.bot.start()
        await self._bootstrap_credentials()
        await self._register_bot_commands()
        if self.settings.banner:
            t = get_translator(self.settings.language)
            logger.info(t(
                "app.banner",
                version=__version__,
                bot=self.bot.username or "-",
                providers=", ".join(available_provider_names(self.settings)),
            ))
        logger.info("配置快照：%s", self.settings.as_log_fields())

    async def _bootstrap_credentials(self) -> None:
        """把 .env 里预置的凭证加密写入数据库（首次启动即开箱可用）。"""
        bootstrap = self.settings.bootstrap
        owner = self.settings.admin_ids[0] if self.settings.admin_ids else 0
        if owner:
            self.db.ensure_user(owner, "bootstrap-admin", role="admin")

        candidates: List[Dict[str, Any]] = []
        if bootstrap.has_digitalocean:
            candidates.append({
                "provider": "digitalocean",
                "label": bootstrap.label,
                "secrets": {"token": bootstrap.digitalocean_token},
            })
        if bootstrap.has_aws:
            candidates.append({
                "provider": "aws",
                "label": bootstrap.label,
                "secrets": {
                    "access_key_id": bootstrap.aws_access_key_id,
                    "secret_access_key": bootstrap.aws_secret_access_key,
                    "session_token": bootstrap.aws_session_token,
                    "region": bootstrap.aws_region or self.settings.aws.region,
                },
            })

        for candidate in candidates:
            provider_name = candidate["provider"]
            if self.db.count_credentials(provider_name) > 0:
                logger.debug("已存在 %s 凭证，跳过 .env 预置", provider_name)
                continue
            try:
                provider = create_provider(provider_name, candidate["secrets"], self.settings,
                                          label=candidate["label"])
                identity = await asyncio.to_thread(provider.validate_credentials)
                provider.close()
            except Exception as exc:
                logger.warning("跳过 .env 预置的 %s 凭证（校验失败）：%s", provider_name, exc)
                continue
            credential = self.store.bind(owner, provider_name, candidate["label"],
                                         candidate["secrets"], shared=True, activate=True)
            self.store.update_meta(credential, {"identity": identity, "source": "env"})
            logger.info("已从 .env 导入 %s 凭证（%s）", provider_name, identity)

    def _command_menu(self, *, include_admin: bool) -> List[Dict[str, str]]:
        """构造 Telegram 指令菜单：``include_admin=False`` 时隐藏写操作指令。"""
        commands = []
        for spec in self.dispatcher.specs():
            if spec.hidden:
                continue
            if spec.admin_only and not include_admin:
                continue
            summary = spec.describe(self.settings.language) or spec.name
            commands.append({"command": spec.name, "description": summary[:250]})
        return commands

    async def _register_bot_commands(self) -> None:
        """注册 Telegram 指令菜单（用户输入 ``/`` 时出现）。

        默认作用域只挂**公共**指令，避免把写操作指令暴露给未授权用户；
        管理员则用 chat 作用域单独挂上完整菜单（移动端体验的关键一笔）。
        """
        await self.bot.set_my_commands(self._command_menu(include_admin=False))
        admin_commands = self._command_menu(include_admin=True)
        for admin_id in self.settings.admin_ids:
            await self.bot.set_my_commands(admin_commands,
                                           scope={"type": "chat", "chat_id": admin_id})

    # ------------------------------------------------------------------ #
    async def _drain_pending(self) -> None:
        """丢弃 Bot 停机期间堆积的更新，避免重启后执行过期指令。"""
        try:
            updates = await self.bot.get_updates(offset=None, timeout=0, limit=DRAIN_LIMIT)
        except TelegramError as exc:
            logger.warning("清理历史更新失败：%s", exc)
            return
        if updates:
            self._offset = max(int(item.get("update_id", 0)) for item in updates) + 1
            logger.info("已跳过 %d 条停机期间的积压更新", len(updates))

    async def run(self) -> None:
        """长轮询主循环（论文 5.2：Bot 持续监听群组消息）。"""
        await self._drain_pending()
        self._install_signal_handlers()
        logger.info("开始长轮询（timeout=%ss）…", self.settings.telegram.poll_timeout)
        backoff = 1.0
        while not self._stopping.is_set():
            try:
                updates = await self.bot.get_updates(offset=self._offset)
                backoff = 1.0
            except TelegramError as exc:
                if exc.conflict:
                    logger.error("检测到 409 冲突：同一 Bot Token 有另一个实例在轮询，"
                                 "请先停止它（等待 10s 后重试）")
                    await self._sleep(10.0)
                    continue
                logger.warning("拉取更新失败：%s（%.0fs 后重试）", exc, backoff)
                await self._sleep(backoff)
                backoff = min(60.0, backoff * 2)
                continue
            except asyncio.CancelledError:  # pragma: no cover
                raise
            except Exception:  # pragma: no cover - 网络层兜底
                logger.exception("长轮询异常，%.0fs 后重试", backoff)
                await self._sleep(backoff)
                backoff = min(60.0, backoff * 2)
                continue

            for update in updates:
                self._offset = int(update.get("update_id", 0)) + 1
                await self.dispatcher.handle_update(update)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        except TimeoutError:
            pass

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.request_stop)
            except (NotImplementedError, RuntimeError):  # pragma: no cover - Windows
                signal.signal(sig, lambda *_: self.request_stop())

    def request_stop(self) -> None:
        if not self._stopping.is_set():
            t = get_translator(self.settings.language)
            logger.info(t("app.stopping"))
            self._stopping.set()

    # ------------------------------------------------------------------ #
    async def shutdown(self) -> None:
        """优雅退出：等待后台任务 → 关闭 HTTP 会话。"""
        self.confirms.purge()
        await self.jobs.shutdown(timeout=30.0)
        await self.bot.close()
        t = get_translator(self.settings.language)
        logger.info(t("app.stopped"))


def build_application(settings: Optional[Settings] = None,
                      env_file: Optional[str] = ".env",
                      *, bot: Optional[TelegramClient] = None) -> Application:
    """按约定装配应用（供 main.py 与测试复用）。"""
    settings = settings or Settings.from_env(env_file)
    settings.validate()
    setup_logging(settings.log_level, settings.log_file)
    return Application(settings, bot=bot)
