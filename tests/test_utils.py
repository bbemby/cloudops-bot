"""tools / utils 模块的单元测试（时间、脱敏、消息切分、.env 解析）。"""

from __future__ import annotations

import os
import tempfile
import unittest
import unittest.mock as mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

from support import ROOT  # noqa: F401 - 触发 sys.path 注入

from cloudops.utils import (
    dumps,
    envelope,
    format_local,
    get_timezone,
    humanize_age,
    humanize_duration,
    is_ip_address,
    load_dotenv_file,
    looks_like_ipv4,
    looks_like_ipv6,
    mask_mapping,
    mask_secret,
    now_iso,
    now_utc,
    parse_dotenv,
    parse_iso,
    random_code,
    random_suffix,
    redact_mapping,
    split_message,
    to_iso,
    truncate,
    unique,
)


class TimeTest(unittest.TestCase):
    def test_now_iso_is_utc_z_suffixed(self) -> None:
        value = now_iso()
        self.assertTrue(value.endswith("Z"))
        self.assertIsNotNone(parse_iso(value))

    def test_to_iso_normalizes_naive_datetime(self) -> None:
        naive = datetime(2026, 1, 2, 3, 4, 5)
        self.assertEqual(to_iso(naive), "2026-01-02T03:04:05Z")

    def test_to_iso_converts_other_timezones(self) -> None:
        shanghai = get_timezone("Asia/Shanghai")
        local = datetime(2026, 1, 2, 11, 0, 0, tzinfo=shanghai)
        self.assertEqual(to_iso(local), "2026-01-02T03:00:00Z")

    def test_parse_iso_accepts_z_and_offset(self) -> None:
        self.assertEqual(parse_iso("2026-01-02T03:04:05Z").tzinfo, timezone.utc)
        self.assertEqual(parse_iso("2026-01-02T03:04:05+00:00").hour, 3)

    def test_parse_iso_rejects_garbage(self) -> None:
        self.assertIsNone(parse_iso("not-a-time"))
        self.assertIsNone(parse_iso(None))
        self.assertIsNone(parse_iso(""))

    def test_format_local_respects_timezone(self) -> None:
        stamp = "2026-01-02T03:00:00Z"
        self.assertEqual(format_local(stamp, "Asia/Shanghai"), "2026-01-02 11:00")
        self.assertEqual(format_local(stamp, "UTC"), "2026-01-02 03:00")
        self.assertEqual(format_local("bogus", "UTC"), "-")

    def test_humanize_age(self) -> None:
        recent = to_iso(now_utc() - timedelta(seconds=30))
        self.assertIn("秒前", humanize_age(recent))
        self.assertIn("分钟前", humanize_age(to_iso(now_utc() - timedelta(minutes=5))))
        self.assertIn("小时前", humanize_age(to_iso(now_utc() - timedelta(hours=3))))
        # 未来时间不做"负 N 秒前"这种怪文案
        self.assertIn("秒前", humanize_age(to_iso(now_utc() + timedelta(minutes=5))))
        self.assertEqual(humanize_age(None), "-")

    def test_humanize_duration(self) -> None:
        self.assertEqual(humanize_duration(0), "0s")
        self.assertEqual(humanize_duration(45), "45s")
        self.assertEqual(humanize_duration(75), "1m15s")
        self.assertEqual(humanize_duration(3725), "1h02m")


class MaskTest(unittest.TestCase):
    def test_mask_secret(self) -> None:
        self.assertEqual(mask_secret(""), "-")
        self.assertEqual(mask_secret(None), "-")
        self.assertEqual(mask_secret("abcd"), "****")
        masked = mask_secret("wJalrXUtnFEMI/K7MDENGbPxRfiCYEXAMPLEKEY")
        self.assertTrue(masked.startswith("wJal"))
        self.assertTrue(masked.endswith("EKEY"))
        self.assertNotIn("XUtnFEMI", masked)

    def test_mask_mapping_only_touches_secret_fields(self) -> None:
        masked = mask_mapping({"token": "abcdefghijkl", "region": "sgp1"}, ("token",))
        self.assertEqual(masked["region"], "sgp1")
        self.assertNotEqual(masked["token"], "abcdefghijkl")

    def test_redact_mapping_hides_suspicious_keys(self) -> None:
        redacted = redact_mapping({"TELEGRAM_BOT_TOKEN": "x", "SECRET_KEY": "y",
                                   "LOG_LEVEL": "INFO", "aws_access_key_id": "z"})
        self.assertEqual(redacted["TELEGRAM_BOT_TOKEN"], "***")
        self.assertEqual(redacted["SECRET_KEY"], "***")
        self.assertEqual(redacted["aws_access_key_id"], "***")
        self.assertEqual(redacted["LOG_LEVEL"], "INFO")


