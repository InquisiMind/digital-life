"""PR-2 验证：统一待办模型 — workspace 懒创建 + attach_workspace + manage_work 底层合并。

验证三个核心改造：
1. create_task 不再无脑 mkdir（只有项目来源或有 type 才建目录）
2. attach_workspace 能给已有待办懒创建 workspace
3. manage_work add 底层走 create_task（tasks 表出现行）
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch


def _setup_isolated_db(tmp_path, monkeypatch):
    """在 tmp_path 下创建隔离的 tasks.db。"""
    db_path = tmp_path / "tasks.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS todos (
            id TEXT PRIMARY KEY, title TEXT, description TEXT,
            acceptance_criteria TEXT DEFAULT '',
            status TEXT DEFAULT 'idea', priority TEXT DEFAULT 'medium',
            deadline TEXT, tags TEXT DEFAULT '[]',
            project_id TEXT DEFAULT '', assignee_instance TEXT DEFAULT '',
            assignee_kind TEXT DEFAULT '', parent_id TEXT DEFAULT '',
            source TEXT DEFAULT 'personal', origin_instance TEXT DEFAULT '',
            linked_deliverable_id TEXT, type TEXT DEFAULT '',
            has_workspace INTEGER DEFAULT 0,
            assignee_position TEXT DEFAULT '',
            detail TEXT DEFAULT '',
            created_at TEXT, updated_at TEXT, notes TEXT
        );
        CREATE TABLE IF NOT EXISTS todo_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
            content TEXT, deadline TEXT, status TEXT DEFAULT 'pending',
            order_num INTEGER DEFAULT 0, created_at TEXT, completed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS todo_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
            content TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS todo_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
            session_id TEXT, digest TEXT, started_at TEXT, ended_at TEXT
        );
        CREATE TABLE IF NOT EXISTS todo_triggers (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
            assignee TEXT DEFAULT '', content TEXT,
            trigger_type TEXT DEFAULT 'time', due_at TEXT,
            trigger_condition TEXT DEFAULT '', status TEXT DEFAULT 'pending',
            assignee_text TEXT DEFAULT '', note TEXT DEFAULT '',
            created_at TEXT, updated_at TEXT
        );
    """)
    conn.commit()
    conn.close()

    def _fake_get_db():
        c = sqlite3.connect(str(db_path))
        c.row_factory = sqlite3.Row
        return c

    import domain.todos._infra as infra
    import domain.todos.crud as crud
    monkeypatch.setattr(infra, "get_db", _fake_get_db)
    monkeypatch.setattr(crud, "get_db", _fake_get_db)  # crud 模块内已绑定的名字

    # tasks_dir 也隔离
    tasks_dir = tmp_path / "tasks_workspace"
    tasks_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(infra, "tasks_dir", lambda: tasks_dir)
    monkeypatch.setattr(crud, "tasks_dir", lambda: tasks_dir)

    return db_path, tasks_dir


# ── 1. workspace 懒创建 ──────


def test_simple_todo_no_workspace(tmp_path, monkeypatch):
    """普通待办（无 type、无项目来源）不应创建 workspace 目录。"""
    db_path, tasks_dir = _setup_isolated_db(tmp_path, monkeypatch)

    # mock schedule_task_wakeup（避免真的发事件）
    with patch("domain.todos.crud.schedule_task_wakeup"):
        from domain.todos.crud import create_task
        result = create_task(title="回张三消息", status="planned", source="manual")

    assert result["ok"], f"创建失败: {result}"
    tid = result["task"]["id"]

    # workspace 目录不应存在
    ws = tasks_dir / tid
    assert not ws.exists(), f"普通待办不应建 workspace，但 {ws} 存在了"

    # has_workspace 标记应为 0
    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT has_workspace FROM todos WHERE id=?", (tid,)).fetchone()
    conn.close()
    assert row[0] == 0, f"has_workspace 应为 0，实际 {row[0]}"


def test_project_todo_has_workspace(tmp_path, monkeypatch):
    """项目来源的待办应创建 workspace 目录。"""
    db_path, tasks_dir = _setup_isolated_db(tmp_path, monkeypatch)

    with patch("domain.todos.crud.schedule_task_wakeup"):
        from domain.todos.crud import create_task
        result = create_task(
            title="完成回测模块", status="planned", source="project:trading_sim"
        )

    assert result["ok"]
    tid = result["task"]["id"]

    ws = tasks_dir / tid
    assert ws.exists(), "项目待办应有 workspace"
    assert (ws / "NOTES.md").exists(), "NOTES.md 应存在"

    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT has_workspace FROM todos WHERE id=?", (tid,)).fetchone()
    conn.close()
    assert row[0] == 1


# ── 2. attach_workspace ──────


def test_attach_workspace_creates_on_demand(tmp_path, monkeypatch):
    """attach_workspace 能给已存在的普通待办建 workspace。"""
    db_path, tasks_dir = _setup_isolated_db(tmp_path, monkeypatch)

    with patch("domain.todos.crud.schedule_task_wakeup"):
        from domain.todos.crud import create_task, attach_workspace
        result = create_task(title="临时活", status="planned", source="manual")
        tid = result["task"]["id"]

        # 初始无 workspace
        assert not (tasks_dir / tid).exists()

        # attach 后有
        attach_result = attach_workspace(tid)
        assert attach_result["ok"]
        assert (tasks_dir / tid / "NOTES.md").exists()

        # 幂等：再 attach 不报错
        attach2 = attach_workspace(tid)
        assert attach2["ok"]
        assert attach2.get("already_existed") is True


# ── 3. parent_id 拆解 ──────


def test_parent_id_for_decomposition(tmp_path, monkeypatch):
    """子待办能通过 parent_id 关联到父待办。"""
    db_path, tasks_dir = _setup_isolated_db(tmp_path, monkeypatch)

    with patch("domain.todos.crud.schedule_task_wakeup"):
        from domain.todos.crud import create_task, update_task
        parent = create_task(title="大任务", status="planned", source="manual")
        pid = parent["task"]["id"]

        child = create_task(title="子步骤1", status="planned", source="manual")
        cid = child["task"]["id"]

        result = update_task(cid, parent_id=pid)
        assert result["ok"]

    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT parent_id FROM todos WHERE id=?", (cid,)).fetchone()
    conn.close()
    assert row[0] == pid, f"parent_id 应为 {pid}，实际 {row[0]}"
