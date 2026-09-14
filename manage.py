#!/usr/bin/env python3
"""运维小工具集（不需要 Bot 在线即可执行）。

    python manage.py gen-secret          # 生成 SECRET_KEY（只打印）
    python manage.py gen-secret -w       # 生成并直接写进 .env
    python manage.py doctor              # 体检：配置 / 数据库 / 网络 / 云凭证
    python manage.py init-db             # 建库建表（幂等）
    python manage.py users               # 列出已授权用户
    python manage.py promote 123456      # 把用户提升为管理员
    python manage.py demote 123456       # 降为普通用户
    python manage.py revoke 123456       # 撤销授权
    python manage.py creds               # 列出已绑定凭证（密钥脱敏）
    python manage.py stats               # 账号/操作统计
    python manage.py vacuum              # 整理数据库文件
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from cloudops import __homepage__, __project__, __version__
from cloudops.config import DEFAULT_ENV_FILE, Settings
from cloudops.crypto import generate_key
from cloudops.db import Database
from cloudops.errors import CloudOpsError

ROLES = ("admin", "user", "guest")


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #
def cmd_gen_secret(args, _settings: Optional[Settings] = None) -> int:
    key = generate_key()
    # -w 不带值时写到 -e 指定的文件（默认 .env），避免"我明明指了 -e 却改了另一个文件"；
    # 完全没给 -w（None）才是"只打印"的老行为。
    raw_write = getattr(args, "write", None)
    if raw_write is None:
        target = None
    else:
        target = raw_write or getattr(args, "env_file", None) or DEFAULT_ENV_FILE
    if not target:
        print(key)
        print("\n把上面这行写进 .env（或容器环境变量）：", file=sys.stderr)
        print(f"  SECRET_KEY={key}", file=sys.stderr)
        print("注意：SECRET_KEY 变了以后，已入库的云凭证将无法解密，需要重新 /bind。",
              file=sys.stderr)
        return 0

    path = Path(target)
    if not path.exists():
        print(f"✗ 找不到配置文件：{path}", file=sys.stderr)
        print("  先 `cp .env.example .env` 再执行本命令。", file=sys.stderr)
        return 2

    old = _read_secret_key(path)
    if old and old != key:
        # 换密钥会让库里已有的凭证解不开 —— 这是最容易被忽略的一次性破坏，必须先提醒
        database_path = _database_path_from_env(path)
        if database_path and database_path.exists() and database_path.stat().st_size:
            print(f"⚠ {database_path} 已存在：换掉 SECRET_KEY 后，里面的云凭证将无法解密，",
                  file=sys.stderr)
            print("  需要重新 /bind。（想保留旧库就别改密钥，或先备份。)",
                  file=sys.stderr)
    _write_secret_key(path, key)
    print(f"✓ 已写入 {path}：SECRET_KEY={key[:6]}…{key[-4:]}（{len(key)} 字符）")
    print("  下一条：python main.py --check 校验配置。")
    return 0


def _read_secret_key(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("SECRET_KEY="):
            return line.split("=", 1)[1].strip()
    return ""


def _write_secret_key(path: Path, key: str) -> None:
    """就地替换 SECRET_KEY 行；没有就追加。保留其它内容与注释不动。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.strip().startswith("SECRET_KEY="):
            lines[index] = f"SECRET_KEY={key}"
            break
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("SECRET_KEY=" + key)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _database_path_from_env(path: Path) -> Optional[Path]:
    """从 .env 里读出 DATABASE_PATH，用于"改密钥会不会废掉旧库"的判断。"""
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("DATABASE_PATH="):
            value = stripped.split("=", 1)[1].strip().strip('"').strip("'")
            if value:
                return Path(value)
    return None


