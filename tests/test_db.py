"""SQLite 持久化层测试：用户 / 凭证 / 日志 / 任务。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from support import ROOT  # noqa: F401

from cloudops.db import Database, DuplicateCredential
from cloudops.models import (
    LOG_DENIED,
    LOG_SUCCESS,
    ROLE_ADMIN,
    ROLE_GUEST,
    ROLE_READONLY,
    TASK_RUNNING,
    TASK_SUCCESS,
)
from cloudops.utils import parse_iso


class DatabaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "data" / "cloudops.db"
        self.db = Database(self.path)
        self.db.initialize()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- 初始化 ---------------------------------------------------------- #
    def test_initialize_is_idempotent_and_creates_tables(self) -> None:
        self.db.initialize()
        tables = set(self.db.tables())
        for expected in ("users", "credentials", "operation_log", "tasks", "schema_meta"):
            self.assertIn(expected, tables)
        self.assertTrue(self.db.exists)

    # -- 用户 ------------------------------------------------------------ #
    def test_ensure_user_registers_then_refreshes(self) -> None:
        user = self.db.ensure_user(111, "alice")
        self.assertEqual(user.role, ROLE_GUEST)
        self.assertIsNotNone(parse_iso(user.created_at))

        again = self.db.ensure_user(111, "alice2")
        self.assertEqual(again.role, ROLE_GUEST)
        self.assertEqual(again.username, "alice2")

        promoted = self.db.ensure_user(111, "alice2", role=ROLE_ADMIN)
        self.assertEqual(promoted.role, ROLE_ADMIN)
        self.assertEqual(self.db.count_users(), 1)

    def test_set_role_and_list_users(self) -> None:
        self.db.ensure_user(1, "a")
        self.db.ensure_user(2, "b")
        self.db.set_role(2, ROLE_READONLY)
        roles = {user.telegram_id: user.role for user in self.db.list_users()}
        self.assertEqual(roles, {1: ROLE_GUEST, 2: ROLE_READONLY})
        self.assertTrue(self.db.get_user(2).can_read)
        self.assertFalse(self.db.get_user(2).can_operate)

    # -- 凭证 ------------------------------------------------------------ #
    def _add(self, provider: str = "mock", label: str = "default", owner: int = 111,
             *, activate: bool = True):
        return self.db.add_credential(owner, provider, label, "encrypted-blob",
                                      meta={"identity": "demo"}, activate=activate)

    def test_add_and_resolve_active_credential(self) -> None:
        first = self._add(label="first")
        second = self._add(label="second")
        self.assertTrue(second.is_active)
        self.assertFalse(self.db.get_credential(first.id).is_active)

        active = self.db.resolve_active_credential("mock", 111)
        self.assertEqual(active.id, second.id)

        # 共享凭证对其它用户可见；私有凭证不可见
        self.assertEqual(self.db.resolve_active_credential("mock", 999).id, second.id)
        self.db.add_credential(777, "mock", "private", "blob", shared=False)
        self.assertEqual(self.db.resolve_active_credential("mock", 777).owner_id, 777)

    def test_duplicate_label_is_rejected(self) -> None:
        self._add(label="dup")
        with self.assertRaises(DuplicateCredential) as ctx:
            self._add(label="dup")
        self.assertEqual(ctx.exception.key, "error.duplicate_credential")

    def test_same_label_under_different_owners_is_allowed(self) -> None:
        mine = self._add(label="prod", owner=111)
        theirs = self._add(label="prod", owner=222)
        self.assertNotEqual(mine.id, theirs.id)

    def test_activate_only_affects_same_scope(self) -> None:
        mine = self._add(label="mine", owner=111)
        other = self._add(label="other", owner=222)
        self.assertTrue(mine.is_active)
        self.assertTrue(other.is_active)

        self.db.set_active_credential(mine.id)
        # "当前工作区"是 per-user 的：切换自己的凭证不影响别人的激活态
        self.assertTrue(self.db.get_credential(mine.id).is_active)
        self.assertTrue(self.db.get_credential(other.id).is_active)

        third = self._add(label="third", owner=111, activate=False)
        self.db.set_active_credential(third.id, owner_id=111)
        self.assertTrue(self.db.get_credential(third.id).is_active)
        self.assertFalse(self.db.get_credential(mine.id).is_active)
        # 跨用户激活会被拒绝
        with self.assertRaises(Exception):
            self.db.set_active_credential(third.id, owner_id=222)

    def test_delete_credential(self) -> None:
        credential = self._add()
        removed = self.db.delete_credential(credential.id, owner_id=111)
        self.assertEqual(removed.id, credential.id)
        self.assertIsNone(self.db.get_credential(credential.id))
        self.assertEqual(self.db.count_credentials("mock"), 0)

    def test_provider_credential_counts(self) -> None:
        self._add(provider="mock", label="a")
        self._add(provider="aws", label="b")
        counts = self.db.provider_credential_counts()
        self.assertEqual(counts.get("mock"), 1)
        self.assertEqual(counts.get("aws"), 1)

    # -- 日志与任务 ------------------------------------------------------ #
    def test_operation_log(self) -> None:
        self.db.ensure_user(111, "alice")
        self.db.log_operation(111, "create", LOG_SUCCESS, provider="mock",
                              target="mock-1001", detail="ok")
        self.db.log_operation(111, "bind", LOG_DENIED, detail="not allowed")

        rows = self.db.list_logs(10)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].action, "bind")       # 最新的在前
        self.assertEqual(rows[1].status, LOG_SUCCESS)
        self.assertEqual(len(self.db.list_logs(10, telegram_id=111)), 2)
        self.assertEqual(self.db.count_operations(), 2)

    def test_task_lifecycle(self) -> None:
        self.db.create_task("TASK1", 111, "mock", "create",
                            payload={"name": "web-01", "count": 1})
        task = self.db.get_task("TASK1")
        self.assertEqual(task.payload["name"], "web-01")
        self.assertFalse(task.finished)
        self.assertEqual(self.db.count_tasks(active_only=True), 1)

        self.db.update_task("TASK1", status=TASK_RUNNING, ref="mock-1001", detail="accepted")
        self.db.update_task("TASK1", status=TASK_SUCCESS, ref="mock-1001", detail="ready")
        task = self.db.get_task("TASK1")
        self.assertEqual(task.status, TASK_SUCCESS)
        self.assertEqual(task.ref, "mock-1001")
        self.assertTrue(task.finished)
        self.assertEqual(self.db.count_tasks(active_only=True), 0)
        self.assertEqual(self.db.count_tasks(), 1)

    def test_stats_and_vacuum(self) -> None:
        self.db.ensure_user(111, "alice")
        self._add()
        self.db.log_operation(111, "list", LOG_SUCCESS)
        self.db.create_task("T2", 111, "mock", "create")
        stats = self.db.stats()
        self.assertEqual(stats.users, 1)
        self.assertEqual(stats.credentials, 1)
        self.assertEqual(stats.operations, 1)
        self.assertEqual(stats.tasks, 1)
        self.assertTrue(any("users=" in line for line in stats.as_lines()))
        self.db.vacuum()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
