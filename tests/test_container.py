"""容器化部署相关行为的回归测试。

覆盖三类"只在容器/长期运行场景才暴露"的问题：

* **密钥指纹**：SECRET_KEY 从环境变量注入、数据库在数据卷里，两者不同步时
  必须启动即失败，而不是等到用户调用云凭证时才报"解密失败"；
* **库级元信息**：``schema_meta`` 的读写（指纹就寄存在这里）；
* **长轮询空转**：对端（自建网关/反代/桩）不按 poll_timeout 挂起连接时，
  主循环不能零间隔空转，否则会把 Bot 打到 Telegram 限流。
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from support import (  # noqa: E402 - 由 tests 目录直接运行
    ADMIN_ID,
    AsyncTestCase,
    FakeTelegramTransport,
    Harness,
    make_settings,
)

from cloudops.app import SECRET_FINGERPRINT_META
from cloudops.crypto import generate_key
from cloudops.errors import ConfigError


class MetaStoreTest(AsyncTestCase):
    """Database 的库级元信息读写。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.settings = make_settings(self.tmp_path)
        self.harness = Harness(self.settings)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_meta_roundtrip_and_overwrite(self) -> None:
        async def scenario():
            await self.harness.start()
            db = self.harness.app.db

            self.assertIsNone(db.get_meta("nope"))
            db.set_meta("k", "v1")
            self.assertEqual(db.get_meta("k"), "v1")
            db.set_meta("k", "v2")           # 覆盖而不是主键冲突
            self.assertEqual(db.get_meta("k"), "v2")
            await self.harness.stop()

        self.run_async(scenario())

    def test_secret_fingerprint_recorded_on_first_boot(self) -> None:
        async def scenario():
            await self.harness.start()
            db = self.harness.app.db
            stored = db.get_meta(SECRET_FINGERPRINT_META)
            self.assertIsNotNone(stored)
            self.assertEqual(stored, self.harness.app.box.fingerprint)
            self.assertNotIn(self.settings.secret_key, stored or "")  # 不是明文
            await self.harness.stop()

        self.run_async(scenario())


class SecretKeyGuardTest(AsyncTestCase):
    """SECRET_KEY 与卷中凭证不匹配时必须 fail fast。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_boot_fails_when_key_changed_with_credentials(self) -> None:
        async def scenario():
            # 第一次启动：用密钥 A 写入一条凭证
            settings = make_settings(self.tmp_path)
            first = Harness(settings)
            await first.start()
            first.app.store.bind(ADMIN_ID, "mock", "default", {"token": "secret-token"})
            self.assertEqual(first.app.db.count_credentials(), 1)
            await first.stop()

            # 第二次启动：密钥换了（等价于用户重建容器时改了 .env）
            rotated = make_settings(self.tmp_path, secret_key=generate_key())
            second = Harness(rotated)
            with self.assertRaises(ConfigError) as ctx:
                await second.start()
            message = str(ctx.exception)
            self.assertIn("SECRET_KEY", message)
            self.assertIn("1", message)              # 提示受影响凭证数
            self.assertIn("重新 /bind", message)      # 给出可执行修复方式
            self.assertEqual(ctx.exception.key, "error.secret_key_mismatch")

        self.run_async(scenario())

    def test_boot_recovers_when_key_changed_without_credentials(self) -> None:
        """库里没有凭证时换密钥无损失，应自动接受新指纹而不是拦下启动。"""
        async def scenario():
            settings = make_settings(self.tmp_path)
            first = Harness(settings)
            await first.start()
            await first.stop()

            rotated = make_settings(self.tmp_path, secret_key=generate_key())
            second = Harness(rotated)
            await second.start()                      # 不应抛异常
            self.assertEqual(second.app.db.get_meta(SECRET_FINGERPRINT_META),
                             second.app.box.fingerprint)
            await second.stop()

        self.run_async(scenario())

    def test_i18n_message_has_no_placeholder_left(self) -> None:
        """指纹不匹配的文案在中英两种语言下都不能漏 {count} 占位符。"""
        from cloudops.i18n import get_translator

        for language in ("zh", "en"):
            rendered = get_translator(language)("error.secret_key_mismatch", count=3)
            self.assertNotIn("{count}", rendered, rendered)
            self.assertIn("3", rendered, rendered)
            self.assertIn("SECRET_KEY", rendered, rendered)


class PollLoopPacingTest(AsyncTestCase):
    """对端不挂起连接时，主循环必须有间隔，且仍能及时响应停机信号。"""

    def test_empty_fast_response_does_not_busy_loop(self) -> None:
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                settings = make_settings(Path(tmp))
                transport = FakeTelegramTransport()
                transport.first_poll_empty = True     # 让"丢弃积压更新"那步返回空
                harness = Harness(settings, transport)
                app = harness.app

                import asyncio

                task = asyncio.ensure_future(app.run())
                await asyncio.sleep(1.6)              # 观察窗口

                polls = len(transport.method_calls("getUpdates"))
                # 没有防护的话这里会是数百次；有了间隔应是个位数
                self.assertLessEqual(polls, 6, f"主循环疑似空转：{polls} 次 getUpdates")

                started = time.monotonic()
                app.request_stop()                    # 等价 docker stop 的 SIGTERM
                await asyncio.wait_for(task, timeout=3.0)
                self.assertLess(time.monotonic() - started, 2.0, "停机响应过慢")
                await app.shutdown()

        self.run_async(scenario())


if __name__ == "__main__":
    unittest.main()
