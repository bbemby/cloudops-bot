"""Telegram Bot API 客户端（论文 2.1：长轮询 + 异步事件驱动）。

为什么不直接用 python-telegram-bot？

* 论文 2.1 明确要求"长轮询 + 异步事件驱动"，且强调在无公网 IP 的机器上部署；
* 本项目只需要 6 个 API，自己实现可让依赖保持在 aiohttp 一个库，
  同时把 409 冲突、429 限流、网络抖动这些"运维现场才会遇到"的问题处理干净。

传输层被抽象为 :class:`TelegramTransport`，测试中可以替换为记录桩，
从而在不联网的前提下做端到端测试。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Sequence

from ..config import Settings
from ..errors import CloudOpsError
from ..utils import split_message

logger = logging.getLogger(__name__)

TELEGRAM_MESSAGE_LIMIT = 4096


class TelegramError(CloudOpsError):
    """Telegram API 返回的业务错误。"""

    default_key = "error.telegram"

    def __init__(self, method: str, description: str, *, error_code: int = 0,
                 retry_after: Optional[float] = None) -> None:
        super().__init__(f"Telegram {method} 失败（{error_code}）：{description}",
                         key="error.telegram")
        self.method = method
        self.description = description
        self.error_code = error_code
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        if self.error_code == 429:
            return True
        if self.error_code in (500, 502, 503, 504):
            return True
        return "Too Many Requests" in self.description

    @property
    def conflict(self) -> bool:
        """409：同一个 Bot Token 有另一个进程在轮询。"""
        return self.error_code == 409


# --------------------------------------------------------------------------- #
# 更新对象
# --------------------------------------------------------------------------- #
@dataclass
class TelegramUserInfo:
    id: int
    username: Optional[str] = None
    first_name: Optional[str] = None
    is_bot: bool = False


@dataclass
class IncomingMessage:
    message_id: int
    chat_id: int
    user: TelegramUserInfo
    text: str = ""
    chat_type: str = "private"
    reply_to: Optional[int] = None
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_private(self) -> bool:
        return self.chat_type == "private"


@dataclass
class CallbackQuery:
    id: str
    user: TelegramUserInfo
    chat_id: int
    message_id: Optional[int]
    data: str = ""
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)


# --------------------------------------------------------------------------- #
# 传输层
# --------------------------------------------------------------------------- #
class TelegramTransport(Protocol):  # pragma: no cover - 协议定义
    async def request(self, method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        ...

    async def close(self) -> None:
        ...


class HttpTransport:
    """基于 aiohttp 的 HTTP 传输实现。"""

    def __init__(self, base_url: str, token: str, *, timeout: float = 35.0,
                 proxy: Optional[str] = None,
                 session: Optional["aiohttp.ClientSession"] = None) -> None:  # noqa: F821
        self.base_url = f"{base_url}/bot{token}"
        self.timeout = timeout
        self.proxy = proxy
        self._session = session
        self._owns_session = session is None

    async def _ensure_session(self) -> "aiohttp.ClientSession":  # noqa: F821
        if self._session is None or self._session.closed:
            import aiohttp

            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    async def request(self, method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        import aiohttp

        session = await self._ensure_session()
        url = f"{self.base_url}/{method}"
        timeout = aiohttp.ClientTimeout(total=self.timeout + float(payload.get("timeout") or 0))
        async with session.post(url, json=payload, timeout=timeout, proxy=self.proxy) as response:
            try:
                data = await response.json(content_type=None)
            except Exception as exc:  # pragma: no cover - 网络异常
                raise TelegramError(method, f"响应解析失败：{exc}", error_code=response.status) from exc
        if not isinstance(data, dict):
            raise TelegramError(method, "响应格式非法", error_code=response.status)
        if not data.get("ok"):
            parameters = data.get("parameters") or {}
            raise TelegramError(
                method,
                str(data.get("description") or "unknown error"),
                error_code=int(data.get("error_code") or response.status),
                retry_after=parameters.get("retry_after"),
            )
        return data

    async def close(self) -> None:
        if self._session is not None and self._owns_session and not self._session.closed:
            await self._session.close()


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #
class TelegramClient:
    """对上层暴露"发送消息 / 拉取更新"两个核心能力的 Bot 客户端。"""

    def __init__(self, settings: Settings, *, transport: Optional[TelegramTransport] = None,
                 bot_name: Optional[str] = None) -> None:
        self.settings = settings
        self.transport = transport or HttpTransport(
            settings.telegram.api_base,
            settings.telegram.token,
            timeout=settings.telegram.request_timeout,
            proxy=settings.telegram.proxy,
        )
        self.username: Optional[str] = None
        self.bot_id: Optional[int] = None
        self.bot_name = bot_name

    # ------------------------------------------------------------------ #
    async def close(self) -> None:
        await self.transport.close()

    async def _call(self, method: str, payload: Dict[str, Any], *, retries: Optional[int] = None) -> Any:
        """带重试的 API 调用：429 遵守 retry_after，5xx/网络错误指数退避。"""
        attempts = self.settings.telegram.retries if retries is None else retries
        delay = 1.0
        last_error: Optional[Exception] = None
        for attempt in range(attempts + 1):
            try:
                response = await self.transport.request(method, payload)
                return response.get("result")
            except TelegramError as exc:
                last_error = exc
                if not exc.retryable or attempt >= attempts:
                    raise
                wait = exc.retry_after or delay
                logger.warning("Telegram %s 可重试错误（%.0fs 后重试）：%s", method, wait, exc.description)
                await asyncio.sleep(wait)
            except Exception as exc:  # 网络层异常
                last_error = exc
                if attempt >= attempts:
                    raise TelegramError(method, f"网络异常：{exc}") from exc
                await asyncio.sleep(delay)
            delay = min(30.0, delay * 2)
        raise TelegramError(method, f"重试耗尽：{last_error}")  # pragma: no cover

    # ------------------------------------------------------------------ #
    async def start(self) -> Dict[str, Any]:
        """``getMe``：校验 Token 并记录 Bot 用户名（用于 ``/cmd@botname``）。"""
        try:
            me = await self._call("getMe", {})
        except BaseException:
            # Token 写错或网络不通时，连接池已经建好了；这里立刻收掉，
            # 否则进程退出时会留下 "Unclosed client session" 噪音
            await self.close()
            raise
        me = me or {}
        self.username = me.get("username")
        self.bot_id = me.get("id")
        self.bot_name = me.get("first_name") or self.username
        logger.info("Telegram Bot 已连接：@%s (id=%s)", self.username, self.bot_id)
        return me

    async def get_updates(self, *, offset: Optional[int] = None, timeout: Optional[int] = None,
                          limit: int = 50) -> List[Dict[str, Any]]:
        payload: Dict[str, Any] = {
            "timeout": int(timeout if timeout is not None else self.settings.telegram.poll_timeout),
            "limit": limit,
            "allowed_updates": ["message", "callback_query", "edited_message"],
        }
        if offset is not None:
            payload["offset"] = offset
        return await self._call("getUpdates", payload) or []

    async def send_message(self, chat_id: int, text: str, *,
                           reply_to: Optional[int] = None,
                           reply_markup: Optional[Dict[str, Any]] = None,
                           disable_notification: bool = False) -> List[Dict[str, Any]]:
        """发送消息，自动按 4096 字上限切分；超长时按钮挂在最后一段。"""
        chunks = split_message(text or "", TELEGRAM_MESSAGE_LIMIT - 96)
        sent: List[Dict[str, Any]] = []
        for index, chunk in enumerate(chunks):
            is_last = index == len(chunks) - 1
            payload: Dict[str, Any] = {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
                "disable_notification": disable_notification,
            }
            if reply_to and index == 0:
                payload["reply_to_message_id"] = reply_to
                payload["allow_sending_without_reply"] = True
            if reply_markup is not None and is_last:
                payload["reply_markup"] = reply_markup
            sent.append(await self._call("sendMessage", payload))
        return sent

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        if not self.settings.telegram.show_typing:
            return
        try:
            await self._call("sendChatAction", {"chat_id": chat_id, "action": action}, retries=0)
        except Exception:  # pragma: no cover - 纯粹的体验优化，失败可忽略
            logger.debug("sendChatAction 失败", exc_info=True)

    async def answer_callback_query(self, callback_id: str, *, text: Optional[str] = None,
                                    show_alert: bool = False) -> None:
        payload: Dict[str, Any] = {"callback_query_id": callback_id, "show_alert": show_alert}
        if text:
            payload["text"] = text[:200]
        await self._call("answerCallbackQuery", payload)

    async def edit_message_text(self, chat_id: int, message_id: int, text: str,
                                reply_markup: Optional[Dict[str, Any]] = None) -> None:
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text[:TELEGRAM_MESSAGE_LIMIT - 1],
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        await self._call("editMessageText", payload)

    async def set_my_commands(self, commands: Sequence[Dict[str, str]],
                              *, scope: Optional[Dict[str, Any]] = None) -> None:
        """注册指令菜单（用户输入 ``/`` 时出现），提升移动端体验。

        ``scope`` 为空表示默认作用域（所有用户）；传
        ``{"type": "chat", "chat_id": <管理员ID>}`` 可给管理员单独挂上
        包含写操作指令的完整菜单。
        """
        if not commands:
            return
        payload: Dict[str, Any] = {"commands": list(commands)}
        if scope is not None:
            payload["scope"] = scope
        try:
            await self._call("setMyCommands", payload)
        except Exception:  # pragma: no cover
            logger.warning("setMyCommands 失败（不影响核心功能）", exc_info=True)

    # ------------------------------------------------------------------ #
    @staticmethod
    def parse_message(raw: Dict[str, Any]) -> Optional[IncomingMessage]:
        message = raw.get("message") or raw.get("edited_message")
        if not message:
            return None
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        return IncomingMessage(
            message_id=int(message.get("message_id") or 0),
            chat_id=int(chat.get("id") or 0),
            user=TelegramUserInfo(
                id=int(sender.get("id") or 0),
                username=sender.get("username"),
                first_name=sender.get("first_name"),
                is_bot=bool(sender.get("is_bot")),
            ),
            text=(message.get("text") or "").strip(),
            chat_type=str(chat.get("type") or "private"),
            reply_to=((message.get("reply_to_message") or {}).get("message_id")),
            raw=message,
        )

    @staticmethod
    def parse_callback(raw: Dict[str, Any]) -> Optional[CallbackQuery]:
        query = raw.get("callback_query")
        if not query:
            return None
        sender = query.get("from") or {}
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        return CallbackQuery(
            id=str(query.get("id") or ""),
            user=TelegramUserInfo(
                id=int(sender.get("id") or 0),
                username=sender.get("username"),
                first_name=sender.get("first_name"),
                is_bot=bool(sender.get("is_bot")),
            ),
            chat_id=int(chat.get("id") or 0),
            message_id=message.get("message_id"),
            data=str(query.get("data") or ""),
            raw=query,
        )
