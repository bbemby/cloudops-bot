"""DigitalOcean 适配器（论文 2.2：Bearer Token 无状态鉴权 + RESTful JSON）。

对应论文 6.2 的核心代码片段，但做了工程化补强：

* 分页遍历（``links.pages.next``），避免账号实例多时只看到第一页；
* 429/5xx 自动退避重试（见 :class:`~cloudops.cloud.http.HttpClient`）；
* 区域/规格/镜像的**别名解析**，让 ``/create sgp1 1gb`` 这类简写可用，
  同时给出"你可能是想……"的候选提示。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ..config import Settings
from ..errors import CloudError, CredentialError, ValidationError
from ..utils import now_iso
from .base import CloudProvider, CreateSpec, CredentialField, Instance
from .http import HttpClient
from .registry import register_provider

API_BASE = "https://api.digitalocean.com/v2"

#: 常用规格简写 → 官方 slug（论文 TC-02 的 ``1gb`` 即命中此项）
SIZE_ALIASES: Dict[str, str] = {
    "512mb": "s-1vcpu-512mb-10gb",
    "1gb": "s-1vcpu-1gb",
    "1vcpu1gb": "s-1vcpu-1gb",
    "2gb": "s-1vcpu-2gb",
    "2vcpu2gb": "s-2vcpu-2gb",
    "2v2gb": "s-2vcpu-2gb",
    "4gb": "s-2vcpu-4gb",
    "8gb": "s-4vcpu-8gb",
    "16gb": "s-8vcpu-16gb",
}

#: 镜像简写 → 官方 slug
IMAGE_ALIASES: Dict[str, str] = {
    "debian": "debian-12-x64",
    "debian12": "debian-12-x64",
    "debian-12": "debian-12-x64",
    "ubuntu": "ubuntu-24-04-x64",
    "ubuntu24": "ubuntu-24-04-x64",
    "ubuntu-24-04": "ubuntu-24-04-x64",
    "ubuntu22": "ubuntu-22-04-x64",
    "ubuntu-22-04": "ubuntu-22-04-x64",
    "almalinux": "almalinux-9-x64",
}

#: 官方状态 → 统一状态
STATUS_MAP = {
    "new": "provisioning",
    "active": "running",
    "off": "stopped",
    "archive": "archived",
}

def _normalize_created_at(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return value if value.endswith("Z") else value.replace("+00:00", "Z")


@register_provider
class DigitalOceanProvider(CloudProvider):
    """基于 ``https://api.digitalocean.com/v2`` 的适配器。"""

    name = "digitalocean"
    display_name = "DigitalOcean"
    docs_url = "https://docs.digitalocean.com/reference/api/api-reference/"
    aliases = ("do", "digitalocean", "digital-ocean")
    returns_password = True
    credential_fields = (
        CredentialField("token", "DigitalOcean 个人访问令牌（Read/Write 权限）",
                        example="dop_v1_xxx 或 64 位十六进制串"),
        CredentialField("region", "默认数据中心区域", required=False, secret=False, example="sgp1"),
    )
    capabilities = frozenset({"list", "create", "destroy", "status"})

    def __init__(self, secrets, settings: Settings, *, label: str = "default", meta=None) -> None:
        super().__init__(secrets, settings, label=label, meta=meta)
        self._http = HttpClient(
            provider=self.name,
            user_agent=settings.user_agent,
            timeout=settings.http_timeout,
            retries=settings.http_retries,
        )
        self._cache: Dict[str, List[Dict[str, Any]]] = {}
        self.warnings: List[str] = []

    # ------------------------------------------------------------------ #
    # 内部请求
    # ------------------------------------------------------------------ #
    @property
    def token(self) -> str:
        return str(self.secrets.get("token", "")).strip()

    def _headers(self) -> Dict[str, str]:
        self.require_credentials()
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        response = self._http.request("GET", f"{API_BASE}{path}", headers=self._headers(), params=params)
        return HttpClient.json(response) or {}

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        response = self._http.request("POST", f"{API_BASE}{path}", headers=self._headers(),
                                      json_body=payload, expect={200, 201, 202})
        return HttpClient.json(response) or {}

    def _delete(self, path: str) -> None:
        self._http.request("DELETE", f"{API_BASE}{path}", headers=self._headers(),
                           expect={200, 202, 204})

    def _cached(self, path: str) -> List[Dict[str, Any]]:
        """缓存区域/规格/镜像目录，避免每次指令都多打几次 API。"""
        if path not in self._cache:
            payload = self._get(path, {"per_page": 200})
            key = path.strip("/").split("/")[-1]
            self._cache[path] = list(payload.get(key) or [])
        return self._cache[path]

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ #
    # 鉴权校验
    # ------------------------------------------------------------------ #
    def validate_credentials(self) -> str:
        try:
            payload = self._get("/account")
        except CloudError as exc:
            if exc.status_code in (401, 403):
                raise CredentialError(
                    params={"provider": self.display_name, "detail": str(exc)},
                    key="error.invalid_credential",
                ) from exc
            raise
        account = payload.get("account") or {}
        email = account.get("email") or account.get("uuid") or "unknown"
        status = account.get("status")
        if status and status != "active":
            self.warnings.append(f"DigitalOcean 账户状态为 {status}")
        return f"{email}"

    # ------------------------------------------------------------------ #
    # 参数解析
    # ------------------------------------------------------------------ #
    def default_region(self) -> str:
        return str(self.secrets.get("region") or self.meta.get("region")
                   or self.settings.digitalocean.region)

    def default_size(self) -> str:
        return str(self.meta.get("size") or self.settings.digitalocean.size)

    def default_image(self) -> str:
        return str(self.meta.get("image") or self.settings.digitalocean.image)

    def resolve_region(self, token: Optional[str]) -> str:
        raw = (token or "").strip().lower()
        if not raw:
            return self.default_region()
        try:
            slugs = [str(item.get("slug")) for item in self._cached("/regions")]
        except CloudError:
            return raw  # 目录不可用时按用户输入透传，交由云端校验
        if raw in slugs:
            return raw
        candidates = [slug for slug in slugs if slug.startswith(raw)]
        if len(candidates) == 1:
            return candidates[0]
        if candidates:
            raise ValidationError(
                params={"value": raw, "options": ", ".join(sorted(candidates)[:8]),
                        "parameter": "region"},
                key="error.ambiguous_option",
            )
        raise ValidationError(
            params={"value": raw, "options": ", ".join(sorted(slugs)[:12]), "parameter": "region"},
            key="error.unknown_option",
        )

    def resolve_size(self, token: Optional[str]) -> str:
        raw = (token or "").strip().lower()
        if not raw:
            return self.default_size()
        try:
            sizes = self._cached("/sizes")
        except CloudError:
            return raw
        slugs = {str(item.get("slug")) for item in sizes}
        if raw in slugs:
            return raw
        alias = SIZE_ALIASES.get(raw)
        if alias and alias in slugs:
            return alias

        # 支持 "2vcpu-4gb" / "4gb" / "2v2gb" 这类直觉写法：按 CPU+内存 反查
        match = re.match(r"^(\d+)?v?cpu?[-_]?(\d+)gb$", raw) or re.match(r"^(\d+)gb$", raw)
        if match:
            groups = match.groups()
            vcpus = int(groups[0]) if len(groups) > 1 and groups[0] else None
            memory = int(groups[-1])
            matched = [
                item for item in sizes
                if int(item.get("memory") or 0) == memory * 1024
                and (vcpus is None or int(item.get("vcpus") or 0) == vcpus)
                and not item.get("deprecated")
            ]
            if matched:
                matched.sort(key=lambda item: float((item.get("price_monthly") or 0) or 0))
                return str(matched[0].get("slug"))

        raise ValidationError(
            params={"value": raw, "options": ", ".join(sorted(slugs)[:12]), "parameter": "规格(size)"},
            key="error.unknown_option",
        )

    def resolve_image(self, token: Optional[str]) -> str:
        raw = (token or "").strip().lower()
        if not raw:
            return self.default_image()
        if raw in IMAGE_ALIASES:
            return IMAGE_ALIASES[raw]
        try:
            images = self._cached("/images")
        except CloudError:
            return raw
        slugs = {str(item.get("slug") or "").lower() for item in images}
        if raw in slugs:
            return raw
        candidates = [slug for slug in slugs if slug.startswith(raw)]
        if candidates:
            return sorted(candidates)[-1]  # 取版本号最大的一个
        return raw  # 交给云端校验（可能是私有镜像 ID）

    def sizes_catalog(self, limit: int = 8) -> str:
        """给用户的"可用规格"提示。"""
        try:
            sizes = self._cached("/sizes")
        except CloudError:
            return "（无法获取规格目录，请检查凭证）"
        rows = [item for item in sizes if not item.get("deprecated")][:limit]
        return ", ".join(
            f"{item.get('slug')}({int(item.get('memory', 0)) // 1024}GB/{item.get('vcpus')}vCPU"
            f"/${item.get('price_monthly')}/mo)" for item in rows
        )

    def regions_catalog(self, limit: int = 12) -> str:
        try:
            regions = self._cached("/regions")
        except CloudError:
            return "（无法获取区域目录，请检查凭证）"
        return ", ".join(str(item.get("slug")) for item in regions[:limit])

    def describe_options(self) -> str:
        return "\n".join([
            super().describe_options(),
            f"可用区域 : {self.regions_catalog()}",
            f"常用规格 : {self.sizes_catalog()}",
            "用法示例 : /create do sgp1 1gb  |  /create do region=fra1 size=s-2vcpu-4gb image=ubuntu",
        ])

    # ------------------------------------------------------------------ #
    # 实例生命周期
    # ------------------------------------------------------------------ #
    def _to_instance(self, droplet: Dict[str, Any], *, password: Optional[str] = None) -> Instance:
        networks = droplet.get("networks") or {}
        public_ip: Optional[str] = None
        private_ip: Optional[str] = None
        ipv6: Optional[str] = None
        for address in networks.get("v4") or []:
            if address.get("type") == "public" and not public_ip:
                public_ip = address.get("ip_address")
            elif address.get("type") == "private" and not private_ip:
                private_ip = address.get("ip_address")
        for address in networks.get("v6") or []:
            ipv6 = address.get("ip_address")
            break
        size = droplet.get("size") or {}
        image = droplet.get("image") or {}
        return Instance(
            provider=self.name,
            id=str(droplet.get("id")),
            name=str(droplet.get("name") or ""),
            status=STATUS_MAP.get(str(droplet.get("status")), str(droplet.get("status") or "unknown")),
            region=str((droplet.get("region") or {}).get("slug") or "-"),
            public_ip=public_ip,
            private_ip=private_ip,
            ipv6=ipv6,
            size=str(size.get("slug")) if isinstance(size, dict) else str(size or ""),
            image=str(image.get("slug") or image.get("name") or "") if isinstance(image, dict) else "",
            created_at=_normalize_created_at(droplet.get("created_at")),
            tags=list(droplet.get("tags") or []),
            monthly_cost=float(size["price_monthly"]) if isinstance(size, dict) and size.get("price_monthly") else None,
            password=password,
            raw=droplet,
        )

    def list_instances(self) -> List[Instance]:
        instances: List[Instance] = []
        page = 1
        while True:
            payload = self._get("/droplets", {"per_page": 200, "page": page})
            for droplet in payload.get("droplets") or []:
                instances.append(self._to_instance(droplet))
            next_page = ((payload.get("links") or {}).get("pages") or {}).get("next")
            if not next_page:
                break
            page += 1
            if page > 50:  # 安全阀：避免异常响应导致死循环
                break
        return instances

    def get_instance(self, instance_id: str) -> Instance:
        payload = self._get(f"/droplets/{instance_id}")
        droplet = payload.get("droplet")
        if not droplet:
            return super().get_instance(instance_id)
        return self._to_instance(droplet)

    def create_instance(self, spec: CreateSpec) -> Instance:
        self.require_credentials()
        payload: Dict[str, Any] = {
            "name": spec.name,
            "region": spec.region or self.default_region(),
            "size": spec.size or self.default_size(),
            "image": spec.image or self.default_image(),
            "backups": bool(spec.extra.get("backups", self.settings.digitalocean.backups)),
            "ipv6": bool(spec.extra.get("ipv6", self.settings.digitalocean.ipv6)),
            "monitoring": bool(spec.extra.get("monitoring", self.settings.digitalocean.monitoring)),
            "tags": list(spec.tags or self.settings.digitalocean.tags) or None,
        }
        ssh_keys = list(spec.ssh_keys or self.settings.digitalocean.ssh_keys)
        if ssh_keys:
            payload["ssh_keys"] = ssh_keys
        if spec.user_data:
            payload["user_data"] = spec.user_data

        data = self._post("/droplets", {key: value for key, value in payload.items() if value is not None})
        droplet = data.get("droplet") or {}
        if not droplet:
            raise CloudError(
                params={"provider": self.display_name, "detail": "响应缺少 droplet 字段"},
                key="error.unexpected_response",
                provider=self.name,
            )
        instance = self._to_instance(droplet, password=droplet.get("root_password"))
        if instance.status == "provisioning" and not droplet.get("root_password"):
            self.warnings.append("根密码仅在创建时返回一次，如需密码请改用未绑定 SSH Key 的方式创建")
        return instance

    def destroy_instance(self, instance_id: str) -> None:
        self.require_credentials()
        self._delete(f"/droplets/{instance_id}")

    # ------------------------------------------------------------------ #
    def action_status(self, action_id: int) -> Dict[str, Any]:
        """查询异步 action（可选能力，用于更精细的进度反馈）。"""
        return self._get(f"/actions/{action_id}")

    def account_balance(self) -> Optional[float]:
        try:
            payload = self._get("/customers/my/balance")
            return float(payload.get("month_to_date_balance") or 0)
        except CloudError:
            return None

    def snapshot(self) -> Dict[str, Any]:
        return {"provider": self.name, "label": self.label, "checked_at": now_iso()}
