"""``cloudops.bot`` —— 交互层（论文 5.1）。"""

from __future__ import annotations

from .confirm import ConfirmationStore, PendingAction
from .dispatcher import (
    CommandSpec,
    Context,
    Dispatcher,
    GROUP_CRED,
    GROUP_OTHER,
    GROUP_READ,
    GROUP_WRITE,
    confirm_keyboard,
)
from .jobs import JobRunner
from .ratelimit import RateLimiter
from .telegram import (
    CallbackQuery,
    HttpTransport,
    IncomingMessage,
    TelegramClient,
    TelegramError,
    TelegramTransport,
    TelegramUserInfo,
)

__all__ = [
    "ConfirmationStore",
    "PendingAction",
    "CommandSpec",
    "Context",
    "Dispatcher",
    "GROUP_CRED",
    "GROUP_OTHER",
    "GROUP_READ",
    "GROUP_WRITE",
    "confirm_keyboard",
    "JobRunner",
    "RateLimiter",
    "CallbackQuery",
    "HttpTransport",
    "IncomingMessage",
    "TelegramClient",
    "TelegramError",
    "TelegramTransport",
    "TelegramUserInfo",
]
