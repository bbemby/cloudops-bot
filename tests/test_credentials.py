"""凭证仓库测试：加密落库、校验、共享、禁用 mock 等。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from support import (
    ROOT,  # noqa: F401
    make_settings,
)

from cloudops.cloud.mock import MockProvider
from cloudops.credentials import CredentialStore
from cloudops.crypto import SecretBox, generate_key
from cloudops.db import Database
from cloudops.errors import CloudOpsError, CredentialError, ValidationError


class CredentialStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.settings = make_settings(self.tmp_path)
        self.db = Database(self.settings.database_path)
        self.db.initialize()
        self.box = SecretBox(generate_key())
        self.store = CredentialStore(self.db, self.box, self.settings)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ------------------------------------------------------------------ #
    def test_bind_encrypts_secret_in_database(self) -> None:
        credential = self.store.bind(111, "do", "main", {"token": "dop_v1_supersecret"})
        self.assertEqual(credential.provider, "digitalocean")   # 别名归一
        self.assertNotIn("dop_v1_supersecret", credential.secret_blob)
        self.assertEqual(self.store.secrets(credential)["token"], "dop_v1_supersecret")
        self.assertTrue(credential.is_active)

    def test_bind_is_idempotent_per_label(self) -> None:
        self.store.bind(111, "digitalocean", "main", {"token": "a"})
        with self.assertRaises(CloudOpsError) as ctx:
            self.store.bind(111, "digitalocean", "main", {"token": "b"})
        self.assertEqual(ctx.exception.key, "error.duplicate_credential")

    def test_bind_rejects_invalid_label(self) -> None:
        with self.assertRaises(ValidationError):
            self.store.bind(111, "digitalocean", "x" * 40, {"token": "a"})

    def test_bind_rejects_unknown_provider(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            self.store.bind(111, "aliyun", "main", {"token": "a"})
        self.assertEqual(ctx.exception.key, "error.unknown_option")

    def test_mock_provider_can_be_disabled(self) -> None:
        self.settings.enable_mock_provider = False
        with self.assertRaises(ValidationError) as ctx:
            self.store.bind(111, "mock", "demo", {"token": "x"})
        self.assertEqual(ctx.exception.key, "error.mock_disabled")
        self.assertNotIn("mock", self.store.bound_providers(111))

    def test_active_for_prefers_own_credential_then_shared(self) -> None:
        shared = self.store.bind(111, "mock", "shared", {"token": "t"}, shared=True)
        own = self.store.bind(222, "mock", "own", {"token": "t2"}, shared=False)

        self.assertEqual(self.store.active_for("demo", 111).id, shared.id)
        self.assertEqual(self.store.active_for("mock", 222).id, own.id)
        # 第三个人只能拿到共享的那条
        self.assertEqual(self.store.active_for("mock", 333).id, shared.id)

    def test_provider_assembly_and_label_selection(self) -> None:
        self.store.bind(111, "mock", "first", {"token": "t1"})
        second = self.store.bind(111, "mock", "second", {"token": "t2", "delay": "0"})

        provider, credential = self.store.provider_with_credential("local", 111, label="second")
        try:
            self.assertIsInstance(provider, MockProvider)
            self.assertEqual(credential.id, second.id)
            self.assertEqual(provider.secrets["token"], "t2")
        finally:
            provider.close()

        with self.assertRaises(CredentialError) as ctx:
            self.store.provider_with_credential("mock", 111, label="missing")
        self.assertEqual(ctx.exception.key, "error.credential_label_not_found")

    def test_provider_requires_credentials(self) -> None:
        with self.assertRaises(CredentialError) as ctx:
            self.store.provider("mock", 111)
        self.assertEqual(ctx.exception.key, "error.credential_missing")
        # 提示里带上可执行的绑定用法
        self.assertIn("/bind mock", ctx.exception.params["usage"])

    def test_provider_from_credential_roundtrip(self) -> None:
        credential = self.store.bind(111, "mock", "demo", {"token": "tk", "delay": "0"})
        provider = self.store.provider_from_credential(credential)
        try:
            self.assertEqual(provider.validate_credentials(), f"本地模拟账户（{self.settings.mock_state_path.name}）")
        finally:
            provider.close()

    def test_activate_switches_workspace(self) -> None:
        first = self.store.bind(111, "mock", "a", {"token": "x"})
        second = self.store.bind(111, "mock", "b", {"token": "y"})
        self.assertTrue(second.is_active)
        self.store.activate(first.id, owner_id=111)
        self.assertEqual(self.store.active_for("mock", 111).id, first.id)

    def test_delete_removes_credential_and_secret(self) -> None:
        credential = self.store.bind(111, "mock", "temp", {"token": "x"})
        removed = self.store.delete(credential.id, owner_id=111)
        self.assertEqual(removed.id, credential.id)
        self.assertEqual(self.store.list_for_user(111), [])

    def test_update_meta_merges(self) -> None:
        credential = self.store.bind(111, "mock", "meta", {"token": "x"})
        self.store.update_meta(credential, {"identity": "acct@example.com"})
        refreshed = self.store.credential_by_id(credential.id)
        self.assertEqual(refreshed.meta_value("identity"), "acct@example.com")

    def test_describe_credentials_masks_secrets(self) -> None:
        self.store.bind(111, "aws", "prod", {
            "access_key_id": "AKIAIOSFODNN7EXAMPLE",
            "secret_access_key": "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
            "region": "us-east-1",
        })
        rows = self.store.describe_credentials(111)
        self.assertEqual(len(rows), 1)
        masked = rows[0]["masked"]
        self.assertEqual(masked["region"], "us-east-1")
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", masked["access_key_id"])
        self.assertNotIn("wJalrXUtnFEMI", masked["secret_access_key"])

    def test_bound_providers_filters_disabled_mock(self) -> None:
        self.store.bind(111, "mock", "demo", {"token": "x"})
        self.assertEqual(self.store.bound_providers(111), ["mock"])
        self.settings.enable_mock_provider = False
        self.assertEqual(self.store.bound_providers(111), [])

    def test_bind_usage_renders_field_hints(self) -> None:
        self.assertIn("/bind digitalocean", self.store.bind_usage("do"))
        self.assertIn("/bind aws", self.store.bind_usage("aws"))
        self.assertIn("/bind", self.store.bind_usage("unknown-provider"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
