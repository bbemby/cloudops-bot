"""端到端链路测试：指令 → 权限 → 云端（Mock Cloud）→ 消息回推。

对应论文第 5.4 节的测试用例：

* TC-01 多平台实例与公网 IP 汇总；
* TC-02 创建实例并异步推送公网 IP；
* TC-03 按 IP 销毁实例（二次确认）；
* TC-04 未授权用户拦截。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from support import (  # noqa: E402 - 由 tests 目录直接运行
    ADMIN_ID,
    GUEST_ID,
    READONLY_ID,
    AsyncTestCase,
    Harness,
    extract_code,
    make_settings,
    read_mock_state,
)


class EndToEndTest(AsyncTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.settings = make_settings(self.tmp_path)
        self.harness = Harness(self.settings)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ------------------------------------------------------------------ #
    async def _boot(self):
        harness = self.harness
        await harness.start()
        return harness

    # ------------------------------------------------------------------ #
    def test_full_lifecycle(self) -> None:
        async def scenario():
            harness = await self._boot()
            transport = harness.transport

            # 启动阶段：注册指令菜单 + 校验 Token
            self.assertTrue(transport.my_commands)
            default_menu = transport.my_commands[0]
            default_names = {item["command"] for item in default_menu["commands"]}
            self.assertIn("list", default_names)
            self.assertNotIn("start", default_names)   # hidden 指令不进菜单
            self.assertNotIn("create", default_names)  # 默认作用域不含写操作指令
            self.assertNotIn("scope", default_menu)

            # 管理员额外拿到 chat 作用域的完整菜单
            scoped = [item for item in transport.my_commands if "scope" in item]
            self.assertTrue(scoped)
            admin_names = {entry["command"] for entry in scoped[0]["commands"]}
            self.assertIn("create", admin_names)
            self.assertIn("bind", admin_names)
            self.assertEqual(scoped[0]["scope"], {"type": "chat", "chat_id": ADMIN_ID})

            # /help
            reply = await harness.send("/help")
            self.assertIn("CloudOps Bot", reply)
            self.assertIn("/create", reply)

            # /bind：绑定 Mock Cloud 凭证（delay=0 让创建后立即可用）
            reply = await harness.send("/bind mock demo token=demo-token delay=0")
            self.assertIn("凭证", reply)
            self.assertNotIn("demo-token", reply)  # 密钥脱敏

            # /creds
            reply = await harness.send("/creds")
            self.assertIn("mock", reply)

            # TC-01：此时还没有实例
            reply = await harness.send("/list")
            self.assertIn("没有任何实例", reply)

            # TC-02：创建实例并等待后台任务推送公网 IP
            reply = await harness.send(
                "/create mock name=web-01 region=mock-sgp1 size=mock-1vcpu-1gb")
            self.assertIn("web-01", reply)
            await harness.drain_jobs()
            pushed = harness.transport.transcript()
            self.assertIn("203.0.113.", pushed)

            state = read_mock_state(self.settings)
            self.assertEqual(len(state["instances"]), 1)

            # TC-01：/list_ip 汇总实例
            reply = await harness.send("/list_ip")
            instance_id = next(iter(state["instances"]))
            self.assertIn("203.0.113.", reply)
            self.assertIn("web-01", reply)

            # /status：按实例 ID 查询详情
            reply = await harness.send(f"/status {instance_id}")
            self.assertIn(instance_id, reply)

            # TC-03：按 IP 销毁，需二次确认
            ip = state["instances"][instance_id]["public_ip"]
            reply = await harness.send(f"/delete {ip}")
            self.assertIn("确认", reply)
            code = extract_code(reply)

            # 确认前实例仍然存在（危险操作不会在确认前执行）
            self.assertEqual(len(read_mock_state(self.settings)["instances"]), 1)

            reply = await harness.send(f"/confirm {code}")
            self.assertIn(instance_id, reply)
            self.assertEqual(read_mock_state(self.settings)["instances"], {})

            # /logs 留痕
            reply = await harness.send("/logs 20")
            self.assertIn("create", reply)
            self.assertIn("delete", reply)

            # /tasks 有历史记录
            reply = await harness.send("/tasks")
            self.assertIn("web-01", reply)

            await harness.stop()

        self.run_async(scenario())

    # ------------------------------------------------------------------ #
    def test_tc04_unauthorized_guest_is_blocked(self) -> None:
        async def scenario():
            harness = await self._boot()
            before = len(harness.transport.messages)

            reply = await harness.send("/list", user_id=GUEST_ID, username="stranger")
            self.assertIn("无权限", reply)
            reply = await harness.send("/create mock hacked", user_id=GUEST_ID, username="stranger")
            self.assertIn("无权限", reply)

            # 云端没有被触碰
            self.assertEqual(read_mock_state(self.settings)["instances"], {})

            # 审计日志记下两次拒绝
            logs = harness.app.db.list_logs(10)
            denied = [row for row in logs if row.status == "denied"]
            self.assertEqual(len(denied), 2)
            self.assertNotEqual(len(harness.transport.messages), before)
            await harness.stop()

        self.run_async(scenario())

    # ------------------------------------------------------------------ #
    def test_readonly_can_query_but_not_operate(self) -> None:
        async def scenario():
            harness = await self._boot()
            harness.settings.readonly_ids = (READONLY_ID,)

            reply = await harness.send("/bind mock demo token=demo-token delay=0",
                                       user_id=ADMIN_ID)
            self.assertIn("凭证", reply)

            reply = await harness.send("/list", user_id=READONLY_ID, username="watcher")
            self.assertNotIn("无权限", reply)

            reply = await harness.send("/create mock nope", user_id=READONLY_ID, username="watcher")
            self.assertIn("管理员", reply)
            self.assertEqual(read_mock_state(self.settings)["instances"], {})
            await harness.stop()

        self.run_async(scenario())

    # ------------------------------------------------------------------ #
    def test_inline_button_confirmation(self) -> None:
        async def scenario():
            harness = await self._boot()
            await harness.send("/bind mock demo token=demo-token delay=0")
            await harness.send("/create mock btn-01")
            await harness.drain_jobs()
            state = read_mock_state(self.settings)
            instance_id = next(iter(state["instances"]))

            reply = await harness.send(f"/delete {instance_id}")
            code = extract_code(reply)
            await harness.click(f"confirm:{code}")

            self.assertTrue(harness.transport.callback_answers)
            self.assertEqual(read_mock_state(self.settings)["instances"], {})
            await harness.stop()

        self.run_async(scenario())

    # ------------------------------------------------------------------ #
    def test_cancel_keeps_instance(self) -> None:
        async def scenario():
            harness = await self._boot()
            await harness.send("/bind mock demo token=demo-token delay=0")
            await harness.send("/create mock keep-01")
            await harness.drain_jobs()
            state = read_mock_state(self.settings)
            instance_id = next(iter(state["instances"]))

            reply = await harness.send(f"/delete {instance_id}")
            code = extract_code(reply)
            await harness.send(f"/cancel {code}")

            self.assertEqual(len(read_mock_state(self.settings)["instances"]), 1)

            # 过期/未知确认码会被拒绝
            reply = await harness.send("/confirm ZZZZZZ")
            self.assertTrue("失败" in reply or "无效" in reply or "过期" in reply)
            await harness.stop()

        self.run_async(scenario())

    # ------------------------------------------------------------------ #
    def test_create_positional_conflict_is_reported(self) -> None:
        """位置参数与 key=value 冲突时显式报错，而不是悄悄丢弃用户输入。"""

        async def scenario():
            harness = await self._boot()
            await harness.send("/bind mock demo token=demo-token delay=0")
            reply = await harness.send("/create mock web-01 region=mock-fra1")
            self.assertNotIn("任务已下发", reply)
            self.assertIn("region", reply)
            self.assertEqual(read_mock_state(self.settings)["instances"], {})
            await harness.stop()

        self.run_async(scenario())

    # ------------------------------------------------------------------ #
    def test_unknown_command_suggests_alias(self) -> None:
        async def scenario():
            harness = await self._boot()
            reply = await harness.send("/lst")
            self.assertIn("/list", reply)
            await harness.stop()

        self.run_async(scenario())

    # ------------------------------------------------------------------ #
    def test_rate_limit_blocks_burst(self) -> None:
        async def scenario():
            self.settings.rate_limit_per_minute = 2
            harness = Harness(self.settings)
            await harness.start()
            await harness.send("/ping")
            await harness.send("/ping")
            reply = await harness.send("/ping")
            self.assertIn("频繁", reply)
            await harness.stop()

        self.run_async(scenario())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
