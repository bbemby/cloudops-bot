"""指令路由与权限校验（论文 5.1 业务逻辑层的"指令路由解析器 + 权限校验模块"）。

一次更新的完整生命周期：

1. 解析更新（消息 / 回调按钮）；
2. 归属用户（env 白名单优先，其次数据库角色）；
3. 指令解析（支持 ``/cmd@botname``、别名、引号参数）；
4. 权限校验（TC-04：未授权用户直接被拦截，**不会**向云厂商发起任何请求）；
5. 频率限制；
6. 交给处理器；异常统一渲染成用户可读文案并写审计日志。
"""

from __future__ import annotations

import difflib
import logging
import shlex
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from .. import __version__
from ..config import Settings
from ..db import Database
from ..credentials import CredentialStore
from ..errors import (
    CloudOpsError,
    ConfirmationInvalid,
    PermissionDenied,
    RateLimited,
    ValidationError,
)
from ..i18n import get_translator
from ..models import (
    LOG_DENIED,
    LOG_SUCCESS,
    ROLE_ADMIN,
    ROLE_GUEST,
    ROLE_READONLY,
    ROLES,
    User,
)
from ..utils import humanize_duration
from .confirm import ConfirmationStore
from .formatter import error_text
from .jobs import JobRunner
from .ratelimit import RateLimiter
from .telegram import CallbackQuery, IncomingMessage, TelegramClient, TelegramUserInfo

logger = logging.getLogger(__name__)

Handler = Callable[["Context", List[str]], Awaitable[None]]

GROUP_READ = "read"
GROUP_WRITE = "write"
GROUP_CRED = "cred"
GROUP_OTHER = "other"


def confirm_keyboard(t, code: str) -> Dict[str, Any]:
    """确认/取消 内联按钮（移动端体验的关键一笔）。"""
    return {
        "inline_keyboard": [[
            {"text": t("confirm.button.confirm"), "callback_data": f"confirm:{code}"},
            {"text": t("confirm.button.cancel"), "callback_data": f"cancel:{code}"},
        ]]
    }


@dataclass
class CommandSpec:
    """一条指令的元数据 + 处理器。"""

    name: str
    handler: Handler
    summary: Dict[str, str] = field(default_factory=dict)
    usage: str = ""
    group: str = GROUP_OTHER
    admin_only: bool = False
    aliases: Tuple[str, ...] = ()
    hidden: bool = False

    def describe(self, lang: str) -> str:
        return self.summary.get(lang) or self.summary.get("zh") or ""

    @property
    def all_names(self) -> Tuple[str, ...]:
        return (self.name,) + tuple(self.aliases)


