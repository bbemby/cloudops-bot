"""基础指令：``/start`` ``/help`` ``/whoami`` ``/ping``。"""

from __future__ import annotations

import time
from typing import Any, Dict, List

from ..bot import Context, GROUP_OTHER, GROUP_READ
from ..bot.formatter import format_credential_fields
from ..cloud import available_provider_names
from ..models import ROLE_GUEST

GROUP_ORDER = ("read", "write", "cred", "other")


def register(dispatcher) -> None:
    @dispatcher.command(
        "start",
        summary={"zh": "查看欢迎信息与快速上手示例", "en": "Welcome message and quick start"},
        group=GROUP_OTHER,
        hidden=True,
    )
    async def start(ctx: Context, args: List[str]) -> None:
        await ctx.reply(_greeting(ctx))

    @dispatcher.command(
        "help",
        summary={"zh": "列出全部可用指令（按身份过滤）", "en": "List available commands"},
        group=GROUP_OTHER,
    )
    async def help_command(ctx: Context, args: List[str]) -> None:
        await ctx.reply(render_help(ctx))

    @dispatcher.command(
        "whoami",
        summary={"zh": "查看你的身份、权限与可用凭证", "en": "Show your identity and credentials"},
        group=GROUP_READ,
    )
    async def whoami(ctx: Context, args: List[str]) -> None:
        t = ctx.t
        rows = [
            t("whoami.line_id", user_id=ctx.user_id),
            t("whoami.line_username", username=ctx.user.username or "-"),
            t("whoami.line_role", role=ctx.user.role_label),
            t("whoami.line_since", created=ctx.user.created_at or "-"),
        ]
        credentials = ctx.store.list_for_user(ctx.user_id)
        if credentials:
            summary = ", ".join(f"{item.provider}:{item.label}#{item.id}" for item in credentials)
        else:
            summary = t("whoami.creds_none")
        rows.append(t("whoami.line_creds", credentials=summary))
        await ctx.reply(t("whoami.title") + "\n" + "\n".join(rows))

    @dispatcher.command(
        "ping",
        summary={"zh": "存活检测与运行状态", "en": "Health check"},
        group=GROUP_OTHER,
    )
    async def ping(ctx: Context, args: List[str]) -> None:
        t = ctx.t
        started = time.monotonic()
        stats = ctx.db.stats()
        await ctx.reply(t(
            "ping.pong",
            uptime=ctx.dispatcher.uptime(),
            version=ctx.dispatcher.version(),
            db=f"{stats.users} users / {stats.credentials} creds / {stats.operations} ops",
            tasks=ctx.jobs.active,
        ) + f"\nlatency: {(time.monotonic() - started) * 1000:.0f}ms")


def _greeting(ctx: Context) -> str:
    t = ctx.t
    if ctx.user.role == ROLE_GUEST:
        return t("denied.guest", user_id=ctx.user_id)
    lines = [
        t("help.title"),
        "",
        t("help.intro"),
        "",
        t("help.role_hint", role=ctx.user.role_label, user_id=ctx.user_id),
        "",
        t("help.footer"),
    ]
    return "\n".join(lines)


def render_help(ctx: Context) -> str:
    """按分组渲染指令清单；guest 只看到"无权限"提示。"""
    t = ctx.t
    if ctx.user.role == ROLE_GUEST:
        return t("denied.guest", user_id=ctx.user_id)

    groups: Dict[str, List[str]] = {}
    for spec in ctx.dispatcher.specs():
        if spec.hidden:
            continue
        if spec.admin_only and not ctx.user.is_admin:
            continue
        line = f"/{spec.name}"
        if spec.aliases:
            line += "（" + "/".join(spec.aliases) + "）"
        line += f"\n    {spec.describe(ctx.lang)}"
        if spec.usage:
            line += f"\n    用法：{spec.usage}"
        groups.setdefault(spec.group, []).append(line)

    titles = {
        "read": t("help.group.read"),
        "write": t("help.group.write"),
        "cred": t("help.group.cred"),
        "other": t("help.group.other"),
    }
    sections = [
        t("help.title"),
        t("help.role_hint", role=ctx.user.role_label, user_id=ctx.user_id),
    ]
    for group in GROUP_ORDER:
        lines = groups.get(group)
        if not lines:
            continue
        sections.append(titles.get(group, group) + "\n" + "\n".join(lines))
    if not any(groups.get(group) for group in GROUP_ORDER):  # pragma: no cover
        sections.append(t("help.no_commands"))
    sections.append(t("help.footer"))
    return "\n\n".join(sections)


def provider_overview(ctx: Context) -> str:
    """``/providers`` 与 ``/help`` 共用的 provider 概览。"""
    names = available_provider_names(ctx.settings)
    return ", ".join(names)
