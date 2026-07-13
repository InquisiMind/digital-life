"""Phase 4 todo 单一真相架构 — 契约单测。

每个测试用独立 uuid pid + 独立 title,避免 dedup 跨测试污染。
"""

from __future__ import annotations

import os
import uuid

import pytest


@pytest.fixture(autouse=True)
def _instance_context(monkeypatch):
    iid = f"test-inst-{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("DIGITAL_LIFE_INSTANCE_ID", iid)
    from domain.lifecycle.events import set_instance_context
    from infrastructure.config import set_current_instance_id
    tok1 = set_instance_context(iid)
    tok2 = set_current_instance_id(iid)
    yield iid
    from domain.lifecycle.events import reset_instance_context
    from infrastructure.config import reset_current_instance_id
    reset_instance_context(tok1)
    reset_current_instance_id(tok2)


def _unique_pid():
    return f"phase4-test-{uuid.uuid4().hex[:6]}"


def _unique_title(prefix="test"):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _cleanup_todo(tid: str):
    if not tid:
        return
    from infrastructure.persistence.global_todos import get_global_todos_db
    db = get_global_todos_db()
    db.execute("DELETE FROM todos WHERE id = ?", (tid,))
    db.commit(); db.close()


def _cleanup_project(pid: str):
    """清掉这个 test pid 下所有 todo。"""
    from infrastructure.persistence.global_todos import get_global_todos_db
    db = get_global_todos_db()
    db.execute("DELETE FROM todos WHERE project_id = ?", (pid,))
    db.commit(); db.close()


class TestAssigneePositionColumn:
    def test_column_exists(self):
        from infrastructure.persistence.global_todos import get_global_todos_db
        db = get_global_todos_db()
        cols = {r[1] for r in db.execute("PRAGMA table_info(todos)").fetchall()}
        db.close()
        assert "assignee_position" in cols

    def test_create_task_persists_assignee_position(self):
        from domain.todos.crud import create_task
        pid = _unique_pid(); title = _unique_title("ap")
        r = create_task(title=title, project_id=pid, assignee_position="architect")
        assert r["ok"]
        from infrastructure.persistence.global_todos import get_global_todos_db
        db = get_global_todos_db()
        row = db.execute(
            "SELECT assignee_position FROM todos WHERE id = ?", (r["task"]["id"],)
        ).fetchone()
        _cleanup_todo(r["task"]["id"])
        assert row and row["assignee_position"] == "architect"

    def test_task_to_dict_has_assignee_position_key(self):
        from domain.todos.core.models import task_to_dict, Task
        d = task_to_dict(Task(id="x", title="t", project_id="p"))
        assert "assignee_position" in d


class TestCreateDeliverableInGlobal:
    def test_create_appears_in_global_todos(self):
        pid = _unique_pid(); title = _unique_title("cd")
        from domain.project.crud import create_deliverable
        did = create_deliverable(
            db=None, title=title, description="d", project_id=pid,
            assignee_position="developer",
        )
        assert did, "create_deliverable 必须返回 id"
        from infrastructure.persistence.global_todos import get_global_todos_db
        db = get_global_todos_db()
        row = db.execute(
            "SELECT id, title, linked_deliverable_id, assignee_position FROM todos WHERE id = ?",
            (did,),
        ).fetchone()
        _cleanup_project(pid)
        assert row is not None
        assert row["title"] == title
        assert row["assignee_position"] == "developer"
        assert row["linked_deliverable_id"]


class TestListMatchesGlobalFilter:
    def test_list_returns_global_rows(self):
        """建 1 个,确认 list 返回它。"""
        pid = _unique_pid()
        from domain.project.crud import create_deliverable, list_deliverables
        d = create_deliverable(
            db=None, title=_unique_title("zz"),
            project_id=pid, assignee_position="dev",
        )
        assert d, "create 失败(可能 dedup)"

        got = list_deliverables(db=None, project_id=pid)
        got_ids = {x["id"] for x in got}
        _cleanup_project(pid)
        assert d in got_ids


class TestUpdateDeliverableInPlace:
    def test_update_status_reflects_global(self):
        pid = _unique_pid()
        from domain.project.crud import (
            create_deliverable, update_deliverable, get_deliverable,
        )
        did = create_deliverable(
            db=None, title=_unique_title("up"), project_id=pid,
        )
        ok = update_deliverable(db=None, deliverable_id=did, project_id=pid, status="done")
        assert ok
        t = get_deliverable(db=None, deliverable_id=did)
        _cleanup_project(pid)
        assert t["status"] == "done"


class TestCancelOnProjectDelete:
    def test_cancel_marks_status_keeps_row(self):
        pid = _unique_pid()
        from domain.project.crud import create_deliverable
        from application.api.system_routes import _cancel_todos_for_deleted_project
        did = create_deliverable(db=None, title=_unique_title("cancel"), project_id=pid)
        n = _cancel_todos_for_deleted_project(pid)
        assert n >= 1, f"应 cancel 至少 1 行(刚建的那条),实际 {n}"

        from infrastructure.persistence.global_todos import get_global_todos_db
        db = get_global_todos_db()
        row = db.execute("SELECT status FROM todos WHERE id = ?", (did,)).fetchone()
        _cleanup_project(pid)

        assert row and row["status"] == "cancelled"


class TestGetDeliverable:
    def test_get_returns_linked_todo(self):
        pid = _unique_pid()
        from domain.project.crud import create_deliverable, get_deliverable
        title = _unique_title("get")
        did = create_deliverable(
            db=None, title=title, description="hello", project_id=pid,
        )
        t = get_deliverable(db=None, deliverable_id=did)
        _cleanup_project(pid)
        assert t is not None
        assert t["title"] == title
        assert t["description"] == "hello"
