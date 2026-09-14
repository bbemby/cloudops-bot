"""数据持久层：SQLite 封装（论文 5.3 数据库设计）。

对应三张核心表：

=============== ==========================================================
users           管理员的即时通讯数字 ID、注册时间与权限等级
credentials     云平台标识 + 加密后的 API Token，外键关联 users
operation_log   每次资源调度的流水（时间戳 / 操作类型 / 目标云商 / 状态）
=============== ==========================================================

另有 ``tasks`` 表用于跟踪需要数十秒轮询的异步操作（创建实例），
属于 E-R 图之外的工程扩展，详见 ``docs/ARCHITECTURE.md``。

实现上刻意保持"极简"：标准库 ``sqlite3`` + 每次操作一个短连接（WAL 模式），
无需连接池，符合论文 2.3 对轻量级运行环境的要求。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

from .errors import CloudError, CloudOpsError, NotFound
from .models import (
    ROLE_GUEST,
    AccountStats,
    Credential,
    OperationLog,
    TaskRecord,
    User,
)
from .utils import dumps, loads, now_iso

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id   INTEGER PRIMARY KEY,
    username      TEXT,
    role          TEXT NOT NULL DEFAULT 'guest',
    created_at    TEXT NOT NULL,
    last_seen_at  TEXT
);

CREATE TABLE IF NOT EXISTS credentials (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id      INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    provider      TEXT NOT NULL,
    label         TEXT NOT NULL,
    secret_blob   TEXT NOT NULL,
    meta_json     TEXT NOT NULL DEFAULT '{}',
    is_active     INTEGER NOT NULL DEFAULT 0,
    shared        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    updated_at    TEXT,
    UNIQUE (owner_id, provider, label)
);

CREATE TABLE IF NOT EXISTS operation_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id   INTEGER NOT NULL,
    provider      TEXT,
    action        TEXT NOT NULL,
    target        TEXT,
    status        TEXT NOT NULL,
    detail        TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,
    telegram_id   INTEGER NOT NULL,
    provider      TEXT NOT NULL,
    kind          TEXT NOT NULL,
    status        TEXT NOT NULL,
    ref           TEXT,
    detail        TEXT,
    payload_json  TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL,
    updated_at    TEXT
);

CREATE TABLE IF NOT EXISTS schema_meta (
    key           TEXT PRIMARY KEY,
    value         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_credentials_provider ON credentials(provider, is_active);
CREATE INDEX IF NOT EXISTS idx_log_created ON operation_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_log_user ON operation_log(telegram_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, created_at DESC);
"""


class DuplicateCredential(CloudOpsError):
    """同一用户名下的同 provider + label 已存在。"""

    default_key = "error.duplicate_credential"


def _to_user(row: sqlite3.Row) -> User:
    return User(
        telegram_id=row["telegram_id"],
        username=row["username"],
        role=row["role"],
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
    )


