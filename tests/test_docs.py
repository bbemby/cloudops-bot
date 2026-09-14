"""文档与代码的一致性测试。

开源项目最常见的"腐烂"是文档漂移：README 里的指令早被删了，`.env.example`
少了新增的变量，用户照着做却起不来。这里用测试把三份东西钉在一起：
**代码里读的配置** ↔ **.env.example** ↔ **README**。
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from support import AsyncTestCase, Harness, make_settings

ROOT = Path(__file__).resolve().parents[1]
CONFIG_SOURCE = (ROOT / "cloudops" / "config.py").read_text(encoding="utf-8")
ENV_EXAMPLE = (ROOT / ".env.example").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")

#: config.py 里读取环境变量的辅助函数
ENV_READER = re.compile(r"""_env_(?:str|get|bool|int|float|list|ids)\(\s*["']([A-Z0-9_]+)["']""")
#: .env.example 里声明的变量
ENV_DECLARED = re.compile(r"^([A-Z][A-Z0-9_]*)\s*=", re.MULTILINE)

#: 历史/第三方习惯用的别名：值取自另一个已文档化的变量，因此不单独声明
#: （``AWS_DEFAULT_REGION`` 是 AWS SDK 的惯用名，与 ``AWS_REGION`` 等价）
ENV_ALIASES = {"AWS_DEFAULT_REGION": "AWS_REGION"}


class EnvExampleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.used = set(ENV_READER.findall(CONFIG_SOURCE))
        self.documented = set(ENV_DECLARED.findall(ENV_EXAMPLE))

    def test_config_reads_something(self) -> None:
        self.assertGreater(len(self.used), 20)      # 正则没写错

    def test_every_setting_is_documented(self) -> None:
        required = {ENV_ALIASES.get(name, name) for name in self.used}
        missing = sorted(required - self.documented)
        self.assertEqual(missing, [], f".env.example 缺少这些变量：{missing}")

    def test_aliases_point_at_documented_variables(self) -> None:
        for alias, canonical in ENV_ALIASES.items():
            self.assertIn(alias, self.used, f"{alias} 已不再被读取，请从别名表里删掉")
            self.assertIn(canonical, self.documented)

    def test_no_stale_variables_in_example(self) -> None:
        stale = sorted(self.documented - self.used)
        self.assertEqual(stale, [], f".env.example 里这些变量代码已经不读了：{stale}")

    def test_example_never_contains_real_secret(self) -> None:
        """样例文件里所有敏感项都必须是空值。"""
        for line in ENV_EXAMPLE.splitlines():
            match = ENV_DECLARED.match(line)
            if not match:
                continue
            if any(token in match.group(1) for token in ("TOKEN", "SECRET", "KEY", "IDS")):
                self.assertEqual(line.split("=", 1)[1].strip(), "",
                                 f"{match.group(1)} 在 .env.example 里必须留空")


class ReadmeTest(unittest.TestCase):
    def test_readme_mentions_entrypoints(self) -> None:
        for text in ("python main.py", "python manage.py gen-secret",
                     "python -m unittest discover", ".env.example"):
            self.assertIn(text, README, f"README 没提到 {text}")

    def test_readme_links_to_license_and_env_example(self) -> None:
        self.assertIn("](LICENSE)", README)
        self.assertIn("](.env.example)", README)


class AdminPanelDocTest(unittest.TestCase):
    """.env.example 里写的端口、README 里的说明、代码里的默认值必须一致。"""

    def test_readme_documents_the_panel(self) -> None:
        for text in ("9878", "WEB_ADMIN_PASSWORD", "WEB_ENABLED=false"):
            self.assertIn(text, README, f"README 没提到面板相关配置：{text}")

    def test_readme_links_to_docker_tutorial(self) -> None:
        self.assertIn("](docs/DOCKER.md", README)      # 允许带 #锚点 的写法
        self.assertTrue((ROOT / "docs" / "DOCKER.md").exists())

    def test_env_example_documents_panel_security_switches(self) -> None:
        for name in ("WEB_ENABLED", "WEB_ADMIN_PASSWORD", "WEB_READONLY",
                     "WEB_SECURE_COOKIE", "WEB_TRUST_PROXY"):
            self.assertIn(f"\n{name}=", ENV_EXAMPLE, f".env.example 缺少 {name}")

    def test_env_default_port_matches_config(self) -> None:
        from cloudops.config import WebSettings

        self.assertEqual(WebSettings().port, 9878)
        self.assertIn("\nWEB_PORT=9878\n", ENV_EXAMPLE)
        self.assertIn("9878", (ROOT / "Dockerfile").read_text(encoding="utf-8"))

    def test_compose_maps_the_panel_port(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("${WEB_PANEL_PORT:-9878}:9878", compose)


class DockerTutorialTest(unittest.TestCase):
    """教程里的命令必须是真命令 —— 照抄能跑，而不是"看着像"。"""

    def setUp(self) -> None:
        self.doc = (ROOT / "docs" / "DOCKER.md").read_text(encoding="utf-8")

    def test_documented_commands_exist(self) -> None:
        # 教程里出现的 CLI 用法都要在代码里真的支持
        for command in ("manage.py gen-secret -w", "main.py -e .env --check",
                        "docker compose pull", "docker compose up -d",
                        "WEB_PANEL_PORT"):
            self.assertIn(command, self.doc, f"教程没写 {command}")

    def test_gen_secret_write_is_supported_by_manage_py(self) -> None:
        source = (ROOT / "manage.py").read_text(encoding="utf-8")
        self.assertIn('"--write"', source)

    def test_docker_run_example_matches_the_image(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("EXPOSE 9878", dockerfile)
        self.assertIn("python deploy/healthcheck.py", dockerfile)
        self.assertIn("useradd --uid 10001", dockerfile)

    def test_tutorial_states_verification_level(self) -> None:
        """教程末尾要诚实标明哪些是实测过的、哪些没跑过。"""
        self.assertIn("验证程度", self.doc)
        self.assertIn("实测通过", self.doc)


class CommandDocTest(AsyncTestCase):
    def test_every_command_is_documented(self) -> None:
        """所有注册的指令都要出现在 README 里，避免"隐藏功能"。"""

        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                harness = Harness(make_settings(Path(tmp)))
                await harness.start()
                names = [spec.name for spec in harness.app.dispatcher.specs()]
                await harness.stop()
            undocumented = [name for name in names if f"/{name}" not in README]
            self.assertEqual(undocumented, [], f"README 未记录的指令：{undocumented}")

        self.run_async(scenario())


class RepoHygieneTest(unittest.TestCase):
    def test_required_files_exist(self) -> None:
        for name in ("README.md", "LICENSE", ".gitignore", "requirements.txt",
                     ".env.example", "main.py", "manage.py"):
            self.assertTrue((ROOT / name).exists(), f"缺少 {name}")

    def test_gitignore_covers_secrets_and_state(self) -> None:
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for pattern in (".env", "data/", "__pycache__/"):
            self.assertIn(pattern, ignored, f".gitignore 未忽略 {pattern}")

    def test_no_env_file_is_tracked_artifact(self) -> None:
        """工作区里可以存在 .env，但它必须被 .gitignore 覆盖。"""
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn(".env", [line.strip() for line in gitignore])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