class Context:
    """一次指令执行的上下文：用户、参数、回复通道、数据访问句柄。"""

    def __init__(self, dispatcher: "Dispatcher", *, user: User, chat_id: int,
                 args: Sequence[str] = (), command: Optional[str] = None,
                 message: Optional[IncomingMessage] = None,
                 callback: Optional[CallbackQuery] = None,
                 started_at: Optional[float] = None) -> None:
        self.dispatcher = dispatcher
        self.settings = dispatcher.settings
        self.db = dispatcher.db
        self.store = dispatcher.store
        self.bot = dispatcher.bot
        self.confirms = dispatcher.confirms
        self.jobs = dispatcher.jobs
        self.user = user
        self.chat_id = int(chat_id)
        self.args = list(args)
        self.command = command
        self.message = message
        self.callback = callback
        self.started_at = started_at if started_at is not None else time.monotonic()

    # -- 便捷访问 ------------------------------------------------------- #
    @property
    def t(self):
        return get_translator(self.settings.language)

    @property
    def lang(self) -> str:
        return self.settings.language

    @property
    def user_id(self) -> int:
        return self.user.telegram_id

    @property
    def is_callback(self) -> bool:
        return self.callback is not None

    @property
    def is_admin(self) -> bool:
        return self.user.role == ROLE_ADMIN

    @property
    def raw_text(self) -> str:
        if self.message is not None:
            return self.message.text
        return ""

    def original_message_id(self) -> Optional[int]:
        if self.message is not None:
            return self.message.message_id
        if self.callback is not None:
            return self.callback.message_id
        return None

    # -- 输出 ----------------------------------------------------------- #
    async def reply(self, text: str, *, reply_markup: Optional[Dict[str, Any]] = None,
                    reply_to: bool = True, disable_notification: bool = False) -> None:
        if not text:
            return
        await self.bot.send_message(
            self.chat_id,
            text,
            reply_to=self.original_message_id() if reply_to else None,
            reply_markup=reply_markup,
            disable_notification=disable_notification,
        )

    async def send_to(self, chat_id: int, text: str,
                      reply_markup: Optional[Dict[str, Any]] = None) -> None:
        """向任意会话推送（例如给管理员发越权告警）。"""
        await self.bot.send_message(chat_id, text, reply_markup=reply_markup)

    async def typing(self, action: str = "typing") -> None:
        await self.bot.send_chat_action(self.chat_id, action)

    # -- 审计 ----------------------------------------------------------- #
    def log(self, action: str, status: str = LOG_SUCCESS, *, provider: Optional[str] = None,
            target: Optional[str] = None, detail: Optional[str] = None) -> None:
        try:
            self.db.log_operation(self.user_id, action, status, provider=provider,
                                  target=target, detail=detail)
        except Exception:  # pragma: no cover - 日志失败不应影响主流程
            logger.warning("写入操作日志失败：%s", action, exc_info=True)


