"""Web 管理面板的端到端测试。

覆盖范围与分工（论文第 6 章"测试"的延伸）：

* 起的是**真的 HTTP 服务**（随机端口 + 真 socket），不是直接调 handler，
  因此中间件顺序、cookie、CSP 头这些"接缝"问题都能被抓到；
* 数据面走**真的 SQLite 与 mock 云适配器**，断言的是"接口返回值 = 库里/云端
  真实状态"，而不是 mock 掉业务层的假结果；
* 安全相关的用例是重点：未登录、越权写、CSRF、只读模式、口令变更失效、
  登录限速、密钥不落响应。

默认口令为 ``TEST-PANEL-PASS``；需要"生成随机口令"的用例会显式清空配置。
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

from support import ADMIN_ID, AsyncTestCase, Harness, make_settings

from cloudops.cloud import CreateSpec
from cloudops.config import WebSettings
from cloudops.errors import ConfigError

#: 测试用固定口令（避免每次 hash 的随机盐导致断言不稳定）
PANEL_PASSWORD = "TEST-PANEL-PASS"

#: 从首页 <meta name="csrf-token"> 抠 CSRF token（与浏览器同一条路径）
CSRF_PATTERN = re.compile(r'name="csrf-token" content="([^"]+)"')


class PanelTestCase(AsyncTestCase):
    """提供一个已启动仪表盘的 Harbor：真实监听 + 真实数据库。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.harnesses = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def make_settings(self, **overrides):
        web = overrides.pop("web", None) or WebSettings(
            enabled=True, host="127.0.0.1", port=0, admin_password=PANEL_PASSWORD)
        return make_settings(self.root, web=web, **overrides)

    async def start_panel(self, **overrides):
        """启动应用（含面板），返回 (harness, base_url)。"""

        harness = Harness(self.make_settings(**overrides))
        self.harnesses.append(harness)
        await harness.start()
        panel = harness.app.web
        assert panel is not None and panel.running
        self.base_url = f"http://127.0.0.1:{panel.port_in_use}"
        return harness, self.base_url

    async def stop_all(self) -> None:
        for harness in self.harnesses:
            await harness.stop()
        self.harnesses.clear()

    # -- 客户端 ---------------------------------------------------------- #
    def client(self, **kwargs):
        """注意 ``CookieJar(unsafe=True)``：aiohttp 默认拒绝为 IP 字面量主机
        存 cookie（换成 localhost 才行），而测试要连 127.0.0.1 的真实端口，
        所以显式放开 —— 这是客户端库的策略，不是面板的问题。"""
        import aiohttp

        kwargs.setdefault("cookie_jar", aiohttp.CookieJar(unsafe=True))
        return aiohttp.ClientSession(**kwargs)

    def jar(self, client):
        """取会话 cookie 原始值（filter_cookies 需要 yarl.URL）。"""
        import yarl

        return client.cookie_jar.filter_cookies(yarl.URL(self.base_url))

    async def login(self, client, password: str = PANEL_PASSWORD):
        return await client.post(f"{self.base_url}/login", data={"password": password},
                                 allow_redirects=False)

    async def csrf(self, client) -> str:
        response = await client.get(f"{self.base_url}/")
        assert response.status == 200, await response.text()
        match = CSRF_PATTERN.search(await response.text())
        assert match, "首页没有下发 CSRF token"
        return match.group(1)

    async def api(self, client, path: str, *, method: str = "GET", body=None, csrf=None,
                  allow_redirects: bool = True):
        headers = {}
        if csrf is not None:
            headers["X-CSRF-Token"] = csrf
        kwargs = {"headers": headers, "allow_redirects": allow_redirects}
        if body is not None:
            kwargs["json"] = body
        return await client.request(method, f"{self.base_url}{path}", **kwargs)


