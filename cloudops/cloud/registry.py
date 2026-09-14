"""适配器工厂（论文 5.1：调度引擎内部实现工厂模式）。

新增一个云平台只需要：

1. 继承 :class:`~cloudops.cloud.base.CloudProvider`；
2. 用 ``@register_provider`` 装饰；
3. 在 ``cloudops/cloud/__init__.py`` 里 import 一次。

其余（指令解析、权限、日志、等待就绪、消息渲染）全部自动生效。
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple, Type

from .base import CloudProvider

_REGISTRY: Dict[str, Type[CloudProvider]] = {}
_ALIASES: Dict[str, str] = {}


def register_provider(cls: Type[CloudProvider]) -> Type[CloudProvider]:
    """注册适配器；重复注册同名适配器会直接报错，避免静默覆盖。"""
    name = getattr(cls, "name", "").strip().lower()
    if not name or name == "base":
        raise ValueError(f"{cls.__name__} 未声明有效的 name")
    if name in _REGISTRY and _REGISTRY[name] is not cls:
        raise ValueError(f"provider 名称冲突：{name}")
    _REGISTRY[name] = cls
    for alias in getattr(cls, "aliases", ()) or ():
        key = str(alias).strip().lower()
        if key and key != name and key not in _REGISTRY:
            _ALIASES[key] = name
    return cls


def canonical_name(name: str) -> str:
    """把别名（``do``）归一为规范名（``digitalocean``）。"""
    key = (name or "").strip().lower()
    if key in _REGISTRY:
        return key
    return _ALIASES.get(key, key)


def get_provider_class(name: str) -> Type[CloudProvider]:
    key = canonical_name(name)
    if key not in _REGISTRY:
        raise KeyError(key)
    return _REGISTRY[key]


def is_known(name: str) -> bool:
    return canonical_name(name) in _REGISTRY


def provider_names() -> List[str]:
    return sorted(_REGISTRY)


def provider_classes() -> Dict[str, Type[CloudProvider]]:
    return dict(_REGISTRY)


def alias_map() -> Dict[str, str]:
    return dict(_ALIASES)


def create_provider(name: str, secrets: Mapping[str, Any], settings: Any, *,
                    label: str = "default", meta: Optional[Mapping[str, Any]] = None) -> CloudProvider:
    """工厂入口：按名称实例化适配器。"""
    cls = get_provider_class(name)
    return cls(secrets, settings, label=label, meta=meta)


def providers_summary() -> List[Tuple[str, str]]:
    """(规范名, 展示名) 列表，供帮助文案使用。"""
    return [(name, cls.display_name) for name, cls in sorted(_REGISTRY.items())]