class Dispatcher:
    """指令注册表 + 更新分派器。"""

    def __init__(self, *, settings: Settings, db: Database, store: CredentialStore,
                 bot: TelegramClient, confirms: ConfirmationStore, jobs: JobRunner) -> None:
        self.settings = settings
        self.db = db
        self.store = store
        self.bot = bot
        self.confirms = confirms
        self.jobs = jobs
        self.rate_limiter = RateLimiter(settings.rate_limit_per_minute, window=60.0)
        self._specs: Dict[str, CommandSpec] = {}
        self._user_cache: Dict[int, Tuple[float, User]] = {}
        self.started_at = time.monotonic()
        self.stats = {"updates": 0, "commands": 0, "errors": 0}

    # ------------------------------------------------------------------ #
    # 注册
    # ------------------------------------------------------------------ #
    def register(self, spec: CommandSpec) -> CommandSpec:
        if spec.name in self._specs:
            raise ValueError(f"指令重复注册：{spec.name}")
        self._specs[spec.name] = spec
        return spec

    def command(self, name: str, *, summary: Optional[Dict[str, str]] = None,
                usage: str = "", group: str = GROUP_OTHER, admin_only: bool = False,
                aliases: Sequence[str] = (), hidden: bool = False) -> Callable[[Handler], Handler]:
        """装饰器：``@dispatcher.command("list", ...)``。"""

        def decorator(handler: Handler) -> Handler:
            self.register(CommandSpec(
                name=name, handler=handler, summary=summary or {}, usage=usage,
                group=group, admin_only=admin_only, aliases=tuple(aliases), hidden=hidden,
            ))
            return handler

        return decorator

    def specs(self) -> List[CommandSpec]:
        return sorted(self._specs.values(), key=lambda spec: (spec.group, spec.name))

    def spec_names(self) -> List[str]:
        names: List[str] = []
        for spec in self._specs.values():
            names.extend(spec.all_names)
        return sorted(set(names))

    def find(self, name: str) -> Optional[CommandSpec]:
        key = (name or "").strip().lower().lstrip("/")
        if key in self._specs:
            return self._specs[key]
        for spec in self._specs.values():
            if key in spec.aliases:
                return spec
        return None

    # ------------------------------------------------------------------ #
    # 更新入口
    # ------------------------------------------------------------------ #
    async def handle_update(self, update: Dict[str, Any]) -> None:
        self.stats["updates"] += 1
        try:
            callback = TelegramClient.parse_callback(update)
            if callback is not None:
                await self._handle_callback(callback)
                return
            message = TelegramClient.parse_message(update)
            if message is not None:
                await self._handle_message(message)
        except Exception:  # pragma: no cover - 兜底：任何异常都不能杀死轮询
            self.stats["errors"] += 1
            logger.exception("处理更新失败：%s", update)

    # ------------------------------------------------------------------ #
    async def _handle_callback(self, callback: CallbackQuery) -> None:
        user = self.resolve_user(callback.user)
        action, _, code = (callback.data or "").partition(":")
        spec = self.find("confirm" if action == "confirm" else "cancel")
        try:
            if spec is None:  # pragma: no cover - 正常不会发生
                return
            if action == "confirm":
                await self.bot.answer_callback_query(callback.id)
            else:
                pending = self.confirms.peek(code, user.telegram_id)
                await self.bot.answer_callback_query(callback.id)
                if pending is None:
                    raise ConfirmationInvalid(key="confirm.invalid")
            context = Context(self, user=user, chat_id=callback.chat_id or user.telegram_id,
                              args=[code], command=spec.name, callback=callback)
            await self._execute(context, spec)
        except CloudOpsError as exc:
            await self.bot.send_message(
                callback.chat_id or user.telegram_id,
                error_text(exc, get_translator(self.settings.language)),
            )
        except Exception:
            self.stats["errors"] += 1
            logger.exception("处理回调失败：%s", callback.data)

    async def _handle_message(self, message: IncomingMessage) -> None:
        if message.user.is_bot or not message.chat_id:
            return
        user = self.resolve_user(message.user)
        text = (message.text or "").strip()

        if not text.startswith("/"):
            # 非指令消息：若有等待确认的操作则提醒确认码，否则给一句轻量提示
            t = get_translator(self.settings.language)
            pending = self.confirms.pending_for(user.telegram_id)
            if pending is not None:
                await self.bot.send_message(
                    message.chat_id,
                    t("pending.hint", code=pending.code, ttl=pending.remaining),
                    reply_to=message.message_id,
                )
            elif message.is_private:
                await self.bot.send_message(
                    message.chat_id, t("help.unknown_hint"), reply_to=message.message_id
                )
            return

        raw_command, args = self.parse_text(text)
        spec = self.find(raw_command)
        context = Context(self, user=user, chat_id=message.chat_id, args=args,
                          command=raw_command, message=message)
        t = context.t

        if spec is None:
            suggestion = self.suggest(raw_command)
            hint = t("help.did_you_mean", suggestion=suggestion) if suggestion else t("help.unknown_hint")
            await context.reply(t("help.unknown_command", command=raw_command, hint=hint))
            context.log(f"unknown:{raw_command}", LOG_DENIED, detail=text[:200])
            return

        await self._execute(context, spec)

    # ------------------------------------------------------------------ #
    async def _execute(self, context: Context, spec: CommandSpec) -> None:
        self.stats["commands"] += 1
        t = context.t
        try:
            self.authorize(context, spec)
            retry_after = self.rate_limiter.check(context.user_id)
            if retry_after is not None:
                raise RateLimited(retry_after)
        except PermissionDenied as exc:
            await self._reject(context, spec, exc, t)
            return
        except RateLimited as exc:
            context.log(spec.name, LOG_DENIED, detail="rate limited")
            await context.reply(t("denied.rate", seconds=int(exc.retry_after)))
            return

        await context.typing()
        try:
            await spec.handler(context, context.args)
        except CloudOpsError as exc:
            rendered = error_text(exc, t)
            if len(rendered) > 60 or "\n" in rendered:
                logger.warning("指令 %s 失败：%s", spec.name, exc)
            context.log(spec.name, exc_status(exc), detail=rendered)
            await context.reply(rendered)
        except Exception as exc:  # pragma: no cover - 兜底
            self.stats["errors"] += 1
            logger.exception("指令 %s 执行异常", spec.name)
            context.log(spec.name, "failed", detail=f"{type(exc).__name__}: {exc}")
            await context.reply(t("error.internal_plain"))

    async def _reject(self, context: Context, spec: CommandSpec, exc: CloudOpsError, t) -> None:
        """越权拦截：回复 + 审计 + 可选通知管理员（TC-04）。"""
        if context.user.role == ROLE_GUEST:
            message = t("denied.guest", user_id=context.user_id)
        else:
            message = t("denied.permission", role=context.user.role_label, user_id=context.user_id)
        context.log(spec.name, LOG_DENIED, detail=f"denied by role={context.user.role}")
        await context.reply(message)
        logger.info("越权拦截：user=%s role=%s command=%s", context.user_id,
                    context.user.role, spec.name)
        if self.settings.notify_admins_on_denied:
            for admin_id in self.settings.admin_ids:
                if admin_id == context.user_id:
                    continue
                await self.bot.send_message(
                    admin_id,
                    f"⛔️ 拦截到越权尝试\n用户: {context.user.display()}\n"
                    f"指令: /{spec.name}\n角色: {context.user.role}",
                    disable_notification=True,
                )

    # ------------------------------------------------------------------ #
    def authorize(self, context: Context, spec: CommandSpec) -> None:
        """权限校验矩阵：admin 全量；readonly 只读；guest 一律拒绝。"""
        if spec.admin_only:
            if not context.user.is_admin:
                raise PermissionDenied()
            return
        if not context.user.can_read:
            raise PermissionDenied()

    def resolve_user(self, info: TelegramUserInfo) -> User:
        """env 白名单优先，其次数据库角色（manage.py 可动态调整）。"""
        cached = self._user_cache.get(info.id)
        now = time.monotonic()
        if cached and now - cached[0] < 30.0:
            return cached[1]
        role: Optional[str] = None
        if info.id in self.settings.admin_ids:
            role = ROLE_ADMIN
        elif info.id in self.settings.readonly_ids:
            role = ROLE_READONLY
        user = self.db.ensure_user(info.id, info.username, role=role)
        if user.role not in ROLES:  # pragma: no cover - 数据被外部改坏的兜底
            user = self.db.set_role(info.id, ROLE_GUEST)
        self._user_cache[info.id] = (now, user)
        return user

    # ------------------------------------------------------------------ #
    @staticmethod
    def parse_text(text: str) -> Tuple[str, List[str]]:
        """解析 ``/create do sgp1 "1gb"`` 形式，支持引号包裹含空格的参数。"""
        stripped = text.strip()
        if stripped.startswith("/"):
            stripped = stripped[1:]
        # 去掉 @botname 后缀（群聊里手机端会自动补全）
        head, _, tail = stripped.partition(" ")
        if "@" in head:
            head = head.split("@", 1)[0]
        raw_args = tail.strip()
        if not raw_args:
            return head.lower(), []
        try:
            args = shlex.split(raw_args, posix=True)
        except ValueError:
            # 引号不配对时退回朴素切分，并顺手去掉残留的引号字符，
            # 免得用户看到 `name="unbalanced` 这种脏参数
            args = [token.strip("\"'") for token in raw_args.split()]
        return head.lower(), args

    def suggest(self, name: str) -> Optional[str]:
        matches = difflib.get_close_matches((name or "").lower(), self.spec_names(), n=1, cutoff=0.6)
        return matches[0] if matches else None

    # ------------------------------------------------------------------ #
    def uptime(self) -> str:
        return humanize_duration(time.monotonic() - self.started_at)

    def version(self) -> str:
        return __version__


def exc_status(exc: CloudOpsError) -> str:
    """把异常映射为审计日志的状态字段。"""
    from ..errors import PermissionDenied as _PermissionDenied
    from ..errors import RateLimited as _RateLimited
    from ..errors import ValidationError as _ValidationError

    if isinstance(exc, (_PermissionDenied, _RateLimited)):
        return LOG_DENIED
    if isinstance(exc, _ValidationError):
        return "invalid"
    return "failed"
