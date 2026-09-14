"""配置层测试：.env 解析、优先级、校验提示、脱敏快照。"""

from __future__ import annotations

import os
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

from support import ROOT  # noqa: F401

from cloudops.config import Settings, TelegramSettings
from cloudops.crypto import generate_key
from cloudops.errors import ConfigError

ENV_SAMPLE = """\
# CloudOps Bot 示例配置
TELEGRAM_BOT_TOKEN=123456:AAExampleToken
BOT_ADMIN_IDS=111,222, abc, @333
BOT_READONLY_IDS=444
SECRET_KEY=abc
RATE_LIMIT_PER_MINUTE = 35
ENABLE_MOCK_PROVIDER=yes
DO_SSH_KEYS=k1;k2,k3
DO_DEFAULT_REGION=fra1
AWS_REGIONS=us-east-1, ap-northeast-1
DISPLAY_TIMEZONE=Asia/Tokyo
DATABASE_PATH=./data/test.db
LOG_FILE=./data/bot.log
"""


class ConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_env(self, text: str) -> Path:
        path = self.dir / ".env"
        path.write_text(text, encoding="utf-8")
        return path

    def _load(self, text: str, **environ: str) -> Settings:
        path = self._write_env(text)
        with mock.patch.dict(os.environ, dict(environ), clear=True):
            return Settings.from_env(str(path))

    # ------------------------------------------------------------------ #
    def test_env_file_is_parsed(self) -> None:
        settings = self._load(ENV_SAMPLE)
        self.assertEqual(settings.telegram.token, "123456:AAExampleToken")
        self.assertEqual(settings.admin_ids, (111, 222, 333))   # 非法项被忽略
        self.assertEqual(settings.readonly_ids, (444,))
        self.assertEqual(settings.rate_limit_per_minute, 35)
        self.assertTrue(settings.enable_mock_provider)
        self.assertEqual(settings.digitalocean.ssh_keys, ["k1", "k2", "k3"])
        self.assertEqual(settings.digitalocean.region, "fra1")
        self.assertEqual(settings.aws.regions, ["us-east-1", "ap-northeast-1"])
        self.assertEqual(settings.display_timezone, "Asia/Tokyo")
        self.assertEqual(settings.database_path, Path("./data/test.db"))
        self.assertEqual(settings.log_file, Path("./data/bot.log"))
        self.assertEqual(settings.env_file, self.dir / ".env")
        self.assertEqual(settings.missing_requirements(), [])

    def test_process_env_wins_over_env_file(self) -> None:
        settings = self._load("TELEGRAM_BOT_TOKEN=from-file\n", TELEGRAM_BOT_TOKEN="from-process")
        self.assertEqual(settings.telegram.token, "from-process")

    def test_missing_requirements_are_actionable(self) -> None:
        settings = self._load("LOG_LEVEL=DEBUG\n")
        problems = settings.missing_requirements()
        self.assertEqual(len(problems), 3)
        joined = "\n".join(problems)
        self.assertIn("TELEGRAM_BOT_TOKEN", joined)
        self.assertIn("BOT_ADMIN_IDS", joined)
        self.assertIn("manage.py gen-secret", joined)

    def test_validate_raises_config_error(self) -> None:
        settings = self._load("LOG_LEVEL=DEBUG\n")
        with self.assertRaises(ConfigError) as ctx:
            settings.validate()
        self.assertIn("配置校验失败", str(ctx.exception))
        self.assertEqual(ctx.exception.key, "error.config")

    def test_validate_can_skip_telegram_requirement(self) -> None:
        settings = self._load("LOG_LEVEL=DEBUG\n")
        with self.assertRaises(ConfigError) as ctx:
            settings.validate(require_telegram=False)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", str(ctx.exception))

    def test_invalid_numbers_fall_back_to_defaults(self) -> None:
        settings = self._load("RATE_LIMIT_PER_MINUTE=abc\nHTTP_TIMEOUT_SECONDS=whatever\n")
        self.assertEqual(settings.rate_limit_per_minute, 20)
        self.assertEqual(settings.http_timeout, 30.0)

    def test_log_snapshot_is_redacted(self) -> None:
        settings = self._load(ENV_SAMPLE)
        settings.secret_key = generate_key()
        fields = settings.as_log_fields()
        self.assertEqual(fields["secret_key"], "***")
        self.assertEqual(fields["telegram_bot_token"], "***")
        self.assertEqual(fields["default_provider"], "digitalocean")

    def test_ensure_directories(self) -> None:
        settings = self._load(ENV_SAMPLE)
        settings.database_path = self.dir / "nested" / "db" / "cloudops.db"
        settings.mock_state_path = self.dir / "nested" / "state.json"
        settings.log_file = self.dir / "nested" / "logs" / "bot.log"
        settings.ensure_directories()
        self.assertTrue(settings.database_path.parent.is_dir())
        self.assertTrue(settings.mock_state_path.parent.is_dir())
        self.assertTrue(settings.log_file.parent.is_dir())

    def test_boolean_parsing(self) -> None:
        for raw, expected in (("true", True), ("0", False), ("YES", True), ("off", False)):
            settings = self._load(f"ENABLE_MOCK_PROVIDER={raw}\n")
            self.assertIs(settings.enable_mock_provider, expected, raw)

    def test_defaults_are_sane(self) -> None:
        settings = Settings()
        self.assertEqual(settings.language, "zh")
        self.assertEqual(settings.telegram.api_base, "https://api.telegram.org")
        self.assertEqual(settings.digitalocean.region, "sgp1")
        self.assertEqual(settings.aws.region_list, ["us-east-1"])
        self.assertFalse(settings.telegram.configured)
        self.assertTrue(TelegramSettings(token="x").configured)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
