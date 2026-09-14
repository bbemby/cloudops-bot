"""Telegram 客户端传输层行为测试：连接清理、重试边界、长消息切分。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from support import AsyncTestCase, FakeTelegramTransport, make_settings

from cloudops.bot.telegram import (
    TELEGRAM_MESSAGE_LIMIT,
    TelegramClient,
    TelegramError,
)


async def _no_sleep(_seconds: float) -> None:
    """``asyncio.sleep`` 的空实现：重试用例不必真的等 1s / 2s。"""
    return None


class ClientTestCase(AsyncTestCase):
    """共享一份指向临时目录的配置，避免读真实 .env。"""
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = make_settings(Path(self._tmp.name))
        self.settings.telegram.retries = 2

    def make_client(self, transport: FakeTelegramTransport) -> TelegramClient:
        return TelegramClient(self.settings, transport=transport)


class StartUpTest(ClientTestCase):
    def test_start_records_bot_identity(self) -> None:
        async def scenario():
            transport = FakeTelegramTransport(username="cloudops_bot")
            client = self.make_client(transport)
            me = await client.start()
            self.assertEqual(me["username"], "cloudops_bot")
            self.assertEqual(client.username, "cloudops_bot")
            self.assertEqual(client.bot_id, 424242)
            self.assertFalse(transport.closed)      # 正常启动不应关闭连接

        self.run_async(scenario())

    def test_fatal_start_error_closes_transport(self) -> None:
        """Token 写错时不能把 HTTP 会话漏在后台（否则退出时打印 Unclosed client session）。"""

        async def scenario():
            transport = FakeTelegramTransport()
            transport.raise_on["getMe"] = 99
            transport.failures["getMe"] = TelegramError("getMe", "Unauthorized", error_code=401)
            client = self.make_client(transport)
            with self.assertRaises(TelegramError):
                await client.start()
            self.assertTrue(transport.closed)
            self.assertEqual(len(transport.method_calls("getMe")), 1)   # 401 不重试

        self.run_async(scenario())


class RetryTest(ClientTestCase):
    """重试逻辑：不真的睡（把 ``asyncio.sleep`` 换成空实现），否则测试白等好几秒。"""

    def setUp(self) -> None:
        super().setUp()
        from unittest import mock

        patcher = mock.patch("cloudops.bot.telegram.asyncio.sleep", new=_no_sleep)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_transient_error_is_retried_then_succeeds(self) -> None:
        async def scenario():
            transport = FakeTelegramTransport(username="retry_bot")
            transport.raise_on["getMe"] = 1          # 第一次 502，第二次成功
            client = self.make_client(transport)
            me = await client.start()
            self.assertEqual(me["username"], "retry_bot")
            self.assertEqual(len(transport.method_calls("getMe")), 2)
            self.assertFalse(transport.closed)

        self.run_async(scenario())

    def test_retries_are_bounded(self) -> None:
        async def scenario():
            transport = FakeTelegramTransport()
            transport.raise_on["sendMessage"] = 99
            transport.failures["sendMessage"] = TelegramError("sendMessage", "Bad Gateway",
                                                              error_code=502)
            client = self.make_client(transport)
            with self.assertRaises(TelegramError):
                await client.send_message(123, "hello")
            self.assertEqual(len(transport.method_calls("sendMessage")),
                             self.settings.telegram.retries + 1)

        self.run_async(scenario())


class SendMessageTest(ClientTestCase):
    def test_long_message_is_split_under_limit(self) -> None:
        async def scenario():
            transport = FakeTelegramTransport()
            client = self.make_client(transport)
            body = "\n".join(f"line {index:04d} " + "x" * 60 for index in range(200))
            await client.send_message(123, body)
            texts = transport.texts
            self.assertGreater(len(texts), 1)
            for text in texts:
                self.assertLessEqual(len(text), TELEGRAM_MESSAGE_LIMIT)
            joined = "\n".join(texts)
            for index in (0, 100, 199):              # 内容不能丢
                self.assertIn(f"line {index:04d}", joined)

        self.run_async(scenario())

    def test_reply_markup_goes_on_last_chunk(self) -> None:
        async def scenario():
            transport = FakeTelegramTransport()
            client = self.make_client(transport)
            body = "\n".join("y" * 100 for _ in range(120))
            await client.send_message(123, body, reply_markup={"inline_keyboard": []})
            payloads = transport.method_calls("sendMessage")
            self.assertGreater(len(payloads), 1)
            self.assertNotIn("reply_markup", payloads[0])
            self.assertIn("reply_markup", payloads[-1])

        self.run_async(scenario())

    def test_reply_to_goes_on_first_chunk(self) -> None:
        async def scenario():
            transport = FakeTelegramTransport()
            client = self.make_client(transport)
            await client.send_message(123, "hi", reply_to=77)
            payload = transport.method_calls("sendMessage")[0]
            self.assertEqual(payload["reply_to_message_id"], 77)
            self.assertTrue(payload["allow_sending_without_reply"])

        self.run_async(scenario())

    def test_typing_action_can_be_disabled(self) -> None:
        async def scenario():
            transport = FakeTelegramTransport()
            client = self.make_client(transport)
            await client.send_chat_action(123)
            self.assertEqual(len(transport.method_calls("sendChatAction")), 1)
            self.settings.telegram.show_typing = False
            await client.send_chat_action(123)
            self.assertEqual(len(transport.method_calls("sendChatAction")), 1)

        self.run_async(scenario())


class CommandMenuTest(ClientTestCase):
    def test_scope_is_forwarded(self) -> None:
        async def scenario():
            transport = FakeTelegramTransport()
            client = self.make_client(transport)
            await client.set_my_commands([{"command": "list", "description": "x"}],
                                         scope={"type": "chat", "chat_id": 42})
            payload = transport.method_calls("setMyCommands")[0]
            self.assertEqual(payload["scope"], {"type": "chat", "chat_id": 42})
            self.assertEqual(payload["commands"][0]["command"], "list")

        self.run_async(scenario())

    def test_empty_menu_is_not_sent(self) -> None:
        async def scenario():
            transport = FakeTelegramTransport()
            client = self.make_client(transport)
            await client.set_my_commands([])
            self.assertEqual(transport.method_calls("setMyCommands"), [])

        self.run_async(scenario())


class ParseUpdateTest(ClientTestCase):
    def test_edited_message_is_accepted(self) -> None:
        raw = {"update_id": 3,
               "edited_message": {"message_id": 7, "date": 1,
                                  "chat": {"id": 5, "type": "private"},
                                  "from": {"id": 9, "is_bot": False, "first_name": "A"},
                                  "text": "/list"}}
        message = TelegramClient.parse_message(raw)
        self.assertIsNotNone(message)
        self.assertEqual(message.text, "/list")
        self.assertEqual(message.chat_id, 5)
        self.assertEqual(message.user.id, 9)

    def test_non_message_update_is_ignored(self) -> None:
        self.assertIsNone(TelegramClient.parse_message({"update_id": 1}))

    def test_bot_authored_message_is_flagged(self) -> None:
        """解析层只负责标记 ``is_bot``，拦截由 dispatcher 完成（避免两个 Bot 互相刷屏）。"""
        raw = {"message": {"message_id": 7, "date": 1, "chat": {"id": 5, "type": "private"},
                           "from": {"id": 9, "is_bot": True}, "text": "/list"}}
        message = TelegramClient.parse_message(raw)
        self.assertIsNotNone(message)
        self.assertTrue(message.user.is_bot)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
