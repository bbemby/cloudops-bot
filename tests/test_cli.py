"""命令行入口的集成测试：真的起子进程，验证退出码与输出。

单元测试证明不了"用户按 README 敲命令能跑起来"，这里补上这一层。
子进程全部使用干净的环境变量，不读取开发机上的真实 .env。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cloudops.crypto import generate_key, is_fernet_key  # noqa: E402

#: 会自动从环境里剔除的前缀（避免读到真实密钥/真实数据库路径）
SENSITIVE_PREFIXES = ("TELEGRAM_", "BOT_", "SECRET_", "DIGITALOCEAN_", "AWS_", "MOCK_",
                      "DATABASE_PATH", "DO_", "ENABLE_MOCK_PROVIDER", "LOG_")


def clean_env(**overrides: str) -> dict:
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(SENSITIVE_PREFIXES)
    }
    env.setdefault("PATH", os.environ.get("PATH", ""))
    env.update(overrides)
    return env


class CliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)

    def run_cli(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, *args],
            cwd=str(ROOT), env=env if env is not None else clean_env(),
            capture_output=True, text=True, timeout=120,
        )

    def good_env(self) -> dict:
        return clean_env(
            TELEGRAM_BOT_TOKEN="123456:TEST-TOKEN",
            BOT_ADMIN_IDS="100000001",
            SECRET_KEY=generate_key(),
            DATABASE_PATH=str(self.data_dir / "cloudops.db"),
            MOCK_STATE_PATH=str(self.data_dir / "mock.json"),
            ENABLE_MOCK_PROVIDER="true",
        )


class MainEntryTest(CliTestCase):
    def test_version(self) -> None:
        result = self.run_cli("main.py", "--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("cloudops-bot", result.stdout)

    def test_module_entry_is_equivalent(self) -> None:
        result = self.run_cli("-m", "cloudops", "--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("cloudops-bot", result.stdout)

    def test_check_passes_with_full_config(self) -> None:
        result = self.run_cli("main.py", "-e", "/dev/null", "--check", env=self.good_env())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("数据库已就绪", result.stdout)
        self.assertTrue((self.data_dir / "cloudops.db").exists())

    def test_check_reports_missing_config_with_exit_code_2(self) -> None:
        result = self.run_cli("main.py", "-e", "/dev/null", "--check")
        self.assertEqual(result.returncode, 2)
        for token in ("TELEGRAM_BOT_TOKEN", "BOT_ADMIN_IDS", "SECRET_KEY"):
            self.assertIn(token, result.stdout)

    def test_missing_env_file_is_reported(self) -> None:
        result = self.run_cli("main.py", "-e", str(self.data_dir / "nope.env"), "--check")
        self.assertEqual(result.returncode, 2)
        self.assertIn("配置文件不存在", result.stderr)

    def test_startup_hint_points_at_check_when_config_incomplete(self) -> None:
        """配置缺项时，应该把用户引到 `--check` 那份清单上。"""
        result = self.run_cli("main.py", "-e", "/dev/null")
        self.assertEqual(result.returncode, 2)
        self.assertIn("TELEGRAM_BOT_TOKEN", result.stderr)
        self.assertIn("python main.py --check", result.stderr)

    def test_startup_hint_omitted_when_secret_key_mismatch(self) -> None:
        """密钥对不上不是"缺配置"，`--check` 会一路绿灯，不能再指过去。"""
        import asyncio

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from support import ADMIN_ID, Harness, make_settings

        old_key = generate_key()
        new_key = generate_key()
        db_path = self.data_dir / "data" / "cloudops.db"      # 与 support.make_settings 保持一致

        async def seed():
            settings = make_settings(self.data_dir, secret_key=old_key)
            harness = Harness(settings)
            await harness.start()
            harness.app.store.bind(ADMIN_ID, "mock", "default", {"token": "seed-token"})
            await harness.stop()

        asyncio.run(seed())
        self.assertTrue(db_path.exists())

        env = clean_env(
            TELEGRAM_BOT_TOKEN="123456:TEST-TOKEN",
            BOT_ADMIN_IDS="100000001",
            SECRET_KEY=new_key,                                  # 与库里的指纹不一致
            DATABASE_PATH=str(db_path),
            MOCK_STATE_PATH=str(self.data_dir / "data" / "mock-cloud.json"),
            ENABLE_MOCK_PROVIDER="true",
            # 万一守卫失效，也要在本机失败，绝不真的去连 Telegram
            TELEGRAM_API_BASE="http://127.0.0.1:9",
            TELEGRAM_RETRIES="0",
            TELEGRAM_REQUEST_TIMEOUT="2",
        )
        result = self.run_cli("main.py", "-e", "/dev/null", env=env)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("SECRET_KEY 与数据库", result.stderr)
        self.assertNotIn("python main.py --check", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_bad_token_does_not_leak_traceback(self) -> None:
        """启动失败也要给人话，而不是甩一坨堆栈。"""
        env = self.good_env()
        env["TELEGRAM_API_BASE"] = "http://127.0.0.1:9"      # 保证连不上
        env["TELEGRAM_RETRIES"] = "0"
        env["TELEGRAM_REQUEST_TIMEOUT"] = "2"
        result = self.run_cli("main.py", "-e", "/dev/null", env=env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("启动失败", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("Unclosed client session", result.stderr)
        self.assertNotIn("Unclosed client session", result.stdout)


class ManageEntryTest(CliTestCase):
    def test_gen_secret_prints_valid_fernet_key(self) -> None:
        result = self.run_cli("manage.py", "gen-secret")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(is_fernet_key(result.stdout.strip().splitlines()[0]))

    def test_gen_secret_writes_into_env_file(self) -> None:
        """``-w`` 直接改文件：只替换 SECRET_KEY 那一行，其它内容与注释原样保留。"""
        env_file = self.data_dir / ".env"
        env_file.write_text(
            "# 注释要留着\nTELEGRAM_BOT_TOKEN=123456:TEST\nSECRET_KEY=\nDATABASE_PATH=data/cloudops.db\n",
            encoding="utf-8")
        result = self.run_cli("manage.py", "-e", str(env_file), "gen-secret", "-w")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        text = env_file.read_text(encoding="utf-8")
        self.assertIn("# 注释要留着", text)
        self.assertIn("TELEGRAM_BOT_TOKEN=123456:TEST", text)
        self.assertIn("DATABASE_PATH=data/cloudops.db", text)
        written = [line for line in text.splitlines() if line.startswith("SECRET_KEY=")]
        self.assertEqual(len(written), 1)
        self.assertTrue(is_fernet_key(written[0].split("=", 1)[1]))

    def test_gen_secret_appends_when_key_missing(self) -> None:
        env_file = self.data_dir / ".env"
        env_file.write_text("TELEGRAM_BOT_TOKEN=123456:TEST\n", encoding="utf-8")
        self.assertEqual(self.run_cli("manage.py", "-e", str(env_file), "gen-secret", "-w")
                         .returncode, 0)
        self.assertIn("SECRET_KEY=", env_file.read_text(encoding="utf-8"))

    def test_gen_secret_warns_before_breaking_existing_database(self) -> None:
        """换密钥会废掉库里已加密的凭证 —— 必须先把这句提醒打出来。"""
        env_file = self.data_dir / ".env"
        env_file.write_text(
            f"SECRET_KEY={generate_key()}\nDATABASE_PATH={self.data_dir / 'cloudops.db'}\n",
            encoding="utf-8")
        (self.data_dir / "cloudops.db").write_bytes(b"SQLite format 3\x00" + b"\x00" * 4096)
        result = self.run_cli("manage.py", "-e", str(env_file), "gen-secret", "-w")
        self.assertEqual(result.returncode, 0)
        self.assertIn("无法解密", result.stderr)

    def test_gen_secret_write_without_env_file_is_reported(self) -> None:
        result = self.run_cli("manage.py", "-e", str(self.data_dir / "nope.env"),
                              "gen-secret", "-w")
        self.assertEqual(result.returncode, 2)
        self.assertIn("找不到配置文件", result.stderr)

    def test_gen_secret_without_write_only_prints(self) -> None:
        """不加 -w 不该碰任何文件（老行为保持不变）。"""
        env_file = self.data_dir / ".env"
        env_file.write_text("SECRET_KEY=\n", encoding="utf-8")
        result = self.run_cli("manage.py", "-e", str(env_file), "gen-secret")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(env_file.read_text(encoding="utf-8"), "SECRET_KEY=\n")

    def test_init_db_creates_tables(self) -> None:
        result = self.run_cli("manage.py", "-e", "/dev/null", "init-db", env=self.good_env())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("数据库已就绪", result.stdout)

    def test_doctor_without_config_exits_nonzero(self) -> None:
        result = self.run_cli("manage.py", "-e", "/dev/null", "doctor")
        self.assertEqual(result.returncode, 1)
        self.assertIn("✗ 配置", result.stdout)
        self.assertIn("体检未通过", result.stdout)

    def test_doctor_lists_providers(self) -> None:
        result = self.run_cli("manage.py", "-e", "/dev/null", "doctor", env=self.good_env())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("mock", result.stdout)

    def test_promote_and_users_roundtrip(self) -> None:
        env = self.good_env()
        promoted = self.run_cli("manage.py", "-e", "/dev/null", "promote", "100000002", env=env)
        self.assertEqual(promoted.returncode, 0, promoted.stdout + promoted.stderr)
        listed = self.run_cli("manage.py", "-e", "/dev/null", "users", env=env)
        self.assertIn("100000002", listed.stdout)
        self.assertIn("admin", listed.stdout)

    def test_invalid_telegram_id_is_rejected(self) -> None:
        result = self.run_cli("manage.py", "-e", "/dev/null", "promote", "not-a-number",
                              env=self.good_env())
        self.assertEqual(result.returncode, 2)
        self.assertIn("合法的 Telegram 数字 ID", result.stderr)

    def test_no_subcommand_shows_help(self) -> None:
        result = self.run_cli("manage.py")
        self.assertEqual(result.returncode, 1)
        self.assertIn("gen-secret", result.stdout)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
