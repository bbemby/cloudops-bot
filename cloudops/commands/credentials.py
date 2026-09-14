"""凭证与工作区管理：``/bind`` ``/creds`` ``/switch`` ``/unbind`` ``/providers``。

流程严格遵循论文 4.3 的用例"绑定 API 密钥：管理员向系统提交新的云平台 Token，
系统对其进行**验证**与存储"——先调云端校验，再加密落库。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ..bot import Context, GROUP_CRED, GROUP_READ, confirm_keyboard
from ..bot.formatter import format_credential_row
from ..cloud import (
    available_provider_names,
    canonical_name,
    get_provider_class,
    is_known,
    provider_help,
    create_provider,
)
from ..errors import CloudOpsError, CredentialError, NotFound, ValidationError
from ..utils import mask_mapping

#: 字段简写 → 规范字段名（减少手机端输入量）
FIELD_ALIASES: Dict[str, str] = {
    "ak": "access_key_id",
    "accesskey": "access_key_id",
    "access_key": "access_key_id",
    "accesskeyid": "access_key_id",
    "sk": "secret_access_key",
    "secret": "secret_access_key",
    "secretkey": "secret_access_key",
    "token": "token",
    "key": "token",
    "apitoken": "token",
    "st": "session_token",
    "session": "session_token",
    "region": "region",
    "regions": "regions",
    "delay": "delay",
}

#: 位置参数顺序（``/bind aws prod AKIAxxx secret region``）
POSITIONAL_FIELDS: Dict[str, Tuple[str, ...]] = {
    "digitalocean": ("token", "region"),
    "aws": ("access_key_id", "secret_access_key", "session_token", "region"),
    "mock": ("token", "region", "delay"),
}


def register(dispatcher) -> None:
    @dispatcher.command(
        "bind",
        summary={"zh": "绑定并校验云平台凭证（加密存储）", "en": "Bind and validate a cloud credential"},
        group=GROUP_CRED,
        admin_only=True,
        usage="/bind <provider> <名称> <字段>=<值> …",
    )
    async def bind(ctx: Context, args: List[str]) -> None:
        t = ctx.t
        if len(args) < 2:
            raise ValidationError(key="bind.usage")

        provider_name, label, payload = _parse_bind_args(args)
        canonical = canonical_name(provider_name)
        if canonical not in available_provider_names(ctx.settings):
            raise ValidationError(
                params={"value": provider_name, "options": ", ".join(available_provider_names(ctx.settings))},
                key="bind.unknown_provider",
            )

        cls = get_provider_class(canonical)
        known_fields = set(cls.field_names())
        unknown = [key for key in payload if key not in known_fields]
        if unknown:
            raise ValidationError(
                params={"value": ", ".join(unknown),
                        "provider": cls.display_name,
                        "options": ", ".join(sorted(known_fields))},
                key="create.invalid_option",
            )
        missing = [field for field in cls.field_names()
                   if field not in payload and field in _required_fields(cls)]
        if missing:
            raise ValidationError(
                params={"fields": ", ".join(missing), "usage": cls.credential_usage()},
                key="bind.missing_fields",
            )

        await ctx.reply(t("bind.validating", provider=cls.display_name))
        identity, resolved = await _validate(ctx, canonical, label, payload)

        credential = ctx.store.bind(
            ctx.user_id, canonical, label, resolved, shared=True, activate=True
        )
        if identity:
            try:
                credential = ctx.store.update_meta(credential, {"identity": identity})
            except Exception:  # pragma: no cover - 元数据写入失败不影响主流程
                pass
        summary = ", ".join(
            f"{key}={mask_mapping(resolved, cls.secret_field_names()).get(key, resolved[key])}"
            for key in resolved
        )
        ctx.log("bind", provider=canonical, target=f"{canonical}:{label}",
                detail=f"credential#{credential.id} {identity}")
        await ctx.reply(t(
            "bind.success",
            credential_id=credential.id,
            provider=cls.display_name,
            label=label,
            identity=identity or "-",
            summary=summary,
        ))

    @dispatcher.command(
        "creds",
        summary={"zh": "列出已绑定的凭证（密钥脱敏）", "en": "List bound credentials (masked)"},
        group=GROUP_READ,
        aliases=("credentials",),
    )
    async def creds(ctx: Context, args: List[str]) -> None:
        t = ctx.t
        provider = canonical_name(args[0]) if args else None
        rows = ctx.store.describe_credentials(ctx.user_id)
        if provider:
            rows = [row for row in rows if row["credential"].provider == provider]
        if not rows:
            usage = ctx.store.bind_usage(provider or ctx.settings.default_provider)
            await ctx.reply(t("creds.none", usage=usage))
            return
        rendered = [format_credential_row(row, t, timezone_name=ctx.settings.display_timezone)
                    for row in rows]
        await ctx.reply(
            t("creds.title") + "\n\n" + "\n".join(rendered)
            + "\n\n" + t("creds.footer", count=len(rendered))
        )

    @dispatcher.command(
        "switch",
        summary={"zh": "切换当前生效的云平台工作区", "en": "Switch the active credential"},
        group=GROUP_CRED,
        admin_only=True,
        usage="/switch <凭证ID>",
    )
    async def switch(ctx: Context, args: List[str]) -> None:
        t = ctx.t
        if not args or not args[0].isdigit():
            raise ValidationError(key="switch.usage")
        credential_id = int(args[0])
        credential = await ctx.jobs.to_thread(ctx.store.activate, credential_id,
                                              owner_id=ctx.user_id)
        ctx.log("switch", provider=credential.provider, target=f"#{credential.id}")
        await ctx.reply(t("switch.done", provider=credential.provider, label=credential.label,
                          credential_id=credential.id))

    @dispatcher.command(
        "unbind",
        summary={"zh": "删除凭证（需二次确认）", "en": "Delete a credential (confirmed)"},
        group=GROUP_CRED,
        admin_only=True,
        usage="/unbind <凭证ID>",
    )
    async def unbind(ctx: Context, args: List[str]) -> None:
        t = ctx.t
        if not args or not args[0].isdigit():
            raise ValidationError(key="unbind.usage")
        credential = ctx.store.credential_by_id(int(args[0]))
        if credential is None or credential.owner_id != ctx.user_id:
            raise NotFound(params={"target": f"credential#{args[0]}"}, key="error.credential_not_found")

        async def do_unbind(confirm_ctx: Context) -> None:
            removed = await confirm_ctx.jobs.to_thread(confirm_ctx.store.delete,
                                                       credential.id, owner_id=confirm_ctx.user_id)
            confirm_ctx.log("unbind", provider=removed.provider, target=f"#{removed.id}")
            await confirm_ctx.reply(confirm_ctx.t("unbind.done", credential_id=removed.id,
                                                  provider=removed.provider, label=removed.label))

        pending = ctx.confirms.create(
            user_id=ctx.user_id,
            chat_id=ctx.chat_id,
            action=f"unbind #{credential.id} ({credential.provider}:{credential.label})",
            runner=do_unbind,
            data={"credential_id": credential.id},
        )
        await ctx.reply(
            t("unbind.pending", credential_id=credential.id, provider=credential.provider,
              label=credential.label)
            + "\n\n" + t("confirm.required", code=pending.code, ttl=int(pending.ttl)),
            reply_markup=confirm_keyboard(t, pending.code),
        )
        ctx.log("unbind", status="pending", provider=credential.provider,
                target=f"#{credential.id}", detail="awaiting confirmation")

    @dispatcher.command(
        "providers",
        summary={"zh": "查看支持的云平台与其字段要求", "en": "Show supported providers"},
        group=GROUP_READ,
    )
    async def providers(ctx: Context, args: List[str]) -> None:
        t = ctx.t
        if args:
            name = canonical_name(args[0])
            if name not in available_provider_names(ctx.settings):
                raise ValidationError(
                    params={"value": args[0], "options": ", ".join(available_provider_names(ctx.settings))},
                    key="bind.unknown_provider",
                )
            cls = get_provider_class(name)
            sections = [provider_help(name)]
            credential = ctx.store.active_for(name, ctx.user_id)
            if credential is not None:
                try:
                    provider = ctx.store.provider_from_credential(credential)
                    sections.append(await ctx.jobs.to_thread(provider.describe_options))
                except CloudOpsError as exc:
                    sections.append(f"（无法获取目录：{exc}）")
            sections.append("字段说明：\n" + cls.credential_usage())
            await ctx.reply("\n\n".join(sections))
            return

        bound = set(ctx.store.bound_providers(ctx.user_id))
        lines = [t("providers.title"), ""]
        for name in available_provider_names(ctx.settings):
            cls = get_provider_class(name)
            marker = t("providers.bound") if name in bound else t("providers.unbound")
            lines.append(t("providers.row", marker=marker, name=name, display=cls.display_name))
        lines.append("")
        lines.append(t("providers.hint"))
        await ctx.reply("\n".join(lines))


# --------------------------------------------------------------------------- #
# 参数解析与校验
# --------------------------------------------------------------------------- #
def _parse_bind_args(args: List[str]) -> Tuple[str, str, Dict[str, Any]]:
    """支持两种写法：``key=value`` 与按位置补齐。"""
    provider_name = args[0]
    label = args[1]
    rest = args[2:]
    canonical = canonical_name(provider_name)
    positional_fields = POSITIONAL_FIELDS.get(canonical, ())

    payload: Dict[str, Any] = {}
    positional_index = 0
    for token in rest:
        if "=" in token and not token.startswith("="):
            raw_key, value = token.split("=", 1)
            key = FIELD_ALIASES.get(raw_key.strip().lower(), raw_key.strip().lower())
            payload[key] = value
            continue
        if positional_index < len(positional_fields):
            payload[positional_fields[positional_index]] = token
            positional_index += 1
    return provider_name, label, payload


def _required_fields(cls) -> Tuple[str, ...]:
    return tuple(field.name for field in cls.credential_fields if field.required)


async def _validate(ctx: Context, canonical: str, label: str, payload: Dict[str, Any]
                    ) -> Tuple[str, Dict[str, Any]]:
    """构造临时适配器并调用云端校验接口；失败时抛出用户可读错误。"""
    resolved = {key: value for key, value in payload.items() if value not in (None, "")}
    provider = create_provider(canonical, resolved, ctx.settings, label=label)
    try:
        identity = await ctx.jobs.to_thread(provider.validate_credentials)
    except CredentialError as exc:
        raise CredentialError(params={"provider": provider.display_name, "detail": exc.render(ctx.t)},
                              key="bind.failed") from exc
    except CloudOpsError as exc:
        raise CredentialError(params={"provider": provider.display_name, "detail": str(exc)},
                              key="bind.failed") from exc
    finally:
        provider.close()
    return identity, resolved
