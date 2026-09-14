"""后台任务（论文 5.2：任务下发后 Bot 进入轮询，就绪后把 IP 推送回聊天窗口）。

职责：

* 统一管理 ``asyncio.Task`` 的生命周期（弱引用集合 + 优雅退出等待）；
* 把阻塞的 requests 调用丢进线程池（``asyncio.to_thread``），
  保证长轮询循环永远不会被云 API 阻塞；
* 兜底捕获异常并写日志，避免"任务静默消失"。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Coroutine, Set

logger = logging.getLogger(__name__)


class JobRunner:
    """轻量的后台任务调度器。"""

    def __init__(self, *, name: str = "cloudops") -> None:
        self.name = name
        self._tasks: Set[asyncio.Task] = set()
        self._counter = 0

    @property
    def active(self) -> int:
        return len([task for task in self._tasks if not task.done()])

    def spawn(self, coro: Coroutine[Any, Any, Any], *, label: str = "job") -> asyncio.Task:
        """提交一个后台协程，返回 Task（异常都会被记录）。"""
        self._counter += 1
        task = asyncio.create_task(self._guarded(coro, label=f"{label}#{self._counter}"),
                                   name=f"{self.name}-{label}-{self._counter}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _guarded(self, coro: Awaitable[Any], *, label: str) -> Any:
        try:
            return await coro
        except asyncio.CancelledError:  # pragma: no cover - 退出流程
            logger.info("后台任务 %s 被取消", label)
            raise
        except Exception:
            logger.exception("后台任务 %s 执行失败", label)
            return None

    async def to_thread(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """在线程池中执行阻塞调用（云厂商 SDK / requests）。"""
        return await asyncio.to_thread(func, *args, **kwargs)

    async def shutdown(self, timeout: float = 30.0) -> None:
        """等待在跑的任务收尾（优雅退出）。"""
        pending = [task for task in self._tasks if not task.done()]
        if not pending:
            return
        logger.info("等待 %d 个后台任务收尾（最多 %.0fs）…", len(pending), timeout)
        try:
            await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=timeout)
        except TimeoutError:  # pragma: no cover
            logger.warning("后台任务未在 %.0fs 内结束，强制取消", timeout)
            for task in pending:
                task.cancel()
