"""领域模型：与 SQLite 表结构一一对应的轻量数据类。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# 角色定义：admin（管理员）/ readonly（只读观察者）/ guest（未授权访客）
ROLE_ADMIN = "admin"
ROLE_READONLY = "readonly"
ROLE_GUEST = "guest"

ROLES = (ROLE_ADMIN, ROLE_READONLY, ROLE_GUEST)

ROLE_LABELS: Dict[str, str] = {
    ROLE_ADMIN: "管理员",
    ROLE_READONLY: "只读",
    ROLE_GUEST: "访客",
}

# 任务状态
TASK_PENDING = "pending"
TASK_RUNNING = "running"
TASK_SUCCESS = "success"
TASK_FAILED = "failed"
TASK_CANCELLED = "cancelled"

# 操作日志状态
LOG_SUCCESS = "success"
LOG_FAILED = "failed"
LOG_DENIED = "denied"


@dataclass
class User:
    """用户表：管理员/观察者的即时通讯数字 ID、角色与注册时间（论文 5.3）。"""

    telegram_id: int
    username: Optional[str] = None
    role: str = ROLE_GUEST
    created_at: str = ""
    last_seen_at: Optional[str] = None

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    @property
    def can_operate(self) -> bool:
        """是否能执行写操作（创建/销毁/绑定凭证）。"""
        return self.role == ROLE_ADMIN

    @property
    def can_read(self) -> bool:
        """是否能执行读操作（查询实例、查看日志）。"""
        return self.role in (ROLE_ADMIN, ROLE_READONLY)

    @property
    def role_label(self) -> str:
        return ROLE_LABELS.get(self.role, self.role)

    def display(self) -> str:
        if self.username:
            return f"@{self.username} ({self.telegram_id})"
        return str(self.telegram_id)


@dataclass
class Credential:
    """凭证表：云平台标识、加密后的 API Token，并通过外键与用户表关联（论文 5.3）。

    ``secret_blob`` 为 Fernet 密文，明文只存在于内存中，落库前完成加密。
    """

    id: int
    owner_id: int
    provider: str
    label: str
    secret_blob: str
    meta: Dict[str, Any] = field(default_factory=dict)
    is_active: bool = False
    shared: bool = True
    created_at: str = ""
    updated_at: Optional[str] = None

    @property
    def display_name(self) -> str:
        return f"{self.provider}:{self.label}"

    def meta_value(self, key: str, default: Any = None) -> Any:
        return (self.meta or {}).get(key, default)


@dataclass
class OperationLog:
    """操作日志表：记录每一次资源调度的流水，便于后期审计（论文 5.3）。"""

    id: int
    telegram_id: int
    provider: Optional[str]
    action: str
    target: Optional[str]
    status: str
    detail: Optional[str] = None
    created_at: str = ""

    @property
    def ok(self) -> bool:
        return self.status == LOG_SUCCESS


@dataclass
class TaskRecord:
    """异步任务记录表（论文 E-R 之外的工程扩展）。

    ``/create`` 这类需要数十秒轮询的操作会被登记为任务，
    使得"任务已下发 → 完成后主动推送"的交互（论文 5.2 时序图）可追踪、可审计，
    即使进程重启也能在日志中找回线索。
    """

    id: str
    telegram_id: int
    provider: str
    kind: str
    status: str = TASK_PENDING
    ref: Optional[str] = None
    detail: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: Optional[str] = None

    @property
    def finished(self) -> bool:
        return self.status in (TASK_SUCCESS, TASK_FAILED, TASK_CANCELLED)


@dataclass
class AccountStats:
    """``/whoami`` 或 ``manage.py stats`` 使用的聚合视图。"""

    users: int = 0
    credentials: int = 0
    operations: int = 0
    instances_tracked: int = 0
    tasks: int = 0

    def as_lines(self) -> List[str]:
        return [
            f"users={self.users}",
            f"credentials={self.credentials}",
            f"operations={self.operations}",
            f"tasks={self.tasks}",
            f"tracked_instances={self.instances_tracked}",
        ]
