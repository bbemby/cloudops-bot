"""消息渲染（交互层的"最后一公里"）。

原则：全部使用纯文本 + emoji，不依赖 Markdown/HTML 解析，
避免用户数据里的特殊字符把 Telegram 的解析器搞崩（少一类线上事故）。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from ..cloud import Instance
from ..models import Credential, OperationLog, TaskRecord
from ..utils import format_local, humanize_age, truncate

STATUS_EMOJI: Dict[str, str] = {
    "running": "🟢",
    "provisioning": "🟡",
    "stopped": "⚪️",
    "stopping": "🟠",
    "rebooting": "🔄",
    "terminated": "⚫️",
    "archived": "⚫️",
    "error": "🔴",
    "unknown": "❔",
}

RISK_STYLE: Dict[str, str] = {
    "success": "✅",
    "failed": "❌",
    "denied": "⛔️",
    "pending": "⏳",
    "running": "🚀",
    "cancelled": "✖️",
}


def status_emoji(status: Optional[str]) -> str:
    return STATUS_EMOJI.get(str(status or "unknown"), "❔")


def risk_emoji(status: Optional[str]) -> str:
    return RISK_STYLE.get(str(status or ""), "•")


# --------------------------------------------------------------------------- #
# 实例
# --------------------------------------------------------------------------- #
def format_instance_line(index: int, instance: Instance, t, *, show_password: bool = False,
                        timezone_name: Optional[str] = None) -> str:
    extra = ""
    if show_password and instance.password:
        extra = t("list.extra_password", password=instance.password)
    return t(
        "list.row",
        index=index,
        emoji=status_emoji(instance.status),
        name=instance.name or instance.id,
        provider=instance.provider,
        status=instance.status,
        region=instance.region,
        size=instance.size or "-",
        public_ip=instance.public_ip or instance.ipv6 or "-",
        extra=extra,
    )


def format_instance_detail(instance: Instance, t, *, timezone_name: Optional[str] = None) -> str:
    rows = [
        ("provider", instance.provider),
        ("id", instance.id),
        ("name", instance.name),
        ("status", f"{status_emoji(instance.status)} {instance.status}"),
        ("region", instance.region),
        ("size", instance.size or "-"),
        ("image", instance.image or "-"),
        ("public_ip", instance.public_ip or "-"),
        ("private_ip", instance.private_ip or "-"),
        ("ipv6", instance.ipv6 or "-"),
        ("created", format_local(instance.created_at, timezone_name) if instance.created_at else "-"),
        ("tags", ", ".join(instance.tags) or "-"),
    ]
    if instance.password:
        rows.append(("password", instance.password))
    return "\n".join(t("status.line", key=key, value=value) for key, value in rows)


def format_created_instance(instance: Instance, t) -> str:
    password = f"\n  初始密码: {instance.password}" if instance.password else ""
    return t(
        "create.done_item",
        name=instance.name or instance.id,
        provider=instance.provider,
        instance_id=instance.id,
        public_ip=instance.public_ip or instance.ipv6 or "-",
        region=instance.region,
        size=instance.size or "-",
        image=instance.image or "-",
        password=password,
    )


# --------------------------------------------------------------------------- #
# 凭证
# --------------------------------------------------------------------------- #
def format_credential_fields(masked: Dict[str, Any]) -> str:
    if not masked:
        return "-"
    return " · ".join(f"{key}={value}" for key, value in masked.items())


def format_credential_row(row: Dict[str, Any], t, *, timezone_name: Optional[str] = None,
                          marker: Optional[str] = None) -> str:
    credential: Credential = row["credential"]
    if marker is None:
        marker = t("creds.active_marker") if row.get("is_active") else t("creds.inactive_marker")
    return t(
        "creds.row",
        marker=marker,
        credential_id=credential.id,
        provider=credential.provider,
        label=credential.label,
        owner=row.get("owner"),
        created=format_local(credential.created_at, timezone_name),
        fields=format_credential_fields(row.get("masked") or {}),
    )


# --------------------------------------------------------------------------- #
# 日志 / 任务
# --------------------------------------------------------------------------- #
def format_log_row(log: OperationLog, t, *, timezone_name: Optional[str] = None) -> str:
    return t(
        "logs.row",
        time=format_local(log.created_at, timezone_name),
        status=f"{risk_emoji(log.status)}{log.status}",
        action=log.action,
        target=log.target or "",
        user=log.telegram_id,
    )


def format_task_row(task: TaskRecord, t, *, timezone_name: Optional[str] = None) -> str:
    name = (task.payload or {}).get("name")
    ref = task.ref or ""
    if name:
        # 让用户一眼看出"这条任务创建/销毁的是哪台机器"
        ref = f"{name} → {ref}" if ref and ref != name else str(name)
    return t(
        "tasks.row",
        time=humanize_age(task.created_at, timezone_name),
        status=f"{risk_emoji(task.status)}{task.status}",
        kind=task.kind,
        task_id=task.id,
        ref=ref,
        detail=truncate(task.detail, 60),
    )


# --------------------------------------------------------------------------- #
# 通用
# --------------------------------------------------------------------------- #
def bullet_list(lines: Iterable[str], limit: int = 50) -> str:
    return "\n".join(list(lines)[:limit])


def error_text(exc: BaseException, t) -> str:
    """把异常渲染成用户可读文案（CloudOpsError 走 i18n，其余走兜底）。"""
    from ..errors import CloudOpsError

    if isinstance(exc, CloudOpsError):
        rendered = exc.render(t)
        if rendered and rendered != exc.key:
            return rendered
        return t("error.generic", detail=str(exc))
    return t("error.internal", detail=type(exc).__name__)


def join_sections(sections: List[str]) -> str:
    return "\n\n".join(section for section in sections if section)