def cmd_doctor(args, settings: Settings) -> int:
    """逐项体检，尽量把问题定位到"下一步该做什么"。"""
    from cloudops.cloud import available_provider_names

    print(f"● 版本      : {__project__} {__version__} ({__homepage__})")
    print(f"● Python    : {sys.version.split()[0]}")
    print(f"● 配置文件  : {settings.env_file if settings.env_file else '(仅环境变量)'}")

    failed = 0
    problems = settings.missing_requirements()
    if problems:
        failed += 1
        print("✗ 配置      : 不完整")
        for item in problems:
            print(f"    • {item}")
    else:
        print("✓ 配置      : Telegram Token / 管理员 ID / SECRET_KEY 均已设置")

    settings.ensure_directories()
    database = Database(settings.database_path)
    try:
        database.initialize()
        stats = database.stats()
        print(f"✓ 数据库    : {settings.database_path}（表 {len(database.tables())} 张，"
              f"用户 {stats.users}，凭证 {stats.credentials}，"
              f"操作 {stats.operations}）")
    except Exception as exc:
        failed += 1
        print(f"✗ 数据库    : {exc}")
        return 1

    print(f"● 云平台    : {', '.join(available_provider_names(settings))}")
    if args.check_cloud:
        failed += check_cloud(database, settings)

    if failed:
        print("\n体检未通过，请先处理上面标 ✗ 的项。")
        return 1
    print("\n体检通过：python main.py 即可启动。")
    return 0


def check_cloud(database: Database, settings: Settings) -> int:
    """对每条已绑定凭证做一次真实 API 调用（``--check-cloud`` 才会跑）。"""
    from cloudops.cloud import create_provider
    from cloudops.crypto import SecretBox

    box = SecretBox(settings.secret_key)
    rows = database.list_credentials()
    if not rows:
        print("● 云凭证    : 尚未绑定（可用 /bind 或 .env 预置）")
        return 0
    failed = 0
    for row in rows:
        try:
            secrets = box.decrypt_dict(row.secret_blob)
            provider = create_provider(row.provider, secrets, settings, label=row.label)
            identity = provider.validate_credentials()
            provider.close()
            print(f"✓ 云凭证    : #{row.id} {row.provider}:{row.label} → {identity}")
        except Exception as exc:
            failed += 1
            print(f"✗ 云凭证    : #{row.id} {row.provider}:{row.label} → {exc}")
    return failed


def cmd_init_db(_args, settings: Settings) -> int:
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    print(f"✓ 数据库已就绪：{settings.database_path}")
    print("  表：" + ", ".join(database.tables()))
    return 0


def cmd_users(_args, settings: Settings) -> int:
    database = _open(settings)
    rows = database.list_users(limit=200)
    if not rows:
        print("（暂无用户。用户首次与 Bot 对话时会自动登记为 guest）")
        return 0
    print(f"{'Telegram ID':>14}  {'角色':<8} {'用户名':<20} 最近活跃")
    for row in rows:
        print(f"{row.telegram_id:>14}  {row.role:<8} {(row.username or '-'):<20} "
              f"{row.last_seen_at or '-'}")
    print(f"\n共 {len(rows)} 人（管理员 {', '.join(str(i) for i in settings.admin_ids) or '-'}）")
    return 0


def _set_role(telegram_id: int, role: str, settings: Settings) -> int:
    database = _open(settings)
    user = database.ensure_user(telegram_id, role=role)
    user = database.set_role(telegram_id, role)
    print(f"✓ {telegram_id}（{user.username or '未知用户名'}）的角色已设为 {user.role}")
    if role == "admin" and str(telegram_id) not in {str(i) for i in settings.admin_ids}:
        print(f"  提醒：也把 {telegram_id} 加进 .env 的 BOT_ADMIN_IDS，"
              "否则重启后菜单/白名单会与数据库不一致。")
    return 0


def cmd_promote(args, settings: Settings) -> int:
    return _set_role(args.telegram_id, "admin", settings)


def cmd_demote(args, settings: Settings) -> int:
    return _set_role(args.telegram_id, "user", settings)


def cmd_revoke(args, settings: Settings) -> int:
    return _set_role(args.telegram_id, "guest", settings)


