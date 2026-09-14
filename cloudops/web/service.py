"""Web 面板的业务层：把 Bot 已有的原语（凭证仓库、云适配器、审计表）组织成
接口视图。

设计原则（与论文 5.x 的"统一入口"一致）：

* **不新增一套数据**：面板读的是 Bot 用的同一个 SQLite 库，因此聊天窗口里
  刚建的实例，刷新面板就能看到；面板上的销毁操作也会以同样的 ``operation_log``
  记录留痕，两边审计一致；
* **不伪造能力**：面板只暴露云适配器真正声明支持的动作（见
  :attr:`CloudProvider.capabilities`）。适配器没有 start/stop，面板就不会长出
  这两个按钮；
* **密钥不出后端**：凭证一律经 :meth:`CredentialStore.describe_credentials`
  脱敏后再交给 HTTP 层，接口不返回密文也不返回明文。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

from .. import __version__
from ..cloud import (
    available_provider_names,
    canonical_name,
    create_provider,
    get_provider_class,
    is_known,
    providers_summary,
)
from ..credentials import CredentialStore
from ..db import Database
from ..errors import CloudOpsError, NotFound, ValidationError
from ..i18n import get_translator
from ..models import LOG_FAILED, LOG_SUCCESS, ROLE_ADMIN, ROLE_GUEST, ROLE_READONLY
from ..utils import now_iso

#: 面板允许操作的目标用户：Bot 的第一个管理员（与 .env 预置凭证的归属一致）
ACTING_ROLE = ROLE_ADMIN

#: 列表接口的单次返回上限（避免大账户把浏览器卡死）
MAX_ROWS = 200

#: 写操作在审计表里的动作名（与 Bot 指令保持同一套动词）
ACTION_ACTIONS = {
    "destroy": "web-destroy",
    "credential.add": "web-bind",
    "credential.delete": "web-unbind",
    "credential.activate": "web-activate",
    "user.role": "web-role",
}

VALID_ROLES = (ROLE_ADMIN, ROLE_READONLY, ROLE_GUEST)


class PanelService:
    """面板的服务门面。"""

    def __init__(self, settings, db: Database, store: CredentialStore, jobs) -> None:
        self.settings = settings
        self.db = db
        self.store = store
        self.jobs = jobs
        self.started_at = time.time()
        self.t = get_translator(settings.language)

    # ------------------------------------------------------------------ #
    # 身份与元信息
    # ------------------------------------------------------------------ #
    @property
    def acting_user_id(self) -> int:
        """面板以哪个身份操作云资源（凭证按 owner 隔离，必须固定一个）。"""
        return self.settings.admin_ids[0] if self.settings.admin_ids else 0

    @property
    def readonly(self) -> bool:
        return bool(self.settings.web.readonly)

    def uptime_seconds(self) -> float:
        """面板进程已运行时长（``/healthz`` 与概览卡片共用）。"""
        return max(0.0, time.time() - self.started_at)

    def bootstrap(self) -> Dict[str, Any]:
        """前端首屏所需的元信息 + 词表（由 :func:`labels` 统一渲染）。"""
        return {
            "version": __version__,
            "language": self.settings.language,
            "readonly": self.readonly,
            "acting_user_id": self.acting_user_id,
            "providers": self.provider_forms(),
            "labels": self.labels(),
        }

    def labels(self) -> Dict[str, str]:
        """把面板用到的文案一次性交给前端。

        文案仍然只写在 ``cloudops/i18n.py``（单一来源），前端通过这个接口拿
        渲染好的字符串。这里刻意逐条显式写出 ``t("…")`` 调用而不是遍历一个
        key 列表：``tests/test_i18n.py`` 靠扫描源码里的字面量 key 来判断
        "两种语言是否齐全""有没有孤儿 key"，写成字面量才能让 Web 界面也纳入
        同一套静态审计。
        """
        t = self.t
        return {
            "web.title": t("web.title"),
            "web.acting_user": t("web.acting_user", id="{id}"),
            "web.bound.yes": t("web.bound.yes"),
            "web.bound.no": t("web.bound.no"),
            "web.logout": t("web.logout"),
            "web.cancel": t("web.cancel"),
            "web.confirm": t("web.confirm"),
            "web.loading": t("web.loading"),
            "web.saved": t("web.saved"),
            "web.deleted": t("web.deleted"),
            "web.activated": t("web.activated"),
            "web.action.failed": t("web.action.failed", detail="{detail}"),
            "web.uptime.format": t("web.uptime.format"),
            "web.readonly.notice": t("web.readonly.notice"),
            "web.error.unauthorized": t("web.error.unauthorized"),
            "web.nav.overview": t("web.nav.overview"),
            "web.nav.instances": t("web.nav.instances"),
            "web.nav.credentials": t("web.nav.credentials"),
            "web.nav.users": t("web.nav.users"),
            "web.nav.audit": t("web.nav.audit"),
            "web.nav.tasks": t("web.nav.tasks"),
            "web.card.users": t("web.card.users"),
            "web.card.credentials": t("web.card.credentials"),
            "web.card.operations": t("web.card.operations"),
            "web.card.tasks": t("web.card.tasks"),
            "web.card.jobs": t("web.card.jobs"),
            "web.card.instances": t("web.card.instances"),
            "web.card.uptime": t("web.card.uptime"),
            "web.card.version": t("web.card.version"),
            "web.overview.hint": t("web.overview.hint"),
            "web.instances.title": t("web.instances.title"),
            "web.instances.hint": t("web.instances.hint", port=self.settings.web.port),
            "web.instances.empty": t("web.instances.empty"),
            "web.instances.refresh": t("web.instances.refresh"),
            "web.instances.all": t("web.instances.all"),
            "web.instances.provider": t("web.instances.provider"),
            "web.instances.name": t("web.instances.name"),
            "web.instances.status": t("web.instances.status"),
            "web.instances.address": t("web.instances.address"),
            "web.instances.region": t("web.instances.region"),
            "web.instances.size": t("web.instances.size"),
            "web.instances.actions": t("web.instances.actions"),
            "web.instances.destroy": t("web.instances.destroy"),
            "web.instances.destroy.prompt": t("web.instances.destroy.prompt",
                                              name="{name}", provider="{provider}"),
            "web.instances.destroy.done": t("web.instances.destroy.done", name="{name}"),
            "web.instances.errors": t("web.instances.errors"),
            "web.instances.no_credential": t("web.instances.no_credential"),
            "web.credentials.title": t("web.credentials.title"),
            "web.credentials.hint": t("web.credentials.hint"),
            "web.credentials.empty": t("web.credentials.empty"),
            "web.credentials.add": t("web.credentials.add"),
            "web.credentials.label": t("web.credentials.label"),
            "web.credentials.provider": t("web.credentials.provider"),
            "web.credentials.owner": t("web.credentials.owner"),
            "web.credentials.identity": t("web.credentials.identity"),
            "web.credentials.fields": t("web.credentials.fields"),
            "web.credentials.active": t("web.credentials.active"),
            "web.credentials.activate": t("web.credentials.activate"),
            "web.credentials.delete": t("web.credentials.delete"),
            "web.credentials.delete.prompt": t("web.credentials.delete.prompt", label="{label}"),
            "web.credentials.source": t("web.credentials.source"),
            "web.users.title": t("web.users.title"),
            "web.users.hint": t("web.users.hint"),
            "web.users.empty": t("web.users.empty"),
            "web.users.id": t("web.users.id"),
            "web.users.role": t("web.users.role"),
            "web.audit.title": t("web.audit.title"),
            "web.audit.hint": t("web.audit.hint"),
            "web.audit.empty": t("web.audit.empty"),
            "web.audit.time": t("web.audit.time"),
            "web.audit.action": t("web.audit.action"),
            "web.audit.status": t("web.audit.status"),
            "web.audit.target": t("web.audit.target"),
            "web.audit.detail": t("web.audit.detail"),
            "web.tasks.title": t("web.tasks.title"),
            "web.tasks.hint": t("web.tasks.hint"),
            "web.tasks.empty": t("web.tasks.empty"),
            "web.tasks.kind": t("web.tasks.kind"),
            "web.tasks.status": t("web.tasks.status"),
            "web.login.failed": t("web.login.failed"),
            "web.login.throttled": t("web.login.throttled", seconds="{seconds}"),
            "web.login.password": t("web.login.password"),
            "web.login.submit": t("web.login.submit"),
            "web.login.footer": t("web.login.footer", port=self.settings.web.port),
        }

    def provider_forms(self) -> List[Dict[str, Any]]:
        """各云平台的凭证字段（驱动面板上的"新增凭证"表单）。"""
        forms: List[Dict[str, Any]] = []
        for name in available_provider_names(self.settings):
            cls = get_provider_class(name)
            forms.append({
                "name": name,
                "label": cls.display_name,
                "fields": [
                    {
                        "name": field.name,
                        "description": field.description,
                        "required": bool(field.required),
                        "secret": bool(field.secret),
                        "example": field.example,
                    }
                    for field in cls.credential_fields
                ],
                "usage": cls.credential_usage(),
                "capabilities": sorted(cls.capabilities),
            })
        return forms

    def overview(self) -> Dict[str, Any]:
        """概览卡片的数据（全部来自本地数据库，不触发云端调用）。"""
        stats = self.db.stats()
        counts = self.db.provider_credential_counts()
        return {
            "version": __version__,
            "uptime_seconds": int(time.time() - self.started_at),
            "users": stats.users,
            "credentials": stats.credentials,
            "operations": stats.operations,
            "tasks": stats.tasks,
            "active_jobs": self.jobs.active,
            "instances_tracked": stats.instances_tracked,
            "provider_counts": counts,
            "providers": [{"name": name, "label": label} for name, label in providers_summary()],
            "bound_providers": self.store.bound_providers(self.acting_user_id),
            "mock_enabled": bool(self.settings.enable_mock_provider),
            "database": str(self.settings.database_path),
            "readonly": self.readonly,
            "web": {"host": self.settings.web.host, "port": self.settings.web.port},
        }

    # ------------------------------------------------------------------ #
    # 实例
    # ------------------------------------------------------------------ #
    async def instances(self, provider_filter: Optional[str] = None) -> Dict[str, Any]:
        """并发拉取各云平台的实例列表。

        单个平台失败（密钥过期、网络抖动、区域不可用）不影响其它平台：
        相应条目带上 ``error`` 字段由前端就地提示，这与论文 3.2 里"多云容错"
        的要求一致 —— 一个云厂商挂了不该让整个面板空白。
        """
        names = self._target_providers(provider_filter)
        results = await asyncio.gather(
            *(self._instances_of(name) for name in names), return_exceptions=False,
        )
        items: List[Dict[str, Any]] = []
        errors: List[Dict[str, str]] = []
        for result in results:
            if result.get("error"):
                errors.append({"provider": result["provider"], "error": result["error"]})
                continue
            items.extend(result["instances"])
        by_status: Dict[str, int] = {}
        for item in items:
            by_status[item["status"]] = by_status.get(item["status"], 0) + 1
        return {
            "instances": items,
            "errors": errors,
            "total": len(items),
            "by_status": by_status,
            "providers": names,
        }

    def _target_providers(self, provider_filter: Optional[str]) -> List[str]:
        if provider_filter:
            name = canonical_name(provider_filter)
            if name not in available_provider_names(self.settings):
                raise ValidationError(
                    params={"value": provider_filter, "parameter": "provider",
                            "options": ", ".join(available_provider_names(self.settings))},
                    key="error.unknown_option",
                )
            return [name]
        return self.store.bound_providers(self.acting_user_id)

    async def _instances_of(self, provider_name: str) -> Dict[str, Any]:
        try:
            provider = self.store.provider(provider_name, self.acting_user_id)
        except CloudOpsError as exc:
            return {"provider": provider_name, "instances": [], "error": str(exc)}
        try:
            instances = await asyncio.to_thread(provider.list_instances)
        except Exception as exc:                      # 单平台容错：降级为一条提示
            return {"provider": provider_name, "instances": [], "error": str(exc)}
        finally:
            provider.close()
        rows = []
        for instance in instances[:MAX_ROWS]:
            row = instance.to_public_dict()
            row["address"] = instance.address
            row["ready"] = instance.ready
            # 前端据此渲染按钮：适配器没声明 destroy 就不要再长出一个注定报错的按钮
            row["actions"] = [name for name in ("destroy",) if provider.supports(name)]
            rows.append(row)
        return {"provider": provider_name, "instances": rows, "error": None}

    async def destroy(self, provider_name: str, instance_id: str,
                      confirm: str) -> Dict[str, Any]:
        """销毁实例：必须回填实例名（或 ID）作为二次确认 —— 与 Bot 的
        ``/delete`` 确认码是同一道防线，只是换了交互形式。"""
        provider = self.store.provider(provider_name, self.acting_user_id)
        try:
            if not provider.supports("destroy"):
                # 面板不假装有能力：接口层也拦一道，不只是靠前端不显示按钮
                raise ValidationError(
                    params={"action": "destroy", "provider": provider_name},
                    key="web.error.action_unsupported",
                )
            instance = await asyncio.to_thread(provider.find_instance, instance_id)
            if instance is None:
                raise NotFound(params={"target": instance_id}, key="error.instance_not_found")
            accepted = {str(instance.id), str(instance.name)}
            if (confirm or "").strip() not in accepted:
                raise ValidationError(
                    params={"expected": instance.name or instance.id},
                    key="web.error.confirm_mismatch",
                )
            await asyncio.to_thread(provider.destroy_instance, instance.id)
        except Exception as exc:
            self._log("destroy", LOG_FAILED, provider_name,
                      target=instance_id, detail=str(exc)[:200])
            raise
        finally:
            provider.close()
        self._log("destroy", LOG_SUCCESS, provider_name, target=instance.label(),
                  detail="via web panel")
        return {"destroyed": instance.id, "name": instance.name}

    # ------------------------------------------------------------------ #
    # 凭证
    # ------------------------------------------------------------------ #
    def credentials(self) -> Dict[str, Any]:
        rows = []
        for item in self.store.describe_credentials(self.acting_user_id):
            credential = item["credential"]
            rows.append({
                "id": credential.id,
                "provider": credential.provider,
                "label": credential.label,
                "owner": item["owner"],
                "is_active": bool(item["is_active"]),
                "shared": bool(credential.shared),
                "fields": item["masked"],
                "identity": (credential.meta or {}).get("identity"),
                "source": (credential.meta or {}).get("source"),
                "created_at": credential.created_at,
                "updated_at": credential.updated_at,
            })
        return {"credentials": rows, "total": len(rows)}

    async def add_credential(self, provider_name: str, label: str,
                             fields: Dict[str, Any]) -> Dict[str, Any]:
        """新增凭证：先按适配器声明校验字段，再真的调一次云 API 验证可用性。

        校验失败不落库 —— 避免面板里堆一堆"看起来绑好了但其实是错的"凭证。
        """
        canonical = canonical_name(provider_name)
        if not is_known(canonical):
            # 注意：get_provider_class 对未知平台抛 KeyError（registry 的内部约定），
            # 这里必须先归一化校验，否则接口会以 500 收场。
            raise ValidationError(
                params={"value": provider_name, "parameter": "provider",
                        "options": ", ".join(available_provider_names(self.settings))},
                key="error.unknown_option",
            )
        if canonical == "mock" and not self.settings.enable_mock_provider:
            raise ValidationError(
                params={"value": provider_name, "parameter": "provider",
                        "options": ", ".join(available_provider_names(self.settings))},
                key="error.mock_disabled",
            )
        cls = get_provider_class(canonical)
        secrets = {key: value for key, value in (fields or {}).items() if value not in (None, "")}
        missing = [field.name for field in cls.credential_fields
                   if field.required and not secrets.get(field.name)]
        if missing:
            raise ValidationError(
                params={"fields": ", ".join(missing)}, key="web.error.missing_fields")
        provider = create_provider(canonical, secrets, self.settings, label=label or "default")
        try:
            identity = await asyncio.to_thread(provider.validate_credentials)
        except Exception as exc:
            self._log("credential.add", LOG_FAILED, canonical, target=label or "default",
                      detail=str(exc)[:200])
            raise
        finally:
            provider.close()
        credential = self.store.bind(self.acting_user_id, canonical, label or "default",
                                     secrets, shared=True, activate=True)
        self.store.update_meta(credential, {"identity": identity, "source": "web"})
        self._log("credential.add", LOG_SUCCESS, canonical, target=credential.label,
                  detail=f"identity={identity}")
        return {"id": credential.id, "provider": canonical, "label": credential.label,
                "identity": identity}

    def delete_credential(self, credential_id: int) -> Dict[str, Any]:
        credential = self.store.delete(credential_id, owner_id=self.acting_user_id)
        self._log("credential.delete", LOG_SUCCESS, credential.provider,
                  target=credential.label)
        return {"deleted": credential.id, "provider": credential.provider}

    def activate_credential(self, credential_id: int) -> Dict[str, Any]:
        credential = self.store.activate(credential_id, owner_id=self.acting_user_id)
        self._log("credential.activate", LOG_SUCCESS, credential.provider,
                  target=credential.label)
        return {"activated": credential.id, "provider": credential.provider}

    # ------------------------------------------------------------------ #
    # 用户 / 审计 / 任务
    # ------------------------------------------------------------------ #
    def users(self) -> Dict[str, Any]:
        rows = [
            {
                "telegram_id": user.telegram_id,
                "username": user.username,
                "role": user.role,
                "created_at": user.created_at,
                "last_seen_at": user.last_seen_at,
            }
            for user in self.db.list_users(limit=MAX_ROWS)
        ]
        return {"users": rows, "total": len(rows), "roles": list(VALID_ROLES)}

    def set_role(self, telegram_id: int, role: str) -> Dict[str, Any]:
        if role not in VALID_ROLES:
            raise ValidationError(
                params={"value": role, "parameter": "role", "options": ", ".join(VALID_ROLES)},
                key="error.unknown_option",
            )
        user = self.db.set_role(telegram_id, role)
        self._log("user.role", LOG_SUCCESS, None, target=str(telegram_id), detail=role)
        return {"telegram_id": user.telegram_id, "role": user.role}

    def logs(self, limit: int = 50) -> Dict[str, Any]:
        rows = [
            {
                "id": log.id,
                "telegram_id": log.telegram_id,
                "provider": log.provider,
                "action": log.action,
                "target": log.target,
                "status": log.status,
                "detail": log.detail,
                "created_at": log.created_at,
            }
            for log in self.db.list_logs(limit=self._clamp(limit))
        ]
        return {"logs": rows, "total": len(rows)}

    def tasks(self, limit: int = 50) -> Dict[str, Any]:
        rows = [
            {
                "id": task.id,
                "telegram_id": task.telegram_id,
                "provider": task.provider,
                "kind": task.kind,
                "status": task.status,
                "ref": task.ref,
                "detail": task.detail,
                "created_at": task.created_at,
                "updated_at": task.updated_at,
            }
            for task in self.db.list_tasks(limit=self._clamp(limit))
        ]
        return {"tasks": rows, "total": len(rows)}

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    @staticmethod
    def _clamp(limit: Any, *, default: int = 50) -> int:
        try:
            value = int(limit)
        except (TypeError, ValueError):
            return default
        return max(1, min(MAX_ROWS, value))

    def _log(self, action: str, status: str, provider: Optional[str], *,
             target: Optional[str] = None, detail: Optional[str] = None) -> None:
        """写审计日志：面板操作与 Bot 指令落在同一张表里。"""
        self.db.log_operation(
            self.acting_user_id, ACTION_ACTIONS.get(action, action), status,
            provider=provider, target=target, detail=detail or f"web@{now_iso()}",
        )

