"""凭证加密（Fernet + HKDF 口令派生）测试。"""

from __future__ import annotations

import unittest

from support import ROOT  # noqa: F401

from cloudops.crypto import (
    SecretBox,
    build_secret_box,
    derive_key,
    fingerprint,
    generate_key,
    is_fernet_key,
)
from cloudops.errors import ConfigError


class KeyTest(unittest.TestCase):
    def test_generate_key_is_valid_fernet_key(self) -> None:
        key = generate_key()
        self.assertTrue(is_fernet_key(key))
        self.assertEqual(len(key), 44)

    def test_is_fernet_key_rejects_garbage(self) -> None:
        self.assertFalse(is_fernet_key(""))
        self.assertFalse(is_fernet_key("short"))
        self.assertFalse(is_fernet_key("!" * 44))

    def test_derive_key_is_deterministic_and_salted(self) -> None:
        first = derive_key("passphrase")
        self.assertEqual(first, derive_key("passphrase"))
        self.assertNotEqual(first, derive_key("passphrase2"))
        self.assertTrue(is_fernet_key(first.decode("utf-8")))

    def test_fingerprint_is_stable_and_short(self) -> None:
        self.assertEqual(fingerprint("token"), fingerprint("token"))
        self.assertEqual(len(fingerprint("token")), 8)
        self.assertNotIn("token", fingerprint("token"))
        self.assertEqual(fingerprint(""), "-")


class SecretBoxTest(unittest.TestCase):
    def test_roundtrip_with_fernet_key(self) -> None:
        box = SecretBox(generate_key())
        token = box.encrypt("dop_v1_secret")
        self.assertNotIn("dop_v1_secret", token)
        self.assertEqual(box.decrypt(token), "dop_v1_secret")
        self.assertEqual(box.key_source, "fernet")

    def test_roundtrip_with_passphrase(self) -> None:
        box = SecretBox("my-admin-passphrase")
        self.assertEqual(box.key_source, "passphrase")
        self.assertEqual(box.decrypt(box.encrypt("hello")), "hello")

    def test_dict_roundtrip(self) -> None:
        box = SecretBox(generate_key())
        payload = {"access_key_id": "AKIA", "secret_access_key": "sk", "region": "us-east-1"}
        self.assertEqual(box.decrypt_dict(box.encrypt_dict(payload)), payload)

    def test_missing_secret_key_raises_config_error(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            build_secret_box("")
        self.assertEqual(ctx.exception.key, "error.missing_secret_key")
        self.assertIn("gen-secret", str(ctx.exception))

    def test_strict_mode_rejects_non_fernet_key(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            build_secret_box("not-a-fernet-key", strict=True)
        self.assertEqual(ctx.exception.key, "error.invalid_secret_key")

    def test_wrong_key_cannot_decrypt(self) -> None:
        token = SecretBox(generate_key()).encrypt("secret")
        other = SecretBox(generate_key())
        with self.assertRaises(ConfigError) as ctx:
            other.decrypt(token)
        self.assertEqual(ctx.exception.key, "error.decrypt_failed")

    def test_passphrase_key_still_decrypts_after_restart(self) -> None:
        """同一条 SECRET_KEY（口令形式）在进程重启后必须仍能解密。"""
        token = SecretBox("stable-passphrase").encrypt("payload")
        self.assertEqual(SecretBox("stable-passphrase").decrypt(token), "payload")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