class PanelConfigTest(PanelTestCase):
    """配置默认值与开关。"""

    def test_default_port_is_9878(self) -> None:
        self.assertEqual(WebSettings().port, 9878)
        self.assertTrue(WebSettings().enabled)

    def test_env_can_override_port_and_password(self) -> None:
        import os
        from unittest import mock

        from cloudops.config import Settings

        env = {
            "WEB_PORT": "8443",
            "WEB_ADMIN_PASSWORD": "from-env",
            "WEB_READONLY": "true",
            "WEB_HOST": "127.0.0.1",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            settings = Settings.from_env(None)
        self.assertEqual(settings.web.port, 8443)
        self.assertEqual(settings.web.admin_password, "from-env")
        self.assertTrue(settings.web.readonly)
        self.assertEqual(settings.web.host, "127.0.0.1")

    def test_password_is_redacted_in_log_snapshot(self) -> None:
        settings = self.make_settings(web=WebSettings(admin_password="super-secret"))
        self.assertEqual(settings.as_log_fields()["web_admin_password"], "***")

    def test_illegal_port_is_rejected(self) -> None:
        settings = self.make_settings(web=WebSettings(port=70000))
        with self.assertRaises(ConfigError):
            settings.validate()

    def test_disabled_panel_does_not_listen(self) -> None:
        async def scenario():
            harness = Harness(self.make_settings(web=WebSettings(enabled=False)))
            self.harnesses.append(harness)
            await harness.start()
            self.assertIsNone(harness.app.web)
            await harness.stop()

        self.run_async(scenario())

    def test_random_password_is_generated_and_logged(self) -> None:
        async def scenario():
            panel, _ = await self.start_panel(
                web=WebSettings(enabled=True, host="127.0.0.1", port=0, admin_password=""))
            generated = panel.app.web.generated_password
            self.assertTrue(generated and len(generated) >= 8)
            from cloudops.web.auth import verify_password

            self.assertTrue(verify_password(generated, panel.app.web.password_hash))
            await self.stop_all()

        with self.assertLogs("cloudops.web.server", level="WARNING") as captured:
            self.run_async(scenario())
        self.assertTrue(any("本次启动口令" in line for line in captured.output))


class PanelAuthTest(PanelTestCase):
    """认证、会话与登录限速。"""

    def test_unauthenticated_page_redirects_to_login(self) -> None:
        async def scenario():
            await self.start_panel()
            async with self.client() as client:
                response = await client.get(f"{self.base_url}/", allow_redirects=False)
                self.assertEqual(response.status, 302)
                self.assertEqual(response.headers["Location"], "/login")
                login_page = await client.get(f"{self.base_url}/login")
                self.assertEqual(login_page.status, 200)
                self.assertIn("password", await login_page.text())
            await self.stop_all()

        self.run_async(scenario())

    def test_health_is_public_and_carries_no_secrets(self) -> None:
        async def scenario():
            harness, _ = await self.start_panel()
            async with self.client() as client:
                response = await client.get(f"{self.base_url}/healthz")
                self.assertEqual(response.status, 200)
                payload = await response.json()
                self.assertEqual(payload["status"], "ok")
                text = json.dumps(payload)
                self.assertNotIn(str(harness.settings.database_path), text)
                self.assertNotIn(PANEL_PASSWORD, text)
                self.assertNotIn(harness.settings.secret_key, text)
            await self.stop_all()

        self.run_async(scenario())

    def test_api_requires_session(self) -> None:
        async def scenario():
            await self.start_panel()
            async with self.client() as client:
                response = await client.get(f"{self.base_url}/api/overview")
                self.assertEqual(response.status, 401)
                self.assertIn("error", await response.json())
            await self.stop_all()

        self.run_async(scenario())

    def test_successful_login_sets_hardened_cookie(self) -> None:
        async def scenario():
            await self.start_panel()
            async with self.client() as client:
                response = await self.login(client)
                self.assertEqual(response.status, 303)
                cookie = response.headers["Set-Cookie"]
                self.assertIn("HttpOnly", cookie)
                self.assertIn("SameSite=Lax", cookie)
                self.assertNotIn("Secure", cookie)          # 未配 HTTPS 时不该加
                overview = await self.api(client, "/api/overview")
                self.assertEqual(overview.status, 200)
            await self.stop_all()

        self.run_async(scenario())

    def test_secure_cookie_flag_is_configurable(self) -> None:
        async def scenario():
            await self.start_panel(web=WebSettings(enabled=True, host="127.0.0.1", port=0,
                                                   admin_password=PANEL_PASSWORD,
                                                   secure_cookie=True))
            async with self.client() as client:
                response = await self.login(client)
                self.assertIn("Secure", response.headers["Set-Cookie"])
            await self.stop_all()

        self.run_async(scenario())

    def test_wrong_password_returns_401_and_reveals_nothing(self) -> None:
        async def scenario():
            await self.start_panel()
            async with self.client() as client:
                response = await self.login(client, "wrong-pass")
                self.assertEqual(response.status, 401)
                text = await response.text()
                self.assertNotIn(PANEL_PASSWORD, text)
                self.assertNotIn("scrypt$", text)
            await self.stop_all()

        self.run_async(scenario())

    def test_login_is_throttled_after_repeated_failures(self) -> None:
        async def scenario():
            await self.start_panel(web=WebSettings(enabled=True, host="127.0.0.1", port=0,
                                                   admin_password=PANEL_PASSWORD,
                                                   max_login_attempts=3))
            async with self.client() as client:
                for _ in range(3):
                    self.assertEqual((await self.login(client, "nope")).status, 401)
                blocked = await self.login(client, "nope")
                self.assertEqual(blocked.status, 429)
                self.assertIn("Retry-After", blocked.headers)
                # 即使口令正确，限速生效期间也要拒绝
                self.assertEqual((await self.login(client, PANEL_PASSWORD)).status, 429)
            await self.stop_all()

        self.run_async(scenario())

    def test_tampered_cookie_is_rejected(self) -> None:
        async def scenario():
            await self.start_panel()
            async with self.client() as client:
                await self.login(client)
                raw = self.jar(client)["cloudops_session"].value
                tampered = raw[:-2] + ("aa" if not raw.endswith("aa") else "bb")
                async with self.client() as other:
                    other.cookie_jar.update_cookies({"cloudops_session": tampered})
                    response = await other.get(f"{self.base_url}/api/overview")
                    self.assertEqual(response.status, 401)
            await self.stop_all()

        self.run_async(scenario())

    def test_other_secret_key_cannot_forge_session(self) -> None:
        async def scenario():
            harness, _ = await self.start_panel()
            async with self.client() as client:
                await self.login(client)
                cookie = self.jar(client)["cloudops_session"].value
            # 用同一个口令、不同 SECRET_KEY 的另一套实例去校验这个 cookie
            other_root = Path(tempfile.mkdtemp(dir=self.root))
            other = Harness(make_settings(other_root,
                                          web=WebSettings(enabled=True, host="127.0.0.1",
                                                          port=0, admin_password=PANEL_PASSWORD)))
            self.harnesses.append(other)
            await other.start()
            panel = other.app.web
            self.assertIsNotNone(panel)
            self.assertIsNone(panel.sessions.verify(cookie, panel.password_hash))
            self.assertIsNotNone(harness.app.web.sessions.verify(
                cookie, harness.app.web.password_hash))
            await self.stop_all()

        self.run_async(scenario())

    def test_password_change_invalidates_old_sessions(self) -> None:
        async def scenario():
            harness, _ = await self.start_panel()
            async with self.client() as client:
                await self.login(client)
                cookie = self.jar(client)["cloudops_session"].value
            panel = harness.app.web
            panel.password_hash = ""
            panel.settings.web.admin_password = "ANOTHER-PASS"
            panel.prepare_password()
            self.assertIsNone(panel.sessions.verify(cookie, panel.password_hash))
            await self.stop_all()

        self.run_async(scenario())

    def test_logout_clears_cookie(self) -> None:
        async def scenario():
            await self.start_panel()
            async with self.client() as client:
                await self.login(client)
                token = await self.csrf(client)
                response = await self.api(client, "/logout", method="POST",
                                          body={"csrf_token": token}, csrf=token,
                                          allow_redirects=False)
                self.assertEqual(response.status, 303)
                self.assertIn("Max-Age=0", response.headers["Set-Cookie"])
            await self.stop_all()

        self.run_async(scenario())

    def test_logout_requires_csrf(self) -> None:
        async def scenario():
            await self.start_panel()
            async with self.client() as client:
                await self.login(client)
                response = await self.api(client, "/logout", method="POST",
                                          body={"csrf_token": "bogus"},
                                          allow_redirects=False)
                self.assertEqual(response.status, 403)
            await self.stop_all()

        self.run_async(scenario())


class PanelSecurityTest(PanelTestCase):
    """CSRF、只读模式、响应头。"""

    def test_write_without_csrf_is_forbidden(self) -> None:
        async def scenario():
            harness, _ = await self.start_panel()
            async with self.client() as client:
                await self.login(client)
                response = await self.api(client, f"/api/users/{ADMIN_ID}/role",
                                          method="POST", body={"role": "guest"})
                self.assertEqual(response.status, 403)
                self.assertEqual(harness.app.db.get_user(ADMIN_ID).role, "admin")
            await self.stop_all()

        self.run_async(scenario())

    def test_readonly_mode_blocks_writes_but_allows_reads(self) -> None:
        async def scenario():
            await self.start_panel(web=WebSettings(enabled=True, host="127.0.0.1", port=0,
                                                   admin_password=PANEL_PASSWORD, readonly=True))
            async with self.client() as client:
                await self.login(client)
                token = await self.csrf(client)
                self.assertEqual((await self.api(client, "/api/overview")).status, 200)
                blocked = await self.api(client, f"/api/users/{ADMIN_ID}/role", method="POST",
                                         body={"role": "guest"}, csrf=token)
                self.assertEqual(blocked.status, 403)
                self.assertIn("只读", await blocked.text())
                created = await self.api(client, "/api/credentials", method="POST",
                                         body={"provider": "mock", "label": "x",
                                               "fields": {"token": "t"}}, csrf=token)
                self.assertEqual(created.status, 403)
            await self.stop_all()

        self.run_async(scenario())

    def test_security_headers_on_pages_and_api(self) -> None:
        async def scenario():
            await self.start_panel()
            async with self.client() as client:
                page = await client.get(f"{self.base_url}/login")
                self.assertEqual(page.headers["X-Frame-Options"], "DENY")
                self.assertEqual(page.headers["X-Content-Type-Options"], "nosniff")
                self.assertIn("default-src 'self'", page.headers["Content-Security-Policy"])
                script = await client.get(f"{self.base_url}/static/app.js")
                self.assertEqual(script.status, 200)
                self.assertIn("nosniff", script.headers["X-Content-Type-Options"])
            await self.stop_all()

        self.run_async(scenario())

    def test_static_whitelist_blocks_traversal(self) -> None:
        async def scenario():
            await self.start_panel()
            async with self.client() as client:
                await self.login(client)
                # 用 %2e%2e 让".." 真的到达服务端（明文 ../ 会被 URL 规范化掉，
                # 那样测的其实是客户端而不是面板）
                traversal = await client.get(f"{self.base_url}/static/%2e%2e/auth.py")
                self.assertEqual(traversal.status, 404)
                self.assertNotIn("verify_password", await traversal.text())
                encoded = await client.get(f"{self.base_url}/static/%2e%2e%2fauth.py")
                self.assertIn(encoded.status, (400, 404))
                nested = await client.get(f"{self.base_url}/static/templates/index.html")
                self.assertEqual(nested.status, 404)
                # 白名单内的两个文件正常可取
                self.assertEqual((await client.get(f"{self.base_url}/static/app.css")).status, 200)
                self.assertEqual((await client.get(f"{self.base_url}/static/app.js")).status, 200)
            await self.stop_all()

        self.run_async(scenario())

    def test_bind_failure_is_reported_as_config_error(self) -> None:
        async def scenario():
            import socket

            probe = socket.socket()
            probe.bind(("127.0.0.1", 0))
            probe.listen(1)
            taken = probe.getsockname()[1]
            try:
                harness = Harness(self.make_settings(
                    web=WebSettings(enabled=True, host="127.0.0.1", port=taken,
                                    admin_password=PANEL_PASSWORD)))
                self.harnesses.append(harness)
                with self.assertRaises(ConfigError) as caught:
                    await harness.app.setup()
                self.assertIn("无法监听", str(caught.exception))
            finally:
                probe.close()
                await self.stop_all()

        self.run_async(scenario())


class PanelDataTest(PanelTestCase):
    """接口返回值必须与数据库/云端真实状态一致。"""

    def test_bootstrap_labels_are_translated(self) -> None:
        async def scenario():
            await self.start_panel()
            async with self.client() as client:
                await self.login(client)
                payload = await (await self.api(client, "/api/bootstrap")).json()
                self.assertTrue(payload["labels"])
                fallback = [key for key, value in payload["labels"].items() if value == key]
                self.assertEqual(fallback, [], "词表缺失，前端会显示成 key")
                names = [item["name"] for item in payload["providers"]]
                self.assertIn("mock", names)
                self.assertIn("digitalocean", names)
                fields = {f["name"] for item in payload["providers"] if item["name"] == "aws"
                          for f in item["fields"]}
                self.assertIn("access_key_id", fields)
                self.assertIn("secret_access_key", fields)
            await self.stop_all()

        self.run_async(scenario())

    def test_overview_matches_database(self) -> None:
        async def scenario():
            harness, _ = await self.start_panel()
            db = harness.app.db
            db.ensure_user(ADMIN_ID, "boss", role="admin")
            harness.app.store.bind(ADMIN_ID, "mock", "default", {"token": "demo"},
                                   shared=True, activate=True)
            db.log_operation(ADMIN_ID, "list", "success", provider="mock", target="all")
            async with self.client() as client:
                await self.login(client)
                payload = await (await self.api(client, "/api/overview")).json()
            self.assertEqual(payload["users"], db.count_users())
            self.assertEqual(payload["credentials"], db.count_credentials())
            self.assertEqual(payload["operations"], db.count_operations())
            self.assertIn("mock", payload["bound_providers"])
            self.assertEqual(payload["web"]["port"], 0)
            self.assertFalse(payload["readonly"])
            await self.stop_all()

        self.run_async(scenario())

    async def seed_instance(self, harness, name: str = "web-01"):
        """用真实的 mock 适配器建一台机器（走 cloud 层的创建路径）。"""
        provider = harness.app.store.provider("mock", ADMIN_ID)
        try:
            return await __import__("asyncio").to_thread(
                provider.create_instance, CreateSpec(name=name))
        finally:
            provider.close()

    def test_instances_are_aggregated_from_provider(self) -> None:
        async def scenario():
            harness, _ = await self.start_panel()
            harness.app.store.bind(ADMIN_ID, "mock", "default", {"token": "demo"},
                                   shared=True, activate=True)
            instance = await self.seed_instance(harness, "web-01")
            async with self.client() as client:
                await self.login(client)
                payload = await (await self.api(client, "/api/instances")).json()
            self.assertEqual(payload["errors"], [])
            self.assertEqual(payload["total"], 1)
            self.assertEqual(payload["instances"][0]["id"], instance.id)
            self.assertEqual(payload["instances"][0]["name"], "web-01")
            self.assertEqual(payload["by_status"], {"provisioning": 1})
            await self.stop_all()

        self.run_async(scenario())

    def test_instances_without_credentials_yields_empty_view(self) -> None:
        async def scenario():
            await self.start_panel()
            async with self.client() as client:
                await self.login(client)
                payload = await (await self.api(client, "/api/instances")).json()
            self.assertEqual(payload["instances"], [])
            self.assertEqual(payload["providers"], [])
            await self.stop_all()

        self.run_async(scenario())

    def test_unknown_provider_filter_is_a_400(self) -> None:
        async def scenario():
            await self.start_panel()
            async with self.client() as client:
                await self.login(client)
                response = await self.api(client, "/api/instances?provider=aliyun")
                self.assertEqual(response.status, 400)
                self.assertIn("error", await response.json())
            await self.stop_all()

        self.run_async(scenario())

    def test_instance_rows_expose_provider_capabilities(self) -> None:
        """实例行带上 actions，前端才不会长出适配器根本没实现的按钮。"""

        async def scenario():
            harness, _ = await self.start_panel()
            harness.app.store.bind(ADMIN_ID, "mock", "default", {"token": "demo"},
                                   shared=True, activate=True)
            await self.seed_instance(harness, "web-03")
            async with self.client() as client:
                await self.login(client)
                payload = await (await self.api(client, "/api/instances")).json()
            actions = payload["instances"][0]["actions"]
            self.assertIn("destroy", actions)
            self.assertNotIn("start", actions)
            await self.stop_all()

        self.run_async(scenario())

    def test_unsupported_action_is_rejected_by_service(self) -> None:
        """能力校验不只看前端：直接调服务层也要被拦住。

        注意要改**类**上的 capabilities —— 服务层每次都从仓库新建适配器实例，
        改某个实例的属性不起作用。
        """
        from cloudops.cloud.mock import MockProvider
        from cloudops.errors import CloudOpsError

        async def scenario():
            harness, _ = await self.start_panel()
            harness.app.store.bind(ADMIN_ID, "mock", "default", {"token": "demo"},
                                   shared=True, activate=True)
            await self.seed_instance(harness, "web-04")
            original = MockProvider.capabilities
            MockProvider.capabilities = frozenset({"list", "create"})
            try:
                with self.assertRaises(CloudOpsError) as ctx:
                    await harness.app.web.service.destroy("mock", "mock-1001", "web-04")
                self.assertEqual(ctx.exception.key, "web.error.action_unsupported")
                listing = await harness.app.web.service.instances()
                self.assertEqual(listing["instances"][0]["actions"], [])
            finally:
                MockProvider.capabilities = original
            await self.stop_all()

        self.run_async(scenario())

    def test_destroy_accepts_empty_body(self) -> None:
        """curl 式调用（不带 JSON）不该吃 400，只看 confirm 是否匹配。"""

        async def scenario():
            harness, _ = await self.start_panel()
            harness.app.store.bind(ADMIN_ID, "mock", "default", {"token": "demo"},
                                   shared=True, activate=True)
            instance = await self.seed_instance(harness, "web-05")
            async with self.client() as client:
                await self.login(client)
                token = await self.csrf(client)
                response = await self.api(
                    client, f"/api/instances/mock/{instance.id}/destroy",
                    method="POST", csrf=token, allow_redirects=False)
                # 空 body → confirm 为空 → 确认码不匹配（400），而不是 "invalid json"
                self.assertEqual(response.status, 400)
                self.assertNotIn("invalid json", await response.text())
            await self.stop_all()

        self.run_async(scenario())

    def test_destroy_requires_matching_name(self) -> None:
        async def scenario():
            harness, _ = await self.start_panel()
            harness.app.store.bind(ADMIN_ID, "mock", "default", {"token": "demo"},
                                   shared=True, activate=True)
            instance = await self.seed_instance(harness, "web-02")
            async with self.client() as client:
                await self.login(client)
                token = await self.csrf(client)
                wrong = await self.api(client, f"/api/instances/mock/{instance.id}/destroy",
                                       method="POST", body={"confirm": "other-name"},
                                       csrf=token)
                self.assertEqual(wrong.status, 400)
                ok = await self.api(client, f"/api/instances/mock/{instance.id}/destroy",
                                    method="POST", body={"confirm": "web-02"}, csrf=token)
                self.assertEqual(ok.status, 200)
                self.assertEqual((await ok.json())["destroyed"], instance.id)
                listing = await (await self.api(client, "/api/instances")).json()
                self.assertEqual(listing["total"], 0)
            logs = harness.app.db.list_logs(limit=5)
            self.assertEqual(logs[0].action, "web-destroy")
            self.assertEqual(logs[0].status, "success")
            await self.stop_all()

        self.run_async(scenario())

    def test_destroy_missing_instance_is_404(self) -> None:
        async def scenario():
            harness, _ = await self.start_panel()
            harness.app.store.bind(ADMIN_ID, "mock", "default", {"token": "demo"},
                                   shared=True, activate=True)
            async with self.client() as client:
                await self.login(client)
                token = await self.csrf(client)
                response = await self.api(client, "/api/instances/mock/mock-9999/destroy",
                                          method="POST", body={"confirm": "x"}, csrf=token)
                self.assertEqual(response.status, 404)
            self.assertEqual(harness.app.db.list_logs(limit=1)[0].action, "web-destroy")
            self.assertEqual(harness.app.db.list_logs(limit=1)[0].status, "failed")
            await self.stop_all()

        self.run_async(scenario())

    def test_credential_listing_masks_secrets(self) -> None:
        async def scenario():
            harness, _ = await self.start_panel()
            credential = harness.app.store.bind(ADMIN_ID, "mock", "demo-cred",
                                                {"token": "super-secret-token"},
                                                shared=True, activate=True)
            async with self.client() as client:
                await self.login(client)
                text = await (await self.api(client, "/api/credentials")).text()
            self.assertNotIn("super-secret-token", text)
            self.assertNotIn(credential.secret_blob, text)
            payload = json.loads(text)
            self.assertEqual(payload["total"], 1)
            self.assertEqual(payload["credentials"][0]["label"], "demo-cred")
            self.assertTrue(payload["credentials"][0]["is_active"])
            masked = payload["credentials"][0]["fields"]["token"]
            self.assertIn("****", masked)
            self.assertNotEqual(masked, "super-secret-token")
            await self.stop_all()

        self.run_async(scenario())

    def test_add_credential_validates_and_persists(self) -> None:
        async def scenario():
            harness, _ = await self.start_panel()
            async with self.client() as client:
                await self.login(client)
                token = await self.csrf(client)
                good = await self.api(client, "/api/credentials", method="POST",
                                      body={"provider": "mock", "label": "demo",
                                            "fields": {"token": "abc"}}, csrf=token)
                self.assertEqual(good.status, 201, await good.text())
                payload = await good.json()
                self.assertEqual(payload["provider"], "mock")
                self.assertEqual(harness.app.db.count_credentials("mock"), 1)
                credential = harness.app.db.list_credentials(provider="mock")[0]
                self.assertEqual(harness.app.store.secrets(credential)["token"], "abc")
                self.assertEqual((credential.meta or {}).get("source"), "web")

                missing = await self.api(client, "/api/credentials", method="POST",
                                         body={"provider": "mock", "label": "bad",
                                               "fields": {}}, csrf=token)
                self.assertEqual(missing.status, 400)
                self.assertIn("token", await missing.text())

                unknown = await self.api(client, "/api/credentials", method="POST",
                                         body={"provider": "aliyun", "label": "bad",
                                               "fields": {"token": "x"}}, csrf=token)
                self.assertEqual(unknown.status, 400)
                self.assertEqual(harness.app.db.count_credentials(), 1)
            await self.stop_all()

        self.run_async(scenario())

    def test_delete_and_activate_credentials(self) -> None:
        async def scenario():
            harness, _ = await self.start_panel()
            first = harness.app.store.bind(ADMIN_ID, "mock", "first", {"token": "1"},
                                           shared=True, activate=True)
            second = harness.app.store.bind(ADMIN_ID, "mock", "second", {"token": "2"},
                                            shared=True, activate=False)
            async with self.client() as client:
                await self.login(client)
                token = await self.csrf(client)
                activated = await self.api(client, f"/api/credentials/{second.id}/activate",
                                           method="POST", body={}, csrf=token)
                self.assertEqual(activated.status, 200)
                self.assertEqual(harness.app.db.get_credential(second.id).is_active, True)
                self.assertEqual(harness.app.db.get_credential(first.id).is_active, False)

                deleted = await self.api(client, f"/api/credentials/{first.id}",
                                         method="DELETE", csrf=token)
                self.assertEqual(deleted.status, 200)
                self.assertEqual(harness.app.db.count_credentials(), 1)
                latest = harness.app.db.list_logs(limit=2)
                self.assertEqual([row.action for row in latest], ["web-unbind", "web-activate"])
            await self.stop_all()

        self.run_async(scenario())

    def test_users_roles_and_validation(self) -> None:
        async def scenario():
            harness, _ = await self.start_panel()
            harness.app.db.ensure_user(ADMIN_ID, "boss", role="admin")
            async with self.client() as client:
                await self.login(client)
                token = await self.csrf(client)
                listing = await (await self.api(client, "/api/users")).json()
                self.assertEqual(listing["total"], 1)
                self.assertEqual(listing["roles"], ["admin", "readonly", "guest"])

                changed = await self.api(client, f"/api/users/{ADMIN_ID}/role", method="POST",
                                         body={"role": "readonly"}, csrf=token)
                self.assertEqual(changed.status, 200)
                self.assertEqual(harness.app.db.get_user(ADMIN_ID).role, "readonly")

                invalid = await self.api(client, f"/api/users/{ADMIN_ID}/role", method="POST",
                                         body={"role": "superuser"}, csrf=token)
                self.assertEqual(invalid.status, 400)
                self.assertEqual(harness.app.db.get_user(ADMIN_ID).role, "readonly")
            await self.stop_all()

        self.run_async(scenario())

    def test_logs_and_tasks_endpoints(self) -> None:
        async def scenario():
            harness, _ = await self.start_panel()
            db = harness.app.db
            db.log_operation(ADMIN_ID, "create", "success", provider="mock", target="web-01")
            db.create_task("task-1", ADMIN_ID, "mock", "create", payload={"name": "web-01"})
            async with self.client() as client:
                await self.login(client)
                logs = await (await self.api(client, "/api/logs?limit=5")).json()
                self.assertEqual(logs["total"], 1)
                self.assertEqual(logs["logs"][0]["action"], "create")
                tasks = await (await self.api(client, "/api/tasks")).json()
                self.assertEqual(tasks["tasks"][0]["id"], "task-1")
                clamped = await (await self.api(client, "/api/logs?limit=999999")).json()
                self.assertLessEqual(clamped["total"], 200)
                bogus = await (await self.api(client, "/api/logs?limit=abc")).json()
                self.assertEqual(bogus["total"], 1)          # 非法参数回落到默认值
            await self.stop_all()

        self.run_async(scenario())


class PanelUnitTest(AsyncTestCase):
    """认证原语的单元测试（不启服务，跑得快）。"""

    def test_hash_and_verify(self) -> None:
        from cloudops.web.auth import hash_password, verify_password

        stored = hash_password("s3cret")
        self.assertTrue(stored.startswith("scrypt$"))
        self.assertNotIn("s3cret", stored)
        self.assertTrue(verify_password("s3cret", stored))
        self.assertFalse(verify_password("s3cret ", stored))
        self.assertFalse(verify_password("", stored))
        self.assertFalse(verify_password("s3cret", "broken-format"))
        # 同样的口令两次哈希不同（随机盐）
        self.assertNotEqual(stored, hash_password("s3cret"))

    def test_session_roundtrip_and_expiry(self) -> None:
        import base64
        import json as jsonlib
        import time as timelib

        from cloudops.web.auth import SessionManager, hash_password, password_fingerprint

        stored = hash_password("pw")
        manager = SessionManager("secret-key-material", ttl=60)
        session = manager.issue(stored)
        verified = manager.verify(session.raw, stored)
        self.assertIsNotNone(verified)
        self.assertEqual(verified.csrf_token, session.csrf_token)
        self.assertEqual(
            SessionManager.read_cookie(f"a=1; {manager.cookie_name}={session.raw}"),
            session.raw)
        self.assertIsNone(SessionManager.read_cookie(None))
        self.assertIsNone(manager.verify(None, stored))
        self.assertIsNone(manager.verify("garbage", stored))
        # 口令换过 → 旧会话失效
        self.assertIsNone(manager.verify(session.raw, hash_password("other")))
        # 签名合法但已过期 → 失效
        body = jsonlib.dumps(
            {"v": 1, "sub": "admin", "exp": int(timelib.time()) - 5,
             "pv": password_fingerprint(stored), "csrf": "x"},
            separators=(",", ":"), sort_keys=True).encode()
        token = base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")
        self.assertIsNone(manager.verify(f"{token}.{manager._sign(body)}", stored))  # noqa: SLF001

    def test_throttle_window(self) -> None:
        from cloudops.web.auth import LoginThrottle

        throttle = LoginThrottle(max_attempts=2, window=60)
        self.assertEqual(throttle.blocked("1.2.3.4"), 0.0)
        throttle.record_failure("1.2.3.4")
        self.assertEqual(throttle.blocked("1.2.3.4"), 0.0)
        throttle.record_failure("1.2.3.4")
        self.assertGreater(throttle.blocked("1.2.3.4"), 0)
        self.assertEqual(throttle.blocked("5.6.7.8"), 0.0)      # 按来源隔离
        throttle.reset("1.2.3.4")
        self.assertEqual(throttle.blocked("1.2.3.4"), 0.0)
