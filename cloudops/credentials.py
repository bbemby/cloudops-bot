"""凭证仓库：数据库记录 ↔ 解密后的明文 ↔ 已实例化的云适配器。

这一层把"凭证管理"（论文 4.2）的职责收拢在一处：

* 明文只在内存里存在（``secrets()`` 临时解密）；
* 实例化适配器时统一注入 Settings（超时、重试、默认区域/规格/镜像）；
* 找不到可用凭证时给出**下一步该做什么**的提示，而不是一句 "no credential"。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .cloud import CloudProvider, available_provider_names, canonical_name, create_provider, is_known
from .config import Settings
from .crypto import SecretBox
from .db import Database
from .errors import CredentialError, ValidationError
from .models import Credential
from .utils import dumps, mask_mapping, now_iso


class CredentialStore:
    """凭证的加密存储与适配器装配。"""

    def __init__(self, db: Database, box: SecretBox, settings: Settings) -> None:
        self.db = db
        self.box = box
        self.settings = settings
        self._cache: Dict[Tuple[str, int], Credential] = {}

    # ------------------------------------------------------------------ #
    # 加解密
    # ------------------------------------------------------------------ #
    def secrets(self, credential: Credential) -> Dict[str, Any]:
        """解密出凭证明文（仅内存使用，切勿落盘/写日志）。"""
        return self.box.decrypt_dict(credential.secret_blob)

    def bind(self, owner_id: int, provider: str, label: str, secrets: Dict[str, Any], *,
             shared: bool = True, activate: bool = True) -> Credential:
        """写入一条加密凭证。"""
        canonical = self._validate_provider(provider)
        label = (label or "default").strip()
        if not label or len(label) > 32:
            raise ValidationError(params={"limit": "32"}, key="error.invalid_label")
        blob = self.box.encrypt_dict(secrets)
        meta = {"region": secrets.get("region"), "regions": secrets.get("regions")}
        credential = self.db.add_credential(
            owner_id, canonical, label, blob,
            meta={key: value for key, value in meta.items() if value},
            shared=shared, activate=activate,
        )
        self._cache.pop((canonical, owner_id), None)
        return credential

    def update_meta(self, credential: Credential, meta: Dict[str, Any]) -> Credential:
        """更新凭证的非敏感元数据（例如验证后的账户标识）。"""
        merged = dict(credential.meta or {})
        merged.update({key: value for key, value in meta.items() if value is not None})
        with self.db.connect() as conn:
            conn.execute("UPDATE credentials SET meta_json = ?, updated_at = ? WHERE id = ?",
                         (dumps(merged), now_iso(), credential.id))
        self._cache.pop((credential.provider, credential.owner_id), None)
        return self.db.get_credential(credential.id) or credential

    def delete(self, credential_id: int, *, owner_id: Optional[int] = None) -> Credential:
        credential = self.db.delete_credential(credential_id, owner_id=owner_id)
        self._cache.pop((credential.provider, credential.owner_id), None)
        return credential

    def activate(self, credential_id: int, *, owner_id: Optional[int] = None) -> Credential:
        credential = self.db.set_active_credential(credential_id, owner_id=owner_id)
        self._cache.pop((credential.provider, credential.owner_id), None)
        return credential

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def list_for_user(self, user_id: int, provider: Optional[str] = None) -> List[Credential]:
        """自己绑定的 + 其他管理员共享的。"""
        own = self.db.list_credentials(provider=provider, owner_id=user_id)
        shared = [item for item in self.db.list_credentials(provider=provider, shared_only=True)
                  if item.owner_id != user_id]
        return own + shared

    def active_for(self, provider: str, user_id: int) -> Optional[Credential]:
        canonical = canonical_name(provider)
        key = (canonical, user_id)
        if key not in self._cache:
            credential = self.db.resolve_active_credential(canonical, user_id)
            if credential is None:
                return None
            self._cache[key] = credential
        return self._cache[key]

    def credential_by_id(self, credential_id: int) -> Optional[Credential]:
        return self.db.get_credential(credential_id)

    # ------------------------------------------------------------------ #
    # 装配适配器
    # ------------------------------------------------------------------ #
    def provider(self, provider: str, user_id: int, *, label: Optional[str] = None) -> CloudProvider:
        """取得"该用户在某云平台上应使用的适配器实例"。"""
        canonical = self._validate_provider(provider)
        if label:
            candidates = [item for item in self.list_for_user(user_id, canonical) if item.label == label]
            if not candidates:
                raise CredentialError(
                    params={"provider": canonical, "label": label},
                    key="error.credential_label_not_found",
                )
            credential = candidates[0]
        else:
            credential = self.active_for(canonical, user_id)
        if credential is None:
            raise CredentialError(
                params={"provider": canonical, "usage": self.bind_usage(canonical)},
                key="error.credential_missing",
            )
        return self.provider_from_credential(credential)

    def provider_from_credential(self, credential: Credential) -> CloudProvider:
        secrets = self.secrets(credential)
        meta = dict(credential.meta or {})
        return create_provider(
            credential.provider, secrets, self.settings,
            label=credential.label, meta=meta,
        )

    def provider_with_credential(self, provider: str, user_id: int, *,
                                 label: Optional[str] = None) -> Tuple[CloudProvider, Credential]:
        """同时返回适配器与其凭证记录（用于日志中标注所用凭证）。"""
        canonical = self._validate_provider(provider)
        if label:
            candidates = [item for item in self.list_for_user(user_id, canonical) if item.label == label]
            if not candidates:
                raise CredentialError(
                    params={"provider": canonical, "label": label},
                    key="error.credential_label_not_found",
                )
            credential = candidates[0]
        else:
            credential = self.active_for(canonical, user_id)
            if credential is None:
                raise CredentialError(
                    params={"provider": canonical, "usage": self.bind_usage(canonical)},
                    key="error.credential_missing",
                )
        return self.provider_from_credential(credential), credential

    # ------------------------------------------------------------------ #
    # 辅助
    # ------------------------------------------------------------------ #
    def bound_providers(self, user_id: int) -> List[str]:
        """该用户可用的 provider（有自己或共享的凭证）。"""
        found = {item.provider for item in self.list_for_user(user_id)}
        return [name for name in available_provider_names(self.settings) if name in found]

    def bind_usage(self, provider: str) -> str:
        try:
            from .cloud import get_provider_class

            return get_provider_class(provider).credential_usage()
        except KeyError:
            return "/bind <provider> <名称> <字段>=<值>"

    def _validate_provider(self, provider: str) -> str:
        canonical = canonical_name(provider)
        if not is_known(canonical):
            raise ValidationError(
                params={"value": provider, "parameter": "云平台",
                        "options": ", ".join(available_provider_names(self.settings))},
                key="error.unknown_option",
            )
        if canonical == "mock" and not self.settings.enable_mock_provider:
            raise ValidationError(
                params={"value": provider, "parameter": "云平台",
                        "options": ", ".join(available_provider_names(self.settings))},
                key="error.mock_disabled",
            )
        return canonical

    def describe_credentials(self, user_id: int) -> List[Dict[str, Any]]:
        """展示用视图（密钥字段已脱敏，由 bot 层渲染）。"""
        rows: List[Dict[str, Any]] = []
        for credential in self.list_for_user(user_id):
            try:
                secrets = self.secrets(credential)
            except Exception:  # pragma: no cover - SECRET_KEY 变更场景
                secrets = {}
            masked = mask_mapping(
                secrets,
                secret_fields=self._secret_fields(credential.provider),
            )
            rows.append({
                "credential": credential,
                "masked": masked,
                "owner": credential.owner_id,
                "is_active": credential.is_active,
            })
        return rows

    @staticmethod
    def _secret_fields(provider: str) -> Tuple[str, ...]:
        try:
            from .cloud import get_provider_class

            return get_provider_class(provider).secret_field_names()
        except KeyError:  # pragma: no cover
            return ("token", "secret_access_key")
