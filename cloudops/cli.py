#!/usr/bin/env python3
"""CloudOps Bot 启动入口。

日常工作：

    cp .env.example .env        # 填 TELEGRAM_BOT_TOKEN / BOT_ADMIN_IDS / SECRET_KEY
    python manage.py gen-secret # 生成 SECRET_KEY
    python main.py              # 启动机器人（长轮询）

也支持两个不开网络的自检开关，方便在 CI 或新机器上先确认环境：

    python main.py --check      # 校验配置 + 初始化数据库，然后退出
    python main.py --version    # 打印版本
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from . import __homepage__, __project__, __version__
from .app import Application, build_application
from .config import DEFAULT_ENV_FILE, Settings
from .errors import CloudOpsError, ConfigError

logger = logging.getLogger("cloudops.main")


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cloudops-bot",
        description=f"{__project__} —— 用即时通讯机器人统一管理多云资源的运维入口",
        epilog=f"项目主页：{__homepage__}",
    )
    parser.add_argument("-e", "--env-file", default=DEFAULT_ENV_FILE,
                        help="配置文件路径（默认 .env，可设为 /dev/null 纯用环境变量）")
    parser.add_argument("--check", action="store_true",
                        help="只校验配置与数据库，不连接 Telegram")
    parser.add_argument("--no-banner", action="store_true", help="不打印启动横幅")
    parser.add_argument("--log-level", default=None,
                        help="覆盖 .env 里的 LOG_LEVEL（DEBUG/INFO/WARNING/ERROR）")
    parser.add_argument("--version", action="version",
                        version=f"{__project__} {__version__} ({__homepage__})")
    return parser.parse_args(argv)


def load_settings(args) -> Settings:
    """装配配置并给出"人话版"错误提示。"""
    env_file = args.env_file
    if env_file and env_file != DEFAULT_ENV_FILE and not Path(env_file).exists():
        raise ConfigError(f"配置文件不存在：{env_file}")
    settings = Settings.from_env(env_file)
    if args.log_level:
        settings.log_level = args.log_level.upper()
    if args.no_banner:
        settings.banner = False
    return settings


def check(settings: Settings) -> int:
    """配置自检：把"缺什么、怎么补"直接打在终端上。"""
    problems = settings.missing_requirements()
    print(f"● 项目      : {__project__} {__version__}")
    print(f"● 配置文件  : {settings.env_file if settings.env_file else '(仅环境变量)'}")
    print(f"● 数据库    : {settings.database_path}")
    print(f"● 语言/时区 : {settings.language} / {settings.display_timezone}")
    print(f"● 默认云平台: {settings.default_provider}")
    print(f"● 已授权 ID : {', '.join(str(i) for i in settings.admin_ids) or '(未配置)'}")
    print(f"● Mock 模式 : {'开启' if settings.enable_mock_provider else '关闭'}")
    if problems:
        print("\n✗ 配置不完整，按下面提示补齐后再启动：")
        for item in problems:
            print(f"  • {item}")
        return 2

    settings.ensure_directories()
    from cloudops.db import Database

    database = Database(settings.database_path)
    database.initialize()
    print(f"\n✓ 配置完整，数据库已就绪（表：{', '.join(database.tables())}）")
    print("  下一步：python main.py")
    return 0


async def serve(settings: Settings) -> int:
    """启动长轮询服务，直到收到 SIGINT/SIGTERM。"""
    app: Application = build_application(settings)
    try:
        await app.setup()
        await app.run()
    finally:
        # 即使是 setup 阶段失败（Token 错、数据库不可写）也要走一遍收尾，
        # 否则可能留下未关闭的 HTTP 会话
        await app.shutdown()
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        settings = load_settings(args)
    except ConfigError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2

    if args.check:
        return check(settings)

    try:
        return asyncio.run(serve(settings))
    except ConfigError as exc:
        # 只有配置确实缺项时，`--check` 才帮得上忙；SECRET_KEY 与库里对不上
        # 这类错误 `--check` 会一路绿灯，指过去只会把人绕晕。
        message = f"✗ {exc}"
        if settings.missing_requirements():
            message += "\n提示：先执行 `python main.py --check` 看缺哪一项。"
        print(message, file=sys.stderr)
        return 2
    except CloudOpsError as exc:  # pragma: no cover - 启动阶段业务错误
        print(f"✗ 启动失败：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover - 交互式中断
        print("\n已退出。")
        return 0


if __name__ == "__main__":
    sys.exit(main())
