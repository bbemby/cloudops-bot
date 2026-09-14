"""指令路由、权限矩阵、频率限制、异常渲染的单元测试。"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from support import (  # noqa: E402
    ADMIN_ID,
    GUEST_ID,
    READONLY_ID,
    AsyncTestCase,
    Harness,
    make_settings,
    read_mock_state,
)

from cloudops.bot.dispatcher import Dispatcher
from cloudops.bot.ratelimit import RateLimiter
from cloudops.bot.telegram import TelegramUserInfo
from cloudops.errors import ValidationError
from cloudops.i18n import get_translator
from cloudops.models import ROLE_ADMIN, ROLE_GUEST, ROLE_READONLY


class ParseTextTest(unittest.TestCase):
    def test_simple_command(self) -> None:
        self.assertEqual(Dispatcher.parse_text("/list"), ("list", []))

    def test_command_with_bot_suffix(self) -> None:
        self.assertEqual(Dispatcher.parse_text("/create@cloudops_bot do sgp1"),
                         ("create", ["do", "sgp1"]))

    def test_quoted_and_spaced_arguments(self) -> None:
        self.assertEqual(Dispatcher.parse_text('/create do name="web 01"'),
                         ("create", ["do", "name=web 01"]))

    def test_extra_whitespace_is_ignored(self) -> None:
        self.assertEqual(Dispatcher.parse_text("   /list    \n"), ("list", []))

    def test_non_command_text(self) -> None:
        self.assertEqual(Dispatcher.parse_text("hello"), ("hello", []))

    def test_unbalanced_quotes_fall_back_to_split(self) -> None:
        name, args = Dispatcher.parse_text('/create do "unbalanced')
        self.assertEqual(name, "create")
        self.assertEqual(args, ["do", "unbalanced"])


class RateLimiterTest(unittest.TestCase):
    def test_disabled_when_zero(self) -> None:
        limiter = RateLimiter(0)
        for _ in range(50):
            self.assertIsNone(limiter.check(1))

    def test_burst_then_block(self) -> None:
        limiter = RateLimiter(3)
        self.assertIsNone(limiter.check(1))
        self.assertIsNone(limiter.check(1))
        self.assertIsNone(limiter.check(1))
        retry_after = limiter.check(1)
        self.assertIsNotNone(retry_after)
        self.assertGreater(retry_after, 0)
        # 不同用户互不影响
        self.assertIsNone(limiter.check(2))

    def test_window_slides(self) -> None:
        limiter = RateLimiter(1, window=0.05)
        self.assertIsNone(limiter.check(1))
        self.assertIsNotNone(limiter.check(1))
        time.sleep(0.06)                      # 让窗口自然滑过，而不是去戳私有属性
        self.assertIsNone(limiter.check(1))

    def test_reset(self) -> None:
        limiter = RateLimiter(1)
        limiter.check(1)
        limiter.reset(1)
        self.assertIsNone(limiter.check(1))


class ErrorRenderTest(unittest.TestCase):
    def test_validation_error_renders_usage(self) -> None:
        from cloudops.bot.formatter import error_text

        t = get_translator("zh")
        exc = ValidationError(params={"value": "moon1", "parameter": "区域",
                                     "options": "sgp1, fra1"})
        text = error_text(exc, t)
        self.assertIn("moon1", text)
        self.assertNotEqual(text.strip(), "")

    def test_unknown_key_degrades_gracefully(self) -> None:
        from cloudops.bot.formatter import error_text
        from cloudops.errors import CloudOpsError

        t = get_translator("en")
        text = error_text(CloudOpsError("boom", key="not.a.real.key"), t)
        self.assertTrue(text)
        self.assertIsInstance(text, str)


class DispatcherIntegrationTest(AsyncTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.settings = make_settings(Path(self._tmp.name))
        self.harness = Harness(self.settings)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ------------------------------------------------------------------ #
    def test_role_resolution_prefers_env_whitelist(self) -> None:
        async def scenario():
            harness = await self.harness.start()
            dispatcher = harness.app.dispatcher

            admin = dispatcher.resolve_user(TelegramUserInfo(id=ADMIN_ID, username="boss"))
            self.assertEqual(admin.role, ROLE_ADMIN)
            readonly = dispatcher.resolve_user(TelegramUserInfo(id=READONLY_ID, username="watcher"))
            self.assertEqual(readonly.role, ROLE_READONLY)
            stranger = dispatcher.resolve_user(TelegramUserInfo(id=4242, username="newbie"))
            self.assertEqual(stranger.role, ROLE_GUEST)

            # 数据库改角色后（白名单之外的用户）立刻生效
            harness.app.db.set_role(4242, ROLE_READONLY)
            dispatcher._user_cache.clear()
            again = dispatcher.resolve_user(TelegramUserInfo(id=4242, username="newbie"))
            self.assertEqual(again.role, ROLE_READONLY)
            await harness.stop()

        self.run_async(scenario())

    def test_whitelist_admin_always_wins_over_db(self) -> None:
        async def scenario():
            harness = await self.harness.start()
            harness.app.db.ensure_user(ADMIN_ID, "boss", role=ROLE_GUEST)
            harness.app.dispatcher._user_cache.clear()
            user = harness.app.dispatcher.resolve_user(
                TelegramUserInfo(id=ADMIN_ID, username="boss"))
            self.assertEqual(user.role, ROLE_ADMIN)
            await harness.stop()

        self.run_async(scenario())

    def test_bot_own_messages_are_ignored(self) -> None:
        async def scenario():
            harness = await self.harness.start()
            before = len(harness.transport.messages)
            await harness.app.dispatcher.handle_update({
                "update_id": 1,
                "message": {"message_id": 1, "from": {"id": 1, "is_bot": True},
                            "chat": {"id": 1, "type": "private"}, "text": "/list"},
            })
            self.assertEqual(len(harness.transport.messages), before)
            await harness.stop()

        self.run_async(scenario())

    def test_typing_action_is_sent(self) -> None:
        async def scenario():
            harness = await self.harness.start()
            await harness.send("/ping")
            self.assertTrue(harness.transport.actions)
            self.assertEqual(harness.transport.actions[-1]["action"], "typing")
            await harness.stop()

        self.run_async(scenario())

    def test_group_chat_small_talk_is_silent(self) -> None:
        async def scenario():
            harness = await self.harness.start()
            before = len(harness.transport.messages)
            await harness.send("大家早", chat_type="group")
            self.assertEqual(len(harness.transport.messages), before)
            # 私聊里同样的话会给一句引导
            await harness.send("你好")
            self.assertEqual(len(harness.transport.messages), before + 1)
            await harness.stop()

        self.run_async(scenario())

    def test_pending_confirmation_hint_on_plain_message(self) -> None:
        async def scenario():
            harness = await self.harness.start()
            await harness.send("/bind mock demo token=demo-token delay=0")
            await harness.send("/create mock hint-01")
            await harness.drain_jobs()
            instance_id = next(iter(read_mock_state(self.settings)["instances"]))

            await harness.send(f"/delete {instance_id}")
            harness.transport.clear()
            await harness.send("hello?")
            self.assertIn("确认", harness.transport.last_text())
            await harness.stop()

        self.run_async(scenario())

    def test_message_from_another_bot_is_ignored(self) -> None:
        """别的 Bot 发的消息不能触发我们的回复（否则两个 Bot 会互刷成环）。"""

        async def scenario():
            harness = await self.harness.start()
            await harness.send("/list", user_id=ADMIN_ID, username="other_bot", is_bot=True)
            self.assertEqual(harness.transport.messages, [])
            self.assertEqual(harness.app.db.list_logs(5), [])
            await harness.stop()

        self.run_async(scenario())

    def test_unknown_command_is_logged(self) -> None:
        async def scenario():
            harness = await self.harness.start()
            await harness.send("/nonsense")
            logs = harness.app.db.list_logs(5)
            self.assertEqual(logs[0].status, "denied")
            self.assertIn("nonsense", logs[0].action)
            await harness.stop()

        self.run_async(scenario())

    def test_denied_attempt_notifies_admins(self) -> None:
        async def scenario():
            harness = await self.harness.start()
            harness.settings.notify_admins_on_denied = True
            harness.transport.clear()
            await harness.send("/list", user_id=GUEST_ID, username="stranger")
            notified = [call for call in harness.transport.method_calls("sendMessage")
                        if call["chat_id"] == ADMIN_ID]
            self.assertTrue(notified)
            self.assertIn("越权", notified[0]["text"])
            await harness.stop()

        self.run_async(scenario())

    def test_unknown_callback_is_answered_gracefully(self) -> None:
        async def scenario():
            harness = await self.harness.start()
            await harness.click("confirm:ZZZZZZ")
            self.assertTrue(harness.transport.callback_answers)
            self.assertTrue(harness.transport.last_text())
            await harness.stop()

        self.run_async(scenario())

    def test_specs_and_aliases(self) -> None:
        async def scenario():
            harness = await self.harness.start()
            dispatcher = harness.app.dispatcher
            self.assertIsNotNone(dispatcher.find("list_ip"))
            self.assertIs(dispatcher.find("list_ip"), dispatcher.find("list"))
            self.assertIsNotNone(dispatcher.find("rm"))
            self.assertIsNotNone(dispatcher.find("credentials"))
            # /start 必须可调用（用户进会话点到的第一条就是它），只是不出现在菜单里
            self.assertIsNotNone(dispatcher.find("start"))
            self.assertTrue(dispatcher.find("start").hidden)
            names = set(dispatcher.spec_names())
            self.assertIn("create", names)
            # 菜单注册在 app 层按 hidden 过滤（见 test_e2e 的菜单断言）；
            # spec_names() 供拼写建议用，故意包含隐藏指令
            self.assertIn("start", names)
            await harness.stop()

        self.run_async(scenario())


def _mock_state(settings):
    import json

    return json.loads(Path(settings.mock_state_path).read_text(encoding="utf-8"))

if __name__ == "__main__":  # pragma: no cover
    unittest.main()
