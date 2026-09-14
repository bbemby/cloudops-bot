"""只读查询指令：``/list_ip``（含 /list、/ip 别名）与 ``/status``。

对应论文 TC-01：机器人遍历已绑定的多云接口，汇总公网 IP 后一次性返回。
多个 provider 的查询通过线程池并发执行，避免串行等待拖慢响应。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple

from ..bot import Context, GROUP_READ
from ..bot.formatter import format_instance_detail, format_instance_line
from ..cloud import Instance, available_provider_names, canonical_name, is_known
from ..credentials import CredentialStore
from ..errors import CredentialError, CloudError, NotFound, ValidationError
from ..models import Credential
from ..utils import humanize_duration


def register(dispatcher) -> None:
    @dispatcher.command(
        "list",
        summary={"zh": "汇总所有云平台的实例与公网 IP", "en": "List instances and public IPs"},
        group=GROUP_READ,
        aliases=("list_ip", "ip", "servers", "instances"),
        usage="/list [provider]",
    )
    async def list_instances(ctx: Context, args: List[str]) -> None:
        t = ctx.t
        provider_filter = _provider_filter(ctx, args[0] if args else None)
        targets = [provider_filter] if provider_filter else ctx.store.bound_providers(ctx.user_id)
        if not targets:
            usage = ctx.store.bind_usage(ctx.settings.default_provider)
            raise CredentialError(params={"provider": ctx.settings.default_provider, "usage": usage},
                                  key="error.credential_missing")

        started = time.monotonic()
        results = await asyncio.gather(*[_fetch(ctx, name) for name in targets])
        elapsed = humanize_duration(time.monotonic() - started)

        instances: List[Instance] = []
        warnings: List[str] = []
        failures: List[Tuple[str, str]] = []
        for name, items, warning_list, error in results:
            if error is not None:
                failures.append((name, error))
                continue
            instances.extend(items)
            warnings.extend(warning_list)

        scope = provider_filter or "多云"
        sections: List[str] = []
        if instances:
            lines = [format_instance_line(index, instance, t,
                                          timezone_name=ctx.settings.display_timezone)
                     for index, instance in enumerate(instances, start=1)]
            sections.append(t("list.header", scope=scope, count=len(instances)) + "\n\n" + "\n".join(lines))
            running = len([item for item in instances if item.status == "running"])
            sections.append(t("list.summary", count=len(instances), running=running, elapsed=elapsed))
        elif not failures:
            sections.append(t("list.empty", scope=scope))

        for name, detail in failures:
            sections.append(t("list.provider_error", provider=name, detail=detail))
        for warning in warnings:
            sections.append(t("list.warning", warning=warning))

        ctx.log("list", provider=provider_filter, target=scope,
                detail=f"{len(instances)} instances, failures={len(failures)}")
        await ctx.reply("\n\n".join(sections))

    @dispatcher.command(
        "status",
        summary={"zh": "查看单台实例详情（ID 或 IP）", "en": "Show one instance detail"},
        group=GROUP_READ,
        usage="/status <provider> <实例ID或IP>",
    )
    async def status(ctx: Context, args: List[str]) -> None:
        t = ctx.t
        if not args:
            raise ValidationError(key="status.usage")
        provider_name, reference = _split_target(ctx, args)
        provider, credential = await _provider(ctx, provider_name)
        instance = await ctx.jobs.to_thread(_lookup, provider, reference)
        if instance is None:
            raise NotFound(params={"target": reference, "provider": provider.display_name},
                           key="error.instance_not_found")
        ctx.log("status", provider=provider_name, target=instance.id)
        await ctx.reply(
            t("status.title", provider=provider.display_name) + "\n"
            + format_instance_detail(instance, t, timezone_name=ctx.settings.display_timezone)
            + f"\ncredential: {credential.provider}:{credential.label}"
        )


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _lookup(provider, reference: str) -> Optional[Instance]:
    """按 ID 或 IP 查找；找不到返回 None（由调用方决定错误文案）。"""
    try:
        found = provider.find_instance(reference)
    except (CloudError, NotFound):
        found = None
    if found is not None:
        return found
    try:
        return provider.get_instance(reference)
    except Exception:
        return None


def _provider_filter(ctx: Context, token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    name = canonical_name(token)
    if name not in available_provider_names(ctx.settings):
        raise ValidationError(
            params={"value": token, "parameter": "云平台",
                    "options": ", ".join(available_provider_names(ctx.settings))},
            key="error.unknown_option",
        )
    return name


def _split_target(ctx: Context, args: List[str]) -> Tuple[str, str]:
    """``/status do 1.2.3.4`` 与 ``/status 1.2.3.4`` 都支持。"""
    if len(args) >= 2 and is_known(args[0]):
        return canonical_name(args[0]), args[1]
    return ctx.settings.default_provider, args[0]


async def _fetch(ctx: Context, provider_name: str
                 ) -> Tuple[str, List[Instance], List[str], Optional[str]]:
    """并发执行体：拉取某 provider 的实例列表，异常转成文本交给渲染层。"""
    try:
        provider, _credential = await _provider(ctx, provider_name)
    except Exception as exc:
        return provider_name, [], [], str(exc)
    try:
        instances = await ctx.jobs.to_thread(provider.list_instances)
    except Exception as exc:
        return provider_name, [], [], str(exc)
    warnings = list(getattr(provider, "warnings", []) or [])
    return provider_name, instances, warnings, None


async def _provider(ctx: Context, provider_name: str):
    return ctx.store.provider_with_credential(provider_name, ctx.user_id)
