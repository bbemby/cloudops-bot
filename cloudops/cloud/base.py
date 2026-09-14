"""多云适配层公共设施。

论文 5.1 提到"调度引擎内部实现了工厂模式，根据不同的指令参数动态实例化
对应的云平台适配器"——工厂与抽象基类即本模块与 ``registry`` 提供的能力。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    ClassVar,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Tuple,
)

from ..errors import CloudError, CloudTimeout, CredentialError, NotFound

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Settings


@dataclass(frozen=True)
class CredentialField:
    """声明式描述一个凭证字段。

    它同时驱动三件事：``/bind`` 的参数解析、``/providers`` 的帮助文案、
    以及文档中"需要用户提供什么"的清单。
    """

    name: str
    description: str
    required: bool = True
    secret: bool = True
    example: str = ""

    def usage(self) -> str:
        flag = "" if self.required else "[可选] "
        tail = f"，例如 {self.example}" if self.example else ""
        return f"{flag}{self.name}=（{self.description}{tail}）"


@dataclass
class Instance:
    """统一资源数据模型（论文 1.3：设计统一的资源数据模型，屏蔽底层差异）。"""

    provider: str
    id: str
    name: str
    status: str = "unknown"
    region: str = "-"
    public_ip: Optional[str] = None
    private_ip: Optional[str] = None
    ipv6: Optional[str] = None
    size: Optional[str] = None
    image: Optional[str] = None
    created_at: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    monthly_cost: Optional[float] = None
    password: Optional[str] = None
    """仅在创建瞬间由云厂商返回的初始密码（DigitalOcean 场景），不会持久化。"""

    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def address(self) -> str:
        """优先公网 IP，其次 IPv6，最后内网 IP。"""
        return self.public_ip or self.ipv6 or self.private_ip or "-"

    @property
    def ready(self) -> bool:
        return self.status == "running" and bool(self.public_ip or self.ipv6)

    def label(self) -> str:
        return f"{self.provider}:{self.name or self.id}"

    def to_public_dict(self) -> Dict[str, Any]:
        """对外（日志/JSON）表示，剔除 raw 与密码。"""
        return {
            "provider": self.provider,
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "region": self.region,
            "public_ip": self.public_ip,
            "private_ip": self.private_ip,
            "size": self.size,
            "image": self.image,
        }


@dataclass
class CreateSpec:
    """创建实例的参数集合，由指令解析器填充后交给适配器。"""

    name: str
    region: Optional[str] = None
    size: Optional[str] = None
    image: Optional[str] = None
    count: int = 1
    tags: List[str] = field(default_factory=list)
    ssh_keys: List[str] = field(default_factory=list)
    key_name: Optional[str] = None
    security_group_ids: List[str] = field(default_factory=list)
    subnet_id: Optional[str] = None
    user_data: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def resolved_summary(self) -> str:
        parts = [f"name={self.name}"]
        for key in ("region", "size", "image"):
            value = getattr(self, key)
            if value:
                parts.append(f"{key}={value}")
        if self.count > 1:
            parts.append(f"count={self.count}")
        return " ".join(parts)

    def with_name(self, name: str) -> "CreateSpec":
        """派生新规格（批量创建时用于生成带序号的实例名）。"""
        clone = CreateSpec(
            name=name,
            region=self.region,
            size=self.size,
            image=self.image,
            count=1,
            tags=list(self.tags),
            ssh_keys=list(self.ssh_keys),
            key_name=self.key_name,
            security_group_ids=list(self.security_group_ids),
            subnet_id=self.subnet_id,
            user_data=self.user_data,
            extra=dict(self.extra),
        )
        return clone


ProgressCallback = Callable[[float, Instance], None]


# --------------------------------------------------------------------------- #
# 适配器抽象基类
# --------------------------------------------------------------------------- #
class CloudProvider(ABC):
    """所有云平台适配器的统一接口。

    子类只需实现 4 个必需方法（鉴权校验 / 列举 / 创建 / 销毁），
    等待就绪、按 IP 查找、批量创建等编排逻辑由基类复用（论文 1.3：
    "屏蔽不同云厂商在鉴权方式、参数结构和响应格式上的底层差异"）。
    """

    #: 工厂标识，用于 ``/create do ...`` 这类指令参数
    name: ClassVar[str] = "base"
    display_name: ClassVar[str] = "Base"
    docs_url: ClassVar[Optional[str]] = None
    #: 指令里可用的简写（do / ec2 ...）
    aliases: ClassVar[Tuple[str, ...]] = ()
    #: 该平台需要的凭证字段（驱动 /bind 与 /providers）
    credential_fields: ClassVar[Tuple[CredentialField, ...]] = ()
    #: 支持的运维动作
    capabilities: ClassVar[FrozenSet[str]] = frozenset({"list", "create", "destroy", "status"})
    #: 视为"已就绪"的状态集合
    ready_states: ClassVar[FrozenSet[str]] = frozenset({"running"})
    #: 是否会在创建时返回初始密码（DigitalOcean 场景）
    returns_password: ClassVar[bool] = False

    def __init__(self, secrets: Mapping[str, Any], settings: "Settings", *,
                 label: str = "default", meta: Optional[Mapping[str, Any]] = None) -> None:
        self.secrets: Dict[str, Any] = {k: v for k, v in (secrets or {}).items() if v not in (None, "")}
        self.settings = settings
        self.label = label
        self.meta: Dict[str, Any] = dict(meta or {})

    # ------------------------------------------------------------------ #
    # 元信息
    # ------------------------------------------------------------------ #
    @classmethod
    def field_names(cls) -> Tuple[str, ...]:
        return tuple(field.name for field in cls.credential_fields)

    @classmethod
    def secret_field_names(cls) -> Tuple[str, ...]:
        return tuple(field.name for field in cls.credential_fields if field.secret)

    @classmethod
    def credential_usage(cls) -> str:
        """渲染 ``/bind`` 用法，例如 ``/bind digitalocean <名称> token=<...>``。"""
        parts = " ".join(field.name + "=" if field.secret else field.name + "=" for field in cls.credential_fields)
        return f"/bind {cls.name} <凭证名称> {parts}".rstrip()

    def missing_fields(self) -> List[str]:
        return [field.name for field in self.credential_fields
                if field.required and not self.secrets.get(field.name)]

    def require_credentials(self) -> None:
        missing = self.missing_fields()
        if missing:
            raise CredentialError(
                params={
                    "provider": self.display_name,
                    "fields": ", ".join(missing),
                    "usage": self.credential_usage(),
                },
                key="error.missing_credential_fields",
            )

    def supports(self, action: str) -> bool:
        return action in self.capabilities

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} label={self.label}>"

    def close(self) -> None:
        """释放底层连接（子类按需覆写）。"""

    def __enter__(self) -> "CloudProvider":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # 子类必须实现
    # ------------------------------------------------------------------ #
    @abstractmethod
    def validate_credentials(self) -> str:
        """校验凭证有效性，返回账户标识（用于 /bind 的回执）。"""

    @abstractmethod
    def list_instances(self) -> List[Instance]:
        """列举该账户下全部实例。"""

    @abstractmethod
    def create_instance(self, spec: CreateSpec) -> Instance:
        """下发创建任务，返回刚创建的实例（可能仍在初始化）。"""

    @abstractmethod
    def destroy_instance(self, instance_id: str) -> None:
        """销毁实例。"""

    # ------------------------------------------------------------------ #
    # 基类通用实现
    # ------------------------------------------------------------------ #
    def get_instance(self, instance_id: str) -> Instance:
        """按实例 ID 查询详情，默认实现从列表中查找。"""
        target = str(instance_id)
        for instance in self.list_instances():
            if str(instance.id) == target:
                return instance
        raise NotFound(params={"target": target, "provider": self.display_name},
                       key="error.instance_not_found")

    def find_instance(self, ref: str) -> Optional[Instance]:
        """按 实例 ID 或 IP 定位实例（论文 TC-03 输入为 IP）。"""
        target = (ref or "").strip()
        if not target:
            return None
        for instance in self.list_instances():
            if str(instance.id) == target or target in (
                instance.public_ip, instance.private_ip, instance.ipv6, instance.name,
            ):
                return instance
        return None

    def wait_until_ready(self, instance_id: str, *, timeout: Optional[float] = None,
                         interval: Optional[float] = None,
                         on_progress: Optional[ProgressCallback] = None) -> Instance:
        """轮询等待实例拿到公网地址（论文 5.2：Bot 进入轮询等待状态）。"""
        timeout = float(timeout if timeout is not None else self.settings.create_timeout_seconds)
        interval = float(interval if interval is not None else self.settings.create_poll_interval)
        started = time.monotonic()
        last: Optional[Instance] = None
        while True:
            elapsed = time.monotonic() - started
            try:
                current = self.get_instance(instance_id)
                last = current
            except (CloudError, NotFound):
                current = last
            if current is not None:
                if on_progress is not None:
                    try:
                        on_progress(elapsed, current)
                    except Exception:  # pragma: no cover - 进度回调不应影响主流程
                        pass
                if current.status in self.ready_states and (current.public_ip or current.ipv6):
                    return current
                if current.status in {"terminated", "error", "failed"}:
                    raise CloudError(
                        params={"provider": self.display_name, "id": instance_id,
                                "status": current.status},
                        key="error.instance_failed",
                        provider=self.name,
                    )
            if elapsed >= timeout:
                detail = last.status if last else "unknown"
                raise CloudTimeout(
                    params={"provider": self.display_name, "id": instance_id,
                            "seconds": int(timeout), "status": detail},
                    provider=self.name,
                )
            time.sleep(min(interval, max(1.0, timeout - elapsed)))

    def create_instances(self, spec: CreateSpec) -> List[Instance]:
        """按 ``count`` 批量创建（默认实现：逐个创建并自动编号命名）。"""
        count = max(1, int(spec.count or 1))
        created: List[Instance] = []
        for index in range(count):
            if count == 1:
                name = spec.name
            else:
                name = f"{spec.name}-{index + 1}"
            created.append(self.create_instance(spec.with_name(name)))
        return created

    # -- 参数归一化（子类覆写以提供别名/校验） ------------------------- #
    def resolve_region(self, token: Optional[str]) -> str:
        return (token or self.default_region()).strip()

    def resolve_size(self, token: Optional[str]) -> str:
        return (token or self.default_size()).strip()

    def resolve_image(self, token: Optional[str]) -> str:
        return (token or self.default_image()).strip()

    def default_region(self) -> str:
        raise NotImplementedError

    def default_size(self) -> str:
        raise NotImplementedError

    def default_image(self) -> str:
        raise NotImplementedError

    def describe_options(self) -> str:
        """``/providers`` 中展示的默认参数与凭证要求。"""
        lines = [
            f"provider : {self.name}",
            f"说明     : {self.display_name}",
            f"默认区域 : {self.safe_default(self.default_region)}",
            f"默认规格 : {self.safe_default(self.default_size)}",
            f"默认镜像 : {self.safe_default(self.default_image)}",
        ]
        if self.docs_url:
            lines.append(f"文档     : {self.docs_url}")
        return "\n".join(lines)

    @staticmethod
    def safe_default(getter: Callable[[], str]) -> str:
        try:
            return getter()
        except Exception:  # pragma: no cover
            return "-"

