"""多云适配层（论文 5.1 的"数据与接口层"）。

对外暴露统一的工厂入口 :func:`create_provider`，以及内置适配器
（DigitalOcean / AWS EC2 / Mock Cloud）的注册表。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

from .aws_ec2 import AwsEc2Provider
from .base import CloudProvider, CreateSpec, CredentialField, Instance
from .digitalocean import DigitalOceanProvider
from .http import HttpClient
from .mock import MockProvider
from .registry import (
    alias_map,
    canonical_name,
    create_provider,
    get_provider_class,
    is_known,
    provider_classes,
    provider_names,
    providers_summary,
    register_provider,
)

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Settings

__all__ = [
    "CloudProvider",
    "CreateSpec",
    "CredentialField",
    "Instance",
    "HttpClient",
    "DigitalOceanProvider",
    "AwsEc2Provider",
    "MockProvider",
    "create_provider",
    "get_provider_class",
    "register_provider",
    "provider_names",
    "provider_classes",
    "providers_summary",
    "canonical_name",
    "is_known",
    "alias_map",
    "available_provider_names",
    "provider_help",
]


def available_provider_names(settings: Optional["Settings"] = None) -> List[str]:
    """对外可见的 provider 列表；``ENABLE_MOCK_PROVIDER=false`` 时隐藏演示云。"""
    names = provider_names()
    if settings is not None and not getattr(settings, "enable_mock_provider", False):
        names = [name for name in names if name != "mock"]
    return names


def provider_help(name: str) -> str:
    """渲染某个 provider 的说明（用于 ``/providers <name>``）。"""
    cls = get_provider_class(name)
    lines = [cls.display_name]
    for field in cls.credential_fields:
        lines.append(f"  • {field.usage()}")
    return "\n".join(lines)