class TextTest(unittest.TestCase):
    def test_truncate(self) -> None:
        self.assertEqual(truncate("short"), "short")
        self.assertEqual(truncate("a\nb\nc"), "a b c")
        self.assertTrue(truncate("x" * 300, 20).endswith("…"))
        self.assertEqual(len(truncate("x" * 300, 20)), 20)

    def test_split_message_respects_limit(self) -> None:
        text = "\n".join(f"line-{index}" for index in range(400))
        chunks = split_message(text, 200)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 200 for chunk in chunks))
        self.assertEqual("".join(chunk.replace("\n", "") for chunk in chunks).replace(" ", ""),
                         text.replace("\n", "").replace(" ", ""))

    def test_split_message_handles_empty(self) -> None:
        self.assertEqual(split_message("", 100), [""])

    def test_unique_preserves_order(self) -> None:
        self.assertEqual(unique(["b", "a", "b", "c", "a"]), ["b", "a", "c"])

    def test_envelope(self) -> None:
        self.assertIn("-----", envelope("hello"))


class RandomTest(unittest.TestCase):
    def test_random_code_charset_and_length(self) -> None:
        codes = {random_code(6) for _ in range(50)}
        self.assertEqual(len(codes), 50)
        for code in codes:
            self.assertEqual(len(code), 6)
            self.assertFalse(set(code) & set("0O1I"))       # 剔除易混淆字符
            self.assertTrue(set(code) <= set("ACDEFGHJKLMNPQRTUVWXY3456789"))

    def test_random_suffix(self) -> None:
        self.assertNotEqual(random_suffix(6), random_suffix(6))


class AddressTest(unittest.TestCase):
    def test_looks_like_ipv4(self) -> None:
        self.assertTrue(looks_like_ipv4("203.0.113.10"))
        self.assertFalse(looks_like_ipv4("203.0.113.999"))
        self.assertFalse(looks_like_ipv4("example.com"))
        self.assertFalse(looks_like_ipv4(""))

    def test_looks_like_ipv6(self) -> None:
        self.assertTrue(looks_like_ipv6("2001:db8::1"))
        self.assertFalse(looks_like_ipv6("2001:db8::1::2"))

    def test_is_ip_address(self) -> None:
        self.assertTrue(is_ip_address("10.0.0.1"))
        self.assertFalse(is_ip_address("10.0.0"))


class DotenvTest(unittest.TestCase):
    def test_parse_dotenv_handles_quotes_comments_and_export(self) -> None:
        text = "\n".join([
            "# 注释行",
            "export A=1",
            "B='quoted value'",
            'C="double value"',
            "D=bare # 行内注释",
            "E=",
            "",
            "NOT_A_VAR line",
        ])
        values = parse_dotenv(text)
        self.assertEqual(values["A"], "1")
        self.assertEqual(values["B"], "quoted value")
        self.assertEqual(values["C"], "double value")
        self.assertEqual(values["D"], "bare")
        self.assertEqual(values["E"], "")
        self.assertNotIn("NOT_A_VAR", values)

    def test_load_dotenv_file_does_not_override_existing_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("TELEGRAM_BOT_TOKEN=from-file\nLOG_LEVEL=DEBUG\n", encoding="utf-8")
            environ = {"TELEGRAM_BOT_TOKEN": "from-process"}
            applied = load_dotenv_file(path, environ)
            self.assertEqual(environ["TELEGRAM_BOT_TOKEN"], "from-process")
            self.assertEqual(environ["LOG_LEVEL"], "DEBUG")
            self.assertEqual(applied, {"LOG_LEVEL": "DEBUG"})

    def test_load_dotenv_file_missing_file(self) -> None:
        self.assertEqual(load_dotenv_file(Path("/nonexistent/.env"), {}), {})

    def test_dumps_is_stable(self) -> None:
        self.assertEqual(dumps({"b": 1, "a": 2}), '{"a":2,"b":1}')


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
