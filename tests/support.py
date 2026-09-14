"""测试支撑层：离线 Telegram 记录桩 + 临时应用装配助手。

设计目标（论文第 6 章测试方案）：

* **完全离线**：不联网也能覆盖"指令 → 云端 API → 回推消息"全链路；
* **可断言**：所有出站消息、API 调用都被记录下来，便于校验权限拦截、
  确认码、脱敏展示等行为；
* **真实装配**：走的是 :class:`cloudops.app.Application` 与真实 SQLite，
  不是把处理器单独拿出来单测，因此能发现"层与层之间"的接缝问题。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # 允许 `python -m unittest` 直接跑
    sys.path.insert(0, str(ROOT))

from cloudops.app import Application
from cloudops.bot.telegram import TelegramClient, TelegramError
from cloudops.config import Settings, TelegramSettings, WebSettings
from cloudops.crypto import generate_key

#: 测试用固定身份
ADMIN_ID = 7001
READONLY_ID = 7002
GUEST_ID = 7003


class FakeTelegramTransport:
    """记录每一次 Bot API 调用，并按需返回预设结果的内存传输层。"""

    def __init__(self, *, username: str = "cloudops_test_bot") -> None:
        self.calls: List[Dict[str, Any]] = []
        self.messages: List[Dict[str, Any]] = []          # sendMessage 的 payload
        self.edits: List[Dict[str, Any]] = []
        self.actions: List[Dict[str, Any]] = []
        self.callback_answers: List[Dict[str, Any]] = []
        self.my_commands: List[Dict[str, Any]] = []
        self.updates: List[Dict[str, Any]] = []
        self.failures: Dict[str, Exception] = {}
        self.raise_on: Dict[str, int] = {}                # method -> 还需失败几次
        self.first_poll_empty = False                     # 供 run() 的"清空积压"用例
        self.closed = False
        self.me = {"id": 424242, "username": username, "first_name": "CloudOps", "is_bot": True}

    # -- 传输层协议 ------------------------------------------------------ #
    async def request(self, method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append({"method": method, "payload": payload})

        if self.raise_on.get(method, 0) > 0:
            self.raise_on[method] -= 1
            raise self.failures.get(method) or TelegramError(method, "injected", error_code=502)

        if method == "getMe":
            return {"ok": True, "result": dict(self.me)}
        if method == "getUpdates":
            if self.first_poll_empty:
                self.first_poll_empty = False
                return {"ok": True, "result": []}
            pending, self.updates = list(self.updates), []
            return {"ok": True, "result": pending}
        if method == "sendMessage":
            self.messages.append(payload)
            return {"ok": True, "result": {"message_id": 9000 + len(self.messages),
                                           "chat": {"id": payload.get("chat_id")}}}
        if method == "editMessageText":
            self.edits.append(payload)
            return {"ok": True, "result": True}
        if method == "sendChatAction":
            self.actions.append(payload)
            return {"ok": True, "result": True}
        if method == "answerCallbackQuery":
            self.callback_answers.append(payload)
            return {"ok": True, "result": True}
        if method == "setMyCommands":
            self.my_commands.append(payload)
            return {"ok": True, "result": True}
        return {"ok": True, "result": True}

    async def close(self) -> None:
        self.closed = True

    # -- 断言助手 -------------------------------------------------------- #
    @property
    def texts(self) -> List[str]:
        return [message["text"] for message in self.messages]

    def last_text(self) -> str:
        return self.messages[-1]["text"] if self.messages else ""

    def transcript(self, since: int = 0) -> str:
        return "\n".join(self.texts[since:])

    def method_calls(self, method: str) -> List[Dict[str, Any]]:
        return [call["payload"] for call in self.calls if call["method"] == method]

    def clear(self) -> None:
        self.calls.clear()
        self.messages.clear()
        self.edits.clear()
        self.actions.clear()
        self.callback_answers.clear()


# --------------------------------------------------------------------------- #
# 更新构造
# --------------------------------------------------------------------------- #
def message_update(text: str, *, user_id: int = ADMIN_ID, chat_id: Optional[int] = None,
                   username: str = "admin", message_id: int = 1,
                   chat_type: str = "private", update_id: int = 1,
                   is_bot: bool = False) -> Dict[str, Any]:
    """构造一条 Telegram message 更新。"""
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "from": {"id": user_id, "username": username, "is_bot": is_bot},
            "chat": {"id": chat_id if chat_id is not None else user_id, "type": chat_type},
            "text": text,
        },
    }


def callback_update(data: str, *, user_id: int = ADMIN_ID, chat_id: Optional[int] = None,
                    message_id: int = 1, update_id: int = 2) -> Dict[str, Any]:
    """构造一条内联按钮回调更新。"""
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb-{update_id}",
            "from": {"id": user_id, "username": "admin", "is_bot": False},
            "message": {
                "message_id": message_id,
                "chat": {"id": chat_id if chat_id is not None else user_id, "type": "private"},
            },
            "data": data,
        },
    }


# --------------------------------------------------------------------------- #
# 应用装配
# --------------------------------------------------------------------------- #
def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    """构造一份指向临时目录的完整配置（不读取真实 .env / 环境变量）。"""
    settings = Settings(
        telegram=TelegramSettings(token="12345:TEST-TOKEN", retries=0, poll_timeout=1,
                                  show_typing=True),
        admin_ids=(ADMIN_ID,),
        readonly_ids=(READONLY_ID,),
        secret_key=generate_key(),
        database_path=tmp_path / "data" / "cloudops.db",
        mock_state_path=tmp_path / "data" / "mock-cloud.json",
        default_provider="mock",
        enable_mock_provider=True,
        banner=False,
        log_level="CRITICAL",
        create_poll_interval=2.0,
        create_timeout_seconds=60,
        rate_limit_per_minute=0,          # 默认不限速，个别用例自行调小
        # Web 面板默认关掉：套件里几十个用例都会 setup()，逐个去抢 9878 端口
        # 只会带来互相干扰。需要面板的用例显式打开（端口用 0 让内核分配）。
        web=WebSettings(enabled=False),
    )
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


class Harness:
    """把 Application + 记录桩 + 便捷的"发消息"包装成一体。"""

    def __init__(self, settings: Settings, transport: Optional[FakeTelegramTransport] = None) -> None:
        self.settings = settings
        self.transport = transport or FakeTelegramTransport()
        self.bot = TelegramClient(settings, transport=self.transport)
        self.app = Application(settings, bot=self.bot)

    # -- 生命周期 -------------------------------------------------------- #
    async def start(self) -> "Harness":
        await self.app.setup()
        return self

    async def stop(self) -> None:
        await self.app.shutdown()

    # -- 交互 ------------------------------------------------------------ #
    async def send(self, text: str, **kwargs: Any) -> str:
        """投递一条指令，返回机器人最后一条回复。"""
        await self.app.dispatcher.handle_update(message_update(text, **kwargs))
        return self.transport.last_text()

    async def click(self, data: str, **kwargs: Any) -> str:
        """模拟点击内联按钮。"""
        await self.app.dispatcher.handle_update(callback_update(data, **kwargs))
        return self.transport.last_text()

    async def drain_jobs(self) -> None:
        """等待所有后台任务（如创建实例）结束。"""
        await self.app.jobs.shutdown(timeout=30.0)

    # -- 便捷断言素材 ---------------------------------------------------- #
    def pending_code(self) -> Optional[str]:
        return self.app.confirms._codes_by_user.get(ADMIN_ID)  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# 异步用例基类
# --------------------------------------------------------------------------- #
class AsyncTestCase(unittest.TestCase):
    """在 unittest 里跑协程，保持零第三方依赖。"""

    def run_async(self, coro):
        import asyncio

        return asyncio.run(coro)


def extract_code(text: str) -> str:
    """从"确认码：XXXXXX"之类的文案里抠出确认码。"""
    import re

    match = re.search(r"\b([ACDEFGHJKLMNPQRTUVWXY3456789]{6})\b", text)
    if not match:
        raise AssertionError(f"文案中没有确认码：{text!r}")
    return match.group(1)


def read_mock_state(settings: Settings) -> Dict[str, Any]:
    path = Path(settings.mock_state_path)
    if not path.exists():
        return {"instances": {}}
    return json.loads(path.read_text(encoding="utf-8"))


__all__ = [
    "ADMIN_ID", "READONLY_ID", "GUEST_ID",
    "FakeTelegramTransport", "Harness", "AsyncTestCase",
    "make_settings", "message_update", "callback_update",
    "extract_code", "read_mock_state", "TelegramError",
]