def _to_credential(row: sqlite3.Row) -> Credential:
    return Credential(
        id=row["id"],
        owner_id=row["owner_id"],
        provider=row["provider"],
        label=row["label"],
        secret_blob=row["secret_blob"],
        meta=loads(row["meta_json"], default={}) or {},
        is_active=bool(row["is_active"]),
        shared=bool(row["shared"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _to_log(row: sqlite3.Row) -> OperationLog:
    return OperationLog(
        id=row["id"],
        telegram_id=row["telegram_id"],
        provider=row["provider"],
        action=row["action"],
        target=row["target"],
        status=row["status"],
        detail=row["detail"],
        created_at=row["created_at"],
    )


def _to_task(row: sqlite3.Row) -> TaskRecord:
    return TaskRecord(
        id=row["id"],
        telegram_id=row["telegram_id"],
        provider=row["provider"],
        kind=row["kind"],
        status=row["status"],
        ref=row["ref"],
        detail=row["detail"],
        payload=loads(row["payload_json"], default={}) or {},
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class Database:
    """SQLite 访问对象。所有写操作即时提交，避免长事务占用锁。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    # ------------------------------------------------------------------ #
    # 连接与初始化
    # ------------------------------------------------------------------ #
    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        """建库建表（幂等）。"""
        parent = self.path.parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            # WAL 模式让"读日志"与"写流水"互不阻塞，适合单机轻量部署
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(SCHEMA_VERSION),),
            )

    @property
    def exists(self) -> bool:
        return self.path.exists()

    # ------------------------------------------------------------------ #
    # 用户
    # ------------------------------------------------------------------ #
    def get_user(self, telegram_id: int) -> Optional[User]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE telegram_id = ?", (int(telegram_id),)
            ).fetchone()
        return _to_user(row) if row else None

    def ensure_user(self, telegram_id: int, username: Optional[str] = None,
                    *, role: Optional[str] = None) -> User:
        """用户首次出现时自动登记；再次出现时刷新用户名与活跃时间。"""
        telegram_id = int(telegram_id)
        timestamp = now_iso()
        existing = self.get_user(telegram_id)
        with self.connect() as conn:
            if existing is None:
                conn.execute(
                    "INSERT INTO users(telegram_id, username, role, created_at, last_seen_at) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (telegram_id, username, role or ROLE_GUEST, timestamp, timestamp),
                )
            else:
                if role and role != existing.role:
                    conn.execute(
                        "UPDATE users SET username = ?, role = ?, last_seen_at = ? "
                        "WHERE telegram_id = ?",
                        (username or existing.username, role, timestamp, telegram_id),
                    )
                else:
                    conn.execute(
                        "UPDATE users SET username = ?, last_seen_at = ? WHERE telegram_id = ?",
                        (username or existing.username, timestamp, telegram_id),
                    )
        user = self.get_user(telegram_id)
        assert user is not None  # 刚写入，必然存在
        return user

    def set_role(self, telegram_id: int, role: str) -> User:
        self.ensure_user(telegram_id, role=role)
        with self.connect() as conn:
            conn.execute("UPDATE users SET role = ? WHERE telegram_id = ?", (role, int(telegram_id)))
        user = self.get_user(telegram_id)
        assert user is not None
        return user

    def list_users(self, limit: int = 50) -> List[User]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM users ORDER BY last_seen_at DESC, telegram_id LIMIT ?", (limit,)
            ).fetchall()
        return [_to_user(row) for row in rows]

    def count_users(self) -> int:
        with self.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"])

    # ------------------------------------------------------------------ #
    # 凭证
    # ------------------------------------------------------------------ #
    def add_credential(self, owner_id: int, provider: str, label: str, secret_blob: str,
                       meta: Optional[Dict[str, Any]] = None, *, shared: bool = True,
                       activate: bool = True) -> Credential:
        """写入一条凭证。

        ``owner_id`` 外键指向 ``users(telegram_id)``；若该用户尚未登记，
        这里会先自动登记（与 :meth:`ensure_user` 语义一致），避免因为
        "谁先写入"的顺序问题触发外键失败。
        """
        owner_id = int(owner_id)
        if self.get_user(owner_id) is None:
            self.ensure_user(owner_id)
        timestamp = now_iso()
        try:
            with self.connect() as conn:
                cursor = conn.execute(
                    "INSERT INTO credentials(owner_id, provider, label, secret_blob, meta_json, "
                    "is_active, shared, created_at, updated_at) VALUES(?, ?, ?, ?, ?, 0, ?, ?, ?)",
                    (
                        owner_id,
                        provider,
                        label,
                        secret_blob,
                        dumps(meta or {}),
                        1 if shared else 0,
                        timestamp,
                        timestamp,
                    ),
                )
                credential_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            # UNIQUE(owner_id, provider, label) 才是"重名"，其它完整性错误要如实上报
            if "UNIQUE" in str(exc).upper():
                raise DuplicateCredential(
                    params={"provider": provider, "label": label},
                    key="error.duplicate_credential",
                ) from exc
            raise CloudError(
                f"凭证写入失败：{exc}", provider=provider, key="error.generic"
            ) from exc

        if activate:
            self.set_active_credential(credential_id, owner_id=owner_id)
        credential = self.get_credential(credential_id)
        assert credential is not None
        return credential

    def get_credential(self, credential_id: int) -> Optional[Credential]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM credentials WHERE id = ?", (int(credential_id),)).fetchone()
        return _to_credential(row) if row else None

    def list_credentials(self, *, provider: Optional[str] = None, owner_id: Optional[int] = None,
                         shared_only: bool = False, limit: int = 100) -> List[Credential]:
        clauses: List[str] = []
        params: List[Any] = []
        if provider:
            clauses.append("provider = ?")
            params.append(provider)
        if owner_id is not None:
            clauses.append("owner_id = ?")
            params.append(int(owner_id))
        if shared_only:
            clauses.append("shared = 1")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM credentials {where} ORDER BY provider, created_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [_to_credential(row) for row in rows]

    def count_credentials(self, provider: Optional[str] = None) -> int:
        with self.connect() as conn:
            if provider:
                row = conn.execute(
                    "SELECT COUNT(*) AS c FROM credentials WHERE provider = ?", (provider,)
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) AS c FROM credentials").fetchone()
        return int(row["c"])

    def set_active_credential(self, credential_id: int, *, owner_id: Optional[int] = None) -> Credential:
        """切换某用户在某云平台上的"当前工作区"（论文 4.2 凭证与环境管理）。"""
        credential = self.get_credential(credential_id)
        if credential is None or (owner_id is not None and credential.owner_id != int(owner_id)):
            raise NotFound(params={"target": f"credential#{credential_id}"}, key="error.credential_not_found")
        timestamp = now_iso()
        with self.connect() as conn:
            conn.execute(
                "UPDATE credentials SET is_active = 0, updated_at = ? "
                "WHERE owner_id = ? AND provider = ?",
                (timestamp, credential.owner_id, credential.provider),
            )
            conn.execute(
                "UPDATE credentials SET is_active = 1, updated_at = ? WHERE id = ?",
                (timestamp, credential.id),
            )
        updated = self.get_credential(credential_id)
        assert updated is not None
        return updated

    def resolve_active_credential(self, provider: str, user_id: int) -> Optional[Credential]:
        """解析"该用户在某云平台上应当使用哪个凭证"。

        顺序：本人显式激活的 → 本人最近绑定的 → 其他管理员共享且激活的
        → 其他管理员共享的任意一个。
        """
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM credentials WHERE provider = ? AND owner_id = ? AND is_active = 1 "
                "ORDER BY created_at DESC LIMIT 1",
                (provider, int(user_id)),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM credentials WHERE provider = ? AND owner_id = ? "
                    "ORDER BY is_active DESC, created_at DESC LIMIT 1",
                    (provider, int(user_id)),
                ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM credentials WHERE provider = ? AND shared = 1 AND is_active = 1 "
                    "ORDER BY created_at DESC LIMIT 1",
                    (provider,),
                ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM credentials WHERE provider = ? AND shared = 1 "
                    "ORDER BY is_active DESC, created_at DESC LIMIT 1",
                    (provider,),
                ).fetchone()
        return _to_credential(row) if row else None

    def delete_credential(self, credential_id: int, *, owner_id: Optional[int] = None) -> Credential:
        credential = self.get_credential(credential_id)
        if credential is None or (owner_id is not None and credential.owner_id != int(owner_id)):
            raise NotFound(params={"target": f"credential#{credential_id}"}, key="error.credential_not_found")
        with self.connect() as conn:
            conn.execute("DELETE FROM credentials WHERE id = ?", (credential_id,))
        # 若删掉的是激活项，则把该用户该平台下最近的一条重新置为激活
        if credential.is_active:
            remaining = self.list_credentials(provider=credential.provider, owner_id=credential.owner_id)
            if remaining:
                self.set_active_credential(remaining[0].id)
        return credential

    def provider_credential_counts(self) -> Dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT provider, COUNT(*) AS c FROM credentials GROUP BY provider ORDER BY provider"
            ).fetchall()
        return {row["provider"]: int(row["c"]) for row in rows}

    # ------------------------------------------------------------------ #
    # 操作日志
    # ------------------------------------------------------------------ #
    def log_operation(self, telegram_id: int, action: str, status: str, *,
                      provider: Optional[str] = None, target: Optional[str] = None,
                      detail: Optional[str] = None) -> OperationLog:
        timestamp = now_iso()
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO operation_log(telegram_id, provider, action, target, status, detail, "
                "created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    int(telegram_id),
                    provider,
                    action,
                    target,
                    status,
                    (detail or "")[:2000] or None,
                    timestamp,
                ),
            )
            log_id = int(cursor.lastrowid)
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM operation_log WHERE id = ?", (log_id,)).fetchone()
        return _to_log(row)

    def list_logs(self, limit: int = 10, *, telegram_id: Optional[int] = None,
                  provider: Optional[str] = None, status: Optional[str] = None) -> List[OperationLog]:
        clauses: List[str] = []
        params: List[Any] = []
        if telegram_id is not None:
            clauses.append("telegram_id = ?")
            params.append(int(telegram_id))
        if provider:
            clauses.append("provider = ?")
            params.append(provider)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM operation_log {where} ORDER BY id DESC LIMIT ?", params
            ).fetchall()
        return [_to_log(row) for row in rows]

    def count_operations(self) -> int:
        with self.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) AS c FROM operation_log").fetchone()["c"])

    # ------------------------------------------------------------------ #
    # 异步任务
    # ------------------------------------------------------------------ #
    def create_task(self, task_id: str, telegram_id: int, provider: str, kind: str, *,
                    ref: Optional[str] = None, payload: Optional[Dict[str, Any]] = None) -> TaskRecord:
        from .models import TASK_PENDING

        timestamp = now_iso()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO tasks(id, telegram_id, provider, kind, status, ref, detail, payload_json, "
                "created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)",
                (
                    task_id,
                    int(telegram_id),
                    provider,
                    kind,
                    TASK_PENDING,
                    ref,
                    dumps(payload or {}),
                    timestamp,
                    timestamp,
                ),
            )
        task = self.get_task(task_id)
        assert task is not None
        return task

    def update_task(self, task_id: str, *, status: Optional[str] = None, ref: Optional[str] = None,
                    detail: Optional[str] = None) -> Optional[TaskRecord]:
        assignments: List[str] = ["updated_at = ?"]
        params: List[Any] = [now_iso()]
        if status:
            assignments.append("status = ?")
            params.append(status)
        if ref:
            assignments.append("ref = ?")
            params.append(ref)
        if detail is not None:
            assignments.append("detail = ?")
            params.append(detail[:2000])
        params.append(task_id)
        with self.connect() as conn:
            conn.execute(f"UPDATE tasks SET {', '.join(assignments)} WHERE id = ?", params)
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> Optional[TaskRecord]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _to_task(row) if row else None

    def list_tasks(self, limit: int = 10, *, telegram_id: Optional[int] = None,
                   status: Optional[str] = None, active_only: bool = False) -> List[TaskRecord]:
        clauses: List[str] = []
        params: List[Any] = []
        if telegram_id is not None:
            clauses.append("telegram_id = ?")
            params.append(int(telegram_id))
        if status:
            clauses.append("status = ?")
            params.append(status)
        if active_only:
            clauses.append("status IN ('pending', 'running')")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM tasks {where} ORDER BY created_at DESC LIMIT ?", params
            ).fetchall()
        return [_to_task(row) for row in rows]

    def count_tasks(self, *, active_only: bool = False) -> int:
        with self.connect() as conn:
            if active_only:
                row = conn.execute(
                    "SELECT COUNT(*) AS c FROM tasks WHERE status IN ('pending', 'running')"
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()
        return int(row["c"])

    # ------------------------------------------------------------------ #
    # 统计
    # ------------------------------------------------------------------ #
    def stats(self) -> AccountStats:
        with self.connect() as conn:
            users = int(conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"])
            credentials = int(conn.execute("SELECT COUNT(*) AS c FROM credentials").fetchone()["c"])
            operations = int(conn.execute("SELECT COUNT(*) AS c FROM operation_log").fetchone()["c"])
            tasks = int(conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"])
        return AccountStats(users=users, credentials=credentials, operations=operations, tasks=tasks)

    def vacuum(self) -> None:
        with self.connect() as conn:
            conn.execute("VACUUM")

    def tables(self) -> Sequence[str]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall()
        return [row["name"] for row in rows]
