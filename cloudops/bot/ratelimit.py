"""简单的滑动窗口限流（防止误触/脚本狂刷导致云端 API 被牵连）。"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Deque, Dict, Optional


class RateLimiter:
    """按用户维度的滑动窗口限流器。``limit <= 0`` 表示关闭限流。"""

    def __init__(self, limit: int = 20, window: float = 60.0) -> None:
        self.limit = limit
        self.window = window
        self._hits: Dict[int, Deque[float]] = defaultdict(deque)

    def check(self, user_id: int, *, cost: int = 1) -> Optional[float]:
        """返回 ``None`` 表示放行；否则返回建议的重试等待秒数。"""
        if self.limit <= 0:
            return None
        now = time.monotonic()
        hits = self._hits[user_id]
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(hits) + cost > self.limit:
            retry_after = self.window - (now - hits[0]) if hits else self.window
            return max(1.0, retry_after)
        for _ in range(cost):
            hits.append(now)
        return None

    def reset(self, user_id: Optional[int] = None) -> None:
        if user_id is None:
            self._hits.clear()
        else:
            self._hits.pop(user_id, None)
