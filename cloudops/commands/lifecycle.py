"""资源生命周期指令：``/create`` ``/delete`` ``/confirm`` ``/cancel`` ``/logs`` ``/tasks``。

关键设计（对应论文 5.2 的时序图与 4.3 的用例）：

* ``/create`` 立刻回一句"任务已下发"，随后由后台任务轮询云端，就绪后把公网 IP
  **主动推送**回聊天窗口；
* ``/delete`` 强制二次确认（确认码或内联按钮），确认后才真正调用销毁接口；
* 每一次调度都写入 operation_log，便于事后审计。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple

from ..bot import Context, GROUP_READ, GROUP_WRITE, confirm_keyboard
from ..bot.formatter import (
    format_created_instance,
    format_log_row,
    format_task_row,
)
from ..bot.telegram import TelegramClient
from ..cloud import CreateSpec, available_provider_names, canonical_name, is_known
from ..credentials import CredentialStore
from ..commands.common import display_provider_name, obtain_provider, safe_lookup
from ..db import Database
from ..errors import (
    CloudOpsError,
    CloudTimeout,
    NotFound,
    ValidationError,
)
from ..i18n import get_translator
from ..models import (
    LOG_FAILED,
    LOG_SUCCESS,
    TASK_FAILED,
    TASK_RUNNING,
    TASK_SUCCESS,
)
from ..utils import humanize_duration, random_code, random_suffix

#: 后台创建任务的心跳间隔（秒）：每这么久给用户推一条进度，避免长时间静默
PROGRESS_INTERVAL_SECONDS = 45.0

#: ``/create`` 支持的关键字参数（含简写）
CREATE_OPTION_ALIASES: Dict[str, str] = {
    "name": "name",
    "region": "region",
    "size": "size",
    "type": "size",
    "image": "image",
    "ami": "image",
    "count": "count",
    "tags": "tags",
    "ssh_keys": "ssh_keys",
    "sshkeys": "ssh_keys",
    "key": "key_name",
    "keyname": "key_name",
    "user_data": "user_data",
    "userdata": "user_data",
    "cred": "credential_label",
    "label": "credential_label",
}

CREATE_POSITIONAL = ("region", "size", "image")


def register(dispatcher) -> None:
    # ------------------------------------------------------------------ #
    @dispatcher.command(
        "create",
        summary={"zh": "一键创建云服务器（默认云平台可省略）", "en": "Create a cloud server"},
        group=GROUP_WRITE,
        admin_only=True,
        usage="/create [provider] [region] [size] [image] [key=value …]",
    )
    async def create(ctx: Context, args: List[str]) -> None:
        t = ctx.t
        if not args:
            raise ValidationError(key="create.usage")

        provider_name, options = _parse_create_args(ctx, args)
        provider, credential = obtain_provider(ctx, provider_name,
                                              options.get("credential_label"))
        spec, count = await _build_spec(ctx, provider, options)
        if count > ctx.settings.create_max_count:
            raise ValidationError(params={"limit": ctx.settings.create_max_count}, key="create.limit")

        task_id = random_code(8)
        ctx.db.create_task(
            task_id, ctx.user_id, provider_name, "create",
            payload={"spec": spec.resolved_summary(), "count": count,
                     "name": spec.name, "credential_id": credential.id},
        )
        await ctx.reply(t(
            "create.plan",
            provider=provider.display_name,
            count=count,
            summary=spec.resolved_summary(),
            credential=f"{credential.provider}:{credential.label}",
        ))
        ctx.log("create", status="pending", provider=provider_name, target=spec.name,
                detail=f"task={task_id} {spec.resolved_summary()}")

        ctx.jobs.spawn(
            _provision_job(
                bot=ctx.bot,
                db=ctx.db,
                store=ctx.store,
                settings=ctx.settings,
                chat_id=ctx.chat_id,
                user_id=ctx.user_id,
                provider_name=provider_name,
                credential_id=credential.id,
                spec=spec,
                task_id=task_id,
                lang=ctx.lang,
            ),
            label=f"create-{task_id}",
        )

    # ------------------------------------------------------------------ #
    @dispatcher.command(
        "delete",
        summary={"zh": "销毁云服务器（ID 或 IP，需二次确认）", "en": "Terminate a server (confirmed)"},
        group=GROUP_WRITE,
        admin_only=True,
        aliases=("destroy", "rm"),
        usage="/delete <实例ID或IP> [provider]",
    )
    async def delete(ctx: Context, args: List[str]) -> None:
        t = ctx.t
        if not args:
            raise ValidationError(key="delete.usage")

        matches = await _find_targets(ctx, args)
        if not matches:
            raise NotFound(params={"target": args[0]}, key="delete.not_found")
        if len(matches) > 1:
            options = ", ".join(f"{name}" for name, _cred, _instance in matches)
            raise ValidationError(
                params={"target": args[0], "options": options}, key="delete.ambiguous"
            )

        provider_name, credential, instance = matches[0]
        if instance.status in ("terminated",):
            raise ValidationError(params={"instance_id": instance.id}, key="delete.terminated")

        async def do_delete(confirm_ctx: Context) -> None:
            provider = confirm_ctx.store.provider_from_credential(credential)
            try:
                await confirm_ctx.jobs.to_thread(provider.destroy_instance, instance.id)
            except CloudOpsError as exc:
                confirm_ctx.log("delete", status=LOG_FAILED, provider=provider_name,
                                target=instance.id, detail=str(exc))
                await confirm_ctx.reply(confirm_ctx.t("delete.failed", detail=str(exc)))
                return
            confirm_ctx.log("delete", provider=provider_name, target=instance.id,
                            detail=f"{instance.name} {instance.public_ip or '-'}")
            await confirm_ctx.reply(confirm_ctx.t(
                "delete.done", provider=provider_name, name=instance.name or instance.id,
                instance_id=instance.id,
            ))

        pending = ctx.confirms.create(
            user_id=ctx.user_id,
            chat_id=ctx.chat_id,
            action=f"delete {instance.id}",
            runner=do_delete,
            data={"instance_id": instance.id, "provider": provider_name},
        )
        ctx.log("delete", status="pending", provider=provider_name, target=instance.id,
                detail="awaiting confirmation")
        await ctx.reply(
            t("delete.pending",
              provider=display_provider_name(ctx, provider_name),
              name=instance.name or instance.id,
              instance_id=instance.id,
              public_ip=instance.public_ip or instance.ipv6 or "-",
              region=instance.region,
              code=pending.code,
              ttl=int(pending.ttl)),
            reply_markup=confirm_keyboard(t, pending.code),
        )

    # ------------------------------------------------------------------ #
    @dispatcher.command(
        "confirm",
        summary={"zh": "执行待确认的危险操作", "en": "Execute a pending action"},
        group=GROUP_WRITE,
        admin_only=True,
        usage="/confirm <确认码>",
    )
    async def confirm(ctx: Context, args: List[str]) -> None:
        t = ctx.t
        if not args:
            pending = ctx.confirms.pending_for(ctx.user_id)
            if pending is None:
                raise ValidationError(key="confirm.usage")
            code = pending.code
        else:
            code = args[0]
        pending = ctx.confirms.consume(code, ctx.user_id)
        await ctx.reply(t("confirm.done", action=pending.action))
        await pending.runner(ctx)

    @dispatcher.command(
        "cancel",
        summary={"zh": "取消待确认的操作", "en": "Cancel a pending action"},
        group=GROUP_WRITE,
        admin_only=True,
        usage="/cancel <确认码>",
    )
    async def cancel(ctx: Context, args: List[str]) -> None:
        code = args[0] if args else None
        pending = ctx.confirms.pending_for(ctx.user_id) if not code else None
        target = code or (pending.code if pending else None)
        if target is None:
            raise ValidationError(key="confirm.usage")
        cancelled = ctx.confirms.cancel(target, ctx.user_id)
        ctx.log("cancel", status="cancelled", detail=cancelled.action)
        await ctx.reply(ctx.t("confirm.cancelled", action=cancelled.action))

    # ------------------------------------------------------------------ #
    @dispatcher.command(
        "logs",
        summary={"zh": "查看最近的操作审计日志", "en": "Recent operation logs"},
        group=GROUP_READ,
        usage="/logs [条数]",
    )
    async def logs(ctx: Context, args: List[str]) -> None:
        t = ctx.t
        limit = 10
        if args:
            if not args[0].isdigit():
                raise ValidationError(key="logs.usage")
            limit = max(1, min(50, int(args[0])))
        scope_user = None if ctx.is_admin else ctx.user_id
        rows = ctx.db.list_logs(limit, telegram_id=scope_user)
        if not rows:
            await ctx.reply(t("logs.empty"))
            return
        lines = [format_log_row(row, t, timezone_name=ctx.settings.display_timezone) for row in rows]
        await ctx.reply(t("logs.title", count=len(rows)) + "\n\n" + "\n".join(lines))

    @dispatcher.command(
        "tasks",
        summary={"zh": "查看后台任务（创建/销毁）状态", "en": "Background task status"},
        group=GROUP_READ,
    )
    async def tasks(ctx: Context, args: List[str]) -> None:
        t = ctx.t
        scope_user = None if ctx.is_admin else ctx.user_id
        rows = ctx.db.list_tasks(10, telegram_id=scope_user)
        if not rows:
            await ctx.reply(t("tasks.empty"))
            return
        lines = [format_task_row(row, t, timezone_name=ctx.settings.display_timezone) for row in rows]
        await ctx.reply(t("tasks.title") + "\n\n" + "\n".join(lines))


# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #
def _parse_create_args(ctx: Context, args: List[str]) -> Tuple[str, Dict[str, Any]]:
    positional: List[str] = []
    options: Dict[str, Any] = {}
    for token in args:
        if "=" in token and not token.startswith("="):
            raw_key, value = token.split("=", 1)
            key = CREATE_OPTION_ALIASES.get(raw_key.strip().lower())
            if key is None:
                raise ValidationError(
                    params={"option": raw_key, "provider": "create",
                            "options": ", ".join(sorted(set(CREATE_OPTION_ALIASES)))},
                    key="create.invalid_option",
                )
            options[key] = value
        else:
            positional.append(token)

    provider_name = ctx.settings.default_provider
    if positional and is_known(positional[0]):
        provider_name = canonical_name(positional.pop(0))
    if provider_name not in available_provider_names(ctx.settings):
        raise ValidationError(
            params={"value": provider_name, "parameter": "云平台",
                    "options": ", ".join(available_provider_names(ctx.settings))},
            key="error.unknown_option",
        )

    for index, field in enumerate(CREATE_POSITIONAL):
        if index >= len(positional):
            break
        if field in options:
            # 例如 `/create mock web-01 region=xxxxxxxx`：位置参数会被悄悄丢掉，
            # 这种"用户以为设了、其实没生效"的歧义必须显式报错。
            raise ValidationError(
                params={"option": positional[index], "provider": provider_name,
                        "options": f"{field} 已由 {field}=… 指定，请只保留一种写法"},
                key="create.invalid_option",
            )
        options[field] = positional[index]
    if len(positional) > len(CREATE_POSITIONAL):
        raise ValidationError(
            params={"option": " ".join(positional[len(CREATE_POSITIONAL):]),
                    "provider": provider_name,
                    "options": "region / size / image / key=value"},
            key="create.invalid_option",
        )
    return provider_name, options


async def _build_spec(ctx: Context, provider, options: Dict[str, Any]) -> Tuple[CreateSpec, int]:
    """把用户输入交给适配器归一化（区域别名、规格别名、AMI 解析…）。"""
    name = str(options.get("name") or f"{ctx.settings.default_instance_name}-{random_suffix(4)}")
    count = 1
    if "count" in options:
        raw_count = str(options["count"])
        if not raw_count.isdigit() or int(raw_count) < 1:
            raise ValidationError(params={"value": raw_count}, key="create.invalid_count")
        count = int(raw_count)

    spec = CreateSpec(
        name=name,
        region=await ctx.jobs.to_thread(provider.resolve_region, options.get("region")),
        size=await ctx.jobs.to_thread(provider.resolve_size, options.get("size")),
        image=await ctx.jobs.to_thread(provider.resolve_image, options.get("image")),
        count=count,
        tags=_split_list(options.get("tags")),
        ssh_keys=_split_list(options.get("ssh_keys")),
        key_name=options.get("key_name"),
        user_data=options.get("user_data"),
    )
    return spec, count


def _split_list(value: Any) -> List[str]:
    if not value:
        return []
    return [item.strip() for item in str(value).replace(";", ",").split(",") if item.strip()]


async def _find_targets(ctx: Context, args: List[str]
                        ) -> List[Tuple[str, Any, Any]]:
    """定位待销毁实例：返回 [(provider_name, credential, instance), …]。"""
    target = args[0]
    if len(args) > 1 and is_known(args[1]):
        candidates = [canonical_name(args[1])]
    else:
        candidates = ctx.store.bound_providers(ctx.user_id)

    matches: List[Tuple[str, Any, Any]] = []
    for provider_name in candidates:
        try:
            provider, credential = obtain_provider(ctx, provider_name)
        except CloudOpsError:
            continue
        instance = await ctx.jobs.to_thread(safe_lookup, provider, target)
        if instance is not None:
            matches.append((provider_name, credential, instance))
    return matches


# --------------------------------------------------------------------------- #
# 后台创建任务（论文 5.2 的轮询等待 + 主动推送）
# --------------------------------------------------------------------------- #
async def _provision_job(*, bot: TelegramClient, db: Database, store: CredentialStore,
                         settings, chat_id: int, user_id: int, provider_name: str,
                         credential_id: int, spec: CreateSpec, task_id: str,
                         lang: str) -> None:
    t = get_translator(lang)
    credential = db.get_credential(credential_id)
    if credential is None:  # pragma: no cover - 凭证在任务执行前被删除
        db.update_task(task_id, status=TASK_FAILED, detail="credential removed")
        await bot.send_message(chat_id, t("create.failed", task_id=task_id,
                                          detail="credential removed"))
        return

    provider = store.provider_from_credential(credential)
    started = time.monotonic()
    loop = asyncio.get_running_loop()
    last_report = started

    def report_progress(elapsed: float, current) -> None:
        """轮询等待期间的心跳推送（在 worker 线程里被调用，因此要回投到事件循环）。

        论文 5.2 的"Bot 进入轮询等待状态"如果让用户干等 10 分钟体验很差，
        这里每 ``PROGRESS_INTERVAL_SECONDS`` 推一条进度，让移动端用户知道
        "机器还在生、没卡死"。
        """
        nonlocal last_report
        if elapsed - last_report < PROGRESS_INTERVAL_SECONDS:
            return
        last_report = elapsed
        db.update_task(task_id, status=TASK_RUNNING, ref=current.id,
                       detail=f"waiting {int(elapsed)}s")
        asyncio.run_coroutine_threadsafe(
            bot.send_message(
                chat_id,
                t("create.progress", task_id=task_id,
                  detail=f"{current.name or current.id} · {current.status}",
                  elapsed=humanize_duration(int(elapsed))),
            ),
            loop,
        )

    try:
        instances = await asyncio.to_thread(provider.create_instances, spec)
        db.update_task(task_id, status=TASK_RUNNING,
                       ref=instances[0].id if instances else None,
                       detail=f"accepted {len(instances)} instance(s)")
        ready = []
        for instance in instances:
            final = await asyncio.to_thread(
                provider.wait_until_ready, instance.id,
                on_progress=report_progress,
            )
            if final.password is None and instance.password:
                final.password = instance.password
            ready.append(final)

        details = "\n\n".join(format_created_instance(item, t) for item in ready)
        await bot.send_message(chat_id, t("create.done", details=details))
        db.update_task(task_id, status=TASK_SUCCESS,
                       ref=", ".join(item.id for item in ready),
                       detail=f"ready in {int(time.monotonic() - started)}s")
        db.log_operation(user_id, "create", LOG_SUCCESS, provider=provider_name,
                         target=", ".join(item.id for item in ready),
                         detail="; ".join(f"{item.public_ip or item.ipv6}" for item in ready))
    except CloudTimeout as exc:
        detail = exc.render(t)
        db.update_task(task_id, status=TASK_FAILED, detail=detail)
        db.log_operation(user_id, "create", LOG_FAILED, provider=provider_name,
                         target=spec.name, detail=detail)
        await bot.send_message(chat_id, t("create.timeout", detail=detail))
    except CloudOpsError as exc:
        detail = exc.render(t)
        db.update_task(task_id, status=TASK_FAILED, detail=detail)
        db.log_operation(user_id, "create", LOG_FAILED, provider=provider_name,
                         target=spec.name, detail=detail)
        await bot.send_message(chat_id, t("create.failed", task_id=task_id, detail=detail))
    except Exception as exc:  # pragma: no cover - 后台兜底
        db.update_task(task_id, status=TASK_FAILED, detail=f"{type(exc).__name__}: {exc}")
        db.log_operation(user_id, "create", LOG_FAILED, provider=provider_name,
                         target=spec.name, detail=str(exc))
        await bot.send_message(chat_id, t("create.failed", task_id=task_id,
                                          detail=f"{type(exc).__name__}: {exc}"))
    finally:
        provider.close()
