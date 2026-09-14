"""本地模拟云（Mock Cloud）—— 无需真实账号即可跑通全链路的演示/测试适配器。

用途：

* 让开源项目的评审者 ``docker compose up`` 后 30 秒内就能看到完整交互；
* 让 CI 在没有云凭证的情况下覆盖"创建 → 轮询 → 销毁"全流程；
* 论文 TC-01 ~ TC-04 的自动化回归测试正是跑在这个适配器上。

它把实例状态保存在 JSON 文件里，并模拟"创建后需要几十秒才拿到公网 IP"的
真实节奏（可用 ``demo`` 凭证字段或环境变量调整延迟）。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import Settings
from ..errors import CloudError, NotFound
from ..utils import now_iso
from .base import CloudProvider, CreateSpec, CredentialField, Instance
from .registry import register_provider

_LOCK = threading.Lock()
_DEFAULT_DELAY = 3.0


@register_provider
class MockProvider(CloudProvider):
    """把状态存在本地 JSON 文件里的假云厂商。"""

    name = "mock"
    display_name = "Mock Cloud（本地演示）"
    docs_url = "https://github.com/bbemby/cloudops-bot#-mock-cloud"
    aliases = ("demo", "local")
    returns_password = True
    credential_fields = (
        CredentialField("token", "任意非空字符串即可，本地模拟不校验",
                        example="demo-token"),
        CredentialField("region", "模拟区域", required=False, secret=False, example="mock-1"),
        CredentialField("delay", "创建后多少秒变为 running（演示异步推送）",
                        required=False, secret=False, example="3"),
    )

    def __init__(self, secrets, settings: Settings, *, label: str = "default", meta=None) -> None:
        super().__init__(secrets, settings, label=label, meta=meta)
        self.warnings: List[str] = []
        self.path = Path(meta.get("state_path")) if meta and meta.get("state_path") else settings.mock_state_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"instances": {}, "seq": 1000})

    # ------------------------------------------------------------------ #
    # 状态文件
    # ------------------------------------------------------------------ #
    def _read(self) -> Dict[str, Any]:
        with _LOCK:
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {"instances": {}, "seq": 1000}

    def _write(self, state: Dict[str, Any]) -> None:
        with _LOCK:
            self.path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    @property
    def delay(self) -> float:
        try:
            value = float(self.secrets.get("delay") or self.meta.get("delay") or _DEFAULT_DELAY)
        except (TypeError, ValueError):
            value = _DEFAULT_DELAY
        return max(0.0, value)

    # ------------------------------------------------------------------ #
    # 元信息
    # ------------------------------------------------------------------ #
    def default_region(self) -> str:
        return str(self.secrets.get("region") or "mock-sgp1")

    def default_size(self) -> str:
        return str(self.meta.get("size") or "mock-1vcpu-1gb")

    def default_image(self) -> str:
        return str(self.meta.get("image") or "mock-debian-12")

    def describe_options(self) -> str:
        return "\n".join([
            super().describe_options(),
            f"创建延迟 : {self.delay:.0f} 秒后转为 running（用于演示异步推送）",
            f"状态文件 : {self.path}",
            "用法示例 : /create mock myserver  |  /create local region=mock-fra1",
        ])

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def validate_credentials(self) -> str:
        if not self.secrets.get("token"):
            from ..errors import CredentialError

            raise CredentialError(params={"provider": self.display_name, "fields": "token",
                                          "usage": self.credential_usage()},
                                  key="error.missing_credential_fields")
        return f"本地模拟账户（{self.path.name}）"

    def _materialize(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """把"创建时间 + 延迟"折算为当前状态（模拟云端异步就绪）。"""
        created = float(record.get("created_ts") or 0)
        elapsed = time.time() - created
        if record.get("status") == "provisioning" and elapsed >= self.delay:
            tail = int(str(record.get("id", "0")).split("-")[-1]) % 250 + 1
            record["status"] = "running"
            record["public_ip"] = f"203.0.113.{tail}"
            record["private_ip"] = f"10.10.0.{tail}"
            record["ipv6"] = f"2001:db8::{tail}"
            record["ready_at"] = now_iso()
        return record

    def _to_instance(self, record: Dict[str, Any]) -> Instance:
        record = self._materialize(record)
        return Instance(
            provider=self.name,
            id=str(record.get("id")),
            name=str(record.get("name") or ""),
            status=str(record.get("status") or "unknown"),
            region=str(record.get("region") or "-"),
            public_ip=record.get("public_ip"),
            private_ip=record.get("private_ip"),
            ipv6=record.get("ipv6"),
            size=record.get("size"),
            image=record.get("image"),
            created_at=record.get("created_at"),
            tags=list(record.get("tags") or []),
            monthly_cost=float(record.get("monthly_cost") or 0) or None,
            password=record.get("password"),
            raw=record,
        )

    def list_instances(self) -> List[Instance]:
        state = self._read()
        instances = [self._to_instance(record) for record in (state.get("instances") or {}).values()]
        # 状态推进后落盘，模拟云端状态变化
        state["instances"] = {record["id"]: record for record in (instance.raw for instance in instances)}
        self._write(state)
        return sorted(instances, key=lambda item: item.created_at or "")

    def get_instance(self, instance_id: str) -> Instance:
        state = self._read()
        record = (state.get("instances") or {}).get(str(instance_id))
        if record is None:
            raise NotFound(params={"target": instance_id, "provider": self.display_name},
                           key="error.instance_not_found")
        self._materialize(record)
        self._write(state)
        return self._to_instance(record)

    def create_instance(self, spec: CreateSpec) -> Instance:
        state = self._read()
        state["seq"] = int(state.get("seq") or 1000) + 1
        instance_id = f"mock-{state['seq']}"
        record = {
            "id": instance_id,
            "name": spec.name,
            "status": "provisioning",
            "region": spec.region or self.default_region(),
            "size": spec.size or self.default_size(),
            "image": spec.image or self.default_image(),
            "tags": list(spec.tags),
            "created_at": now_iso(),
            "created_ts": time.time(),
            "password": f"mock-{instance_id[-4:]}-pwd",
        }
        state.setdefault("instances", {})[instance_id] = record
        self._write(state)
        return self._to_instance(record)

    def destroy_instance(self, instance_id: str) -> None:
        state = self._read()
        instances = state.get("instances") or {}
        if str(instance_id) not in instances:
            raise NotFound(params={"target": instance_id, "provider": self.display_name},
                           key="error.instance_not_found")
        instances.pop(str(instance_id))
        self._write(state)

    def reset(self) -> None:
        """清空所有模拟实例（测试用）。"""
        self._write({"instances": {}, "seq": 1000})

    def inject_failure(self, instance_id: str) -> None:
        """把某实例标记为 error，用于验证失败路径。"""
        state = self._read()
        record = (state.get("instances") or {}).get(str(instance_id))
        if record:
            record["status"] = "error"
            self._write(state)

    def health(self) -> Dict[str, Any]:
        state = self._read()
        return {"provider": self.name, "instances": len(state.get("instances") or {}),
                "state_file": str(self.path)}

    def ensure_token(self) -> None:  # pragma: no cover - 兼容旧调用
        if not self.secrets.get("token"):
            raise CloudError("mock 适配器需要 token 字段", provider=self.name)
