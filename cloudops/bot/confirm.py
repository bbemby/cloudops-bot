"""二次确认机制（论文 4.3：销毁云服务器需二次确认后释放资源）。

确认码放在内存里并带 TTL：单进程、轻量、重启即失效（这正是我们想要的语义
——重启后没有人可以"照着旧确认码"继续删机器）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional

from ..errors import ConfirmationInvalid
from ..utils import random_code

ContextRunner = Callable[[Any], Awaitable[None]]


@dataclass
class PendingAction:
    """一次等待确认的危险操作。"""

    code: str
    user_id: int
    chat_id: int
    action: str
    runner: ContextRunner
    ttl: float
    created_at: float = field(default_factory=time.monotonic)
    data: Dict[str, Any] = field(default_factory=dict)

    @property
    def age(self) -> float:
        return time.monotonic() - self.created_at

    @property
    def expired(self) -> bool:
        return self.age > self.ttl

    @property
    def remaining(self) -> int:
        return max(0, int(self.ttl - self.age))


class ConfirmationStore:
    """确认码仓库。"""

    def __init__(self, ttl_seconds: int = 300, *, code_length: int = 6) -> None:
        self.ttl = float(ttl_seconds)
        self.code_length = code_length
        self._pending: Dict[str, PendingAction] = {}

    def create(self, *, user_id: int, chat_id: int, action: str, runner: ContextRunner,
               data: Optional[Dict[str, Any]] = None) -> PendingAction:
        self.purge()
        code = self._unique_code()
        pending = PendingAction(
            code=code,
            user_id=int(user_id),
            chat_id=int(chat_id),
            action=action,
            runner=runner,
            ttl=self.ttl,
            data=dict(data or {}),
        )
        self._pending[code] = pending
        return pending

    def peek(self, code: str, user_id: Optional[int] = None) -> Optional[PendingAction]:
        pending = self._pending.get((code or "").strip().upper())
        if pending is None:
            return None
        if pending.expired:
            self._pending.pop(pending.code, None)
            return None
        if user_id is not None and pending.user_id != int(user_id):
            return None
        return pending

    def consume(self, code: str, user_id: int) -> PendingAction:
        """取出并作废确认码；无效/过期/越权都会抛 :class:`ConfirmationInvalid`。"""
        normalized = (code or "").strip().upper()
        pending = self._pending.get(normalized)
        if pending is None:
            raise ConfirmationInvalid(key="confirm.invalid")
        if pending.expired:
            self._pending.pop(normalized, None)
            raise ConfirmationInvalid(params={"code": normalized}, key="confirm.expired")
        if pending.user_id != int(user_id):
            # 关键安全点：不允许他人代确认（论文 TC-04 越权拦截的延伸）
            raise ConfirmationInvalid(key="confirm.invalid")
        self._pending.pop(normalized, None)
        return pending

    def cancel(self, code: str, user_id: int) -> PendingAction:
        normalized = (code or "").strip().upper()
        pending = self._pending.get(normalized)
        if pending is None:
            raise ConfirmationInvalid(key="confirm.invalid")
        if pending.user_id != int(user_id):
            raise ConfirmationInvalid(key="confirm.invalid")
        self._pending.pop(normalized, None)
        return pending

    def pending_for(self, user_id: int) -> Optional[PendingAction]:
        for pending in self._pending.values():
            if pending.user_id == int(user_id) and not pending.expired:
                return pending
        return None

    def purge(self) -> int:
        expired = [code for code, pending in self._pending.items() if pending.expired]
        for code in expired:
            self._pending.pop(code, None)
        return len(expired)

    def __len__(self) -> int:
        return len(self._pending)

    def _unique_code(self) -> str:
        for _ in range(50):
            code = random_code(self.code_length)
            if code not in self._pending:
                return code
        raise RuntimeError("无法生成唯一确认码")  # pragma: no cover