def cmd_creds(_args, settings: Settings) -> int:
    from cloudops.crypto import SecretBox

    database = _open(settings)
    rows = database.list_credentials()
    if not rows:
        print("（尚未绑定任何云凭证：在群里发送 /bind 或写入 .env 预置）")
        return 0
    box = SecretBox(settings.secret_key)
    print(f"{'ID':>4}  {'云平台':<14} {'名称':<16} {'所有者':>14}  启用  字段")
    for row in rows:
        try:
            fields = ", ".join(sorted(box.decrypt_dict(row.secret_blob)))
        except Exception:
            fields = "(无法解密：SECRET_KEY 可能已更换)"
        print(f"{row.id:>4}  {row.provider:<14} {row.label:<16} {row.owner_id:>14}  "
              f"{'▶' if row.is_active else ' ':<4}  {fields}")
    return 0


def cmd_stats(_args, settings: Settings) -> int:
    database = _open(settings)
    stats = database.stats()
    print(f"用户        : {stats.users}")
    print(f"凭证        : {stats.credentials}")
    print(f"操作日志    : {stats.operations}")
    print(f"任务        : {stats.tasks}（进行中 {database.count_tasks(active_only=True)}）")
    print(f"跟踪实例    : {stats.instances_tracked}")
    for provider, count in sorted(database.provider_credential_counts().items()):
        print(f"  · {provider:<14} {count} 条凭证")
    return 0


def cmd_vacuum(_args, settings: Settings) -> int:
    database = _open(settings)
    before = settings.database_path.stat().st_size if settings.database_path.exists() else 0
    database.vacuum()
    after = settings.database_path.stat().st_size if settings.database_path.exists() else 0
    print(f"✓ 整理完成：{_human_size(before)} → {_human_size(after)}")
    return 0


# --------------------------------------------------------------------------- #
def _human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
        size //= 1024
    return f"{size} B"


def _open(settings: Settings) -> Database:
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    return database


def _telegram_id(value: str) -> int:
    token = value.strip().lstrip("@")
    if not token.lstrip("-").isdigit():
        raise argparse.ArgumentTypeError(f"不是合法的 Telegram 数字 ID：{value}")
    return int(token)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python manage.py",
        description=f"{__project__} 运维工具（配置来自 .env）",
        epilog=f"项目主页：{__homepage__}",
    )
    parser.add_argument("-e", "--env-file", default=DEFAULT_ENV_FILE,
                        help="配置文件路径（默认 .env）")
    parser.add_argument("--version", action="version",
                        version=f"{__project__} {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<命令>")

    secret = sub.add_parser("gen-secret", help="生成 SECRET_KEY")
    secret.add_argument("-w", "--write", nargs="?", const="", default=None, metavar="文件",
                        help="直接把密钥写入配置文件（默认取 -e 指定的文件），而不是只打印")
    secret.set_defaults(func=cmd_gen_secret)

    doctor = sub.add_parser("doctor", help="体检：配置 / 数据库 / 云凭证")
    doctor.add_argument("--check-cloud", action="store_true",
                        help="对每条已绑定凭证做一次真实 API 调用（需要联网）")
    doctor.set_defaults(func=cmd_doctor)

    sub.add_parser("init-db", help="建库建表（幂等）").set_defaults(func=cmd_init_db)
    sub.add_parser("users", help="列出已授权用户").set_defaults(func=cmd_users)
    sub.add_parser("creds", help="列出已绑定凭证（字段名脱敏）").set_defaults(func=cmd_creds)
    sub.add_parser("stats", help="账号与操作统计").set_defaults(func=cmd_stats)
    sub.add_parser("vacuum", help="整理数据库文件").set_defaults(func=cmd_vacuum)

    for name, func, help_text in (
        ("promote", cmd_promote, "把某个 Telegram ID 提升为管理员"),
        ("demote", cmd_demote, "把某个 Telegram ID 降为普通用户"),
        ("revoke", cmd_revoke, "撤销某个 Telegram ID 的授权"),
    ):
        node = sub.add_parser(name, help=help_text)
        node.add_argument("telegram_id", type=_telegram_id, help="Telegram 数字 ID")
        node.set_defaults(func=func)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 1

    # gen-secret 不需要读 .env（甚至在没有 .env 的新机器上也要能跑）
    if args.command == "gen-secret":
        return args.func(args, None)

    try:
        settings = Settings.from_env(args.env_file)
    except CloudOpsError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2
    try:
        return args.func(args, settings)
    except CloudOpsError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
