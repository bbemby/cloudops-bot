"""指令处理器共用的小工具。"""

from __future__ import annotations

from typing import Any, Optional, Tuple

from ..bot import Context
from ..cloud import CloudProvider
from ..models import Credential


def obtain_provider(ctx: Context, provider_name: str, label: Optional[str] = None
                    ) -> Tuple[CloudProvider, Credential]:
    """按"用户 + 云平台（+ 可选凭证名）"装配适配器。

    找不到凭证时会抛出带 **下一步操作提示** 的 :class:`CredentialError`。
    """
    return ctx.store.provider_with_credential(provider_name, ctx.user_id, label=label)


def provider_for_credential(ctx: Context, credential: Credential) -> CloudProvider:
    """由凭证记录直接得到适配器（后台任务里常用：凭证在任务创建时已确定）。"""
    return ctx.store.provider_from_credential(credential)


def display_provider_name(ctx: Context, provider_name: str) -> str:
    """规范名 → 展示名（拿不到适配器时退回规范名）。"""
    try:
        from ..cloud import get_provider_class

        return get_provider_class(provider_name).display_name
    except KeyError:  # pragma: no cover
        return provider_name


def safe_lookup(provider: CloudProvider, target: str) -> Optional[Any]:
    """按 ID/IP 定位实例，任何异常都降级为 None。"""
    try:
        found = provider.find_instance(target)
    except Exception:
        found = None
    if found is not None:
        return found
    try:
        return provider.get_instance(target)
    except Exception:
        return None
