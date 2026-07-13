"""acceptance_criteria 字段端到端验证（设计文档 6.6/产品诉求 2026-06-14）。

每个 todo 都应能写明「什么样算 done」，避免：
  - todo 被反复拉起却没明确的关停条件
  - 做完后自评自夸就关闭（没有可验收依据）

验证三个层面：
  1. create_task 写入 acceptance_criteria，get_task / list_tasks 能读回
  2. 工具层（todo 工具 handler）能透传该参数
  3. update_task 能后补 acceptance_criteria（已有/todo 不需重建就能补）
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch


def _setup_isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "global_todos.db"
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
            linked_deliverable_id TEXT, type TEXT DEFAULT '',
            has_workspace INTEGER DEFAULT 0,
            source TEXT DEFAULT 'personal', origin_instance TEXT DEFAULT '',
            assignee_position TEXT DEFAULT '',
            detail TEXT DEFAULT '',
            created_at TEXT, updated_at TEXT
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
    monkeypatch.setattr(crud, "get_db", _fake_get_db)

    tasks_dir = tmp_path / "tasks_workspace"
    tasks_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(infra, "tasks_dir", lambda: tasks_dir)
    monkeypatch.setattr(crud, "tasks_dir", lambda: tasks_dir)

    return db_path, tasks_dir


def test_create_with_acceptance_criteria_persists_and_round_trips(tmp_path, monkeypatch):
    """create_task 写 acceptance_criteria → list / get 能读回该字段。"""
    _setup_isolated_db(tmp_path, monkeypatch)

    with patch("domain.todos.crud.schedule_task_wakeup"):
        from domain.todos.crud import create_task, list_tasks, get_task

        criteria = "输出 SPAC 可投清单 5 条；每条带 1-2 句 why；摘要无报错。"
        result = create_task(
            title="今天扫一遍 SPAC",
            description="最近一周有解禁 IPO，重新筛一遍可投。",
            acceptance_criteria=criteria,
            status="planned",
            source="personal",
        )

    assert result["ok"], f"create 失败: {result}"
    created = result["task"]
    assert created["acceptance_criteria"] == criteria, (
        f"create 返回的 task 应带上 acceptance_criteria，实际: {created.get('acceptance_criteria')!r}"
    )
    tid = created["id"]

    # list_tasks 读回
    rows = list_tasks()
    assert any(r["id"] == tid for r in rows)
    matched = next(r for r in rows if r["id"] == tid)
    assert matched["acceptance_criteria"] == criteria, (
        f"list_tasks 返回里该字段丢失，实际: {matched.get('acceptance_criteria')!r}"
    )

    # get_task 读回（详情视图里 task 嵌入）
    detail = get_task(tid)
    assert detail["task"]["acceptance_criteria"] == criteria


def test_create_without_acceptance_criteria_defaults_empty(tmp_path, monkeypatch):
    """不传 acceptance_criteria → 默认空字符串（不是 None，前端可直接 render）。"""
    _setup_isolated_db(tmp_path, monkeypatch)

    with patch("domain.todos.crud.schedule_task_wakeup"):
        from domain.todos.crud import create_task

        result = create_task(title="简单回个话", status="planned", source="manual")

    assert result["ok"]
    assert result["task"]["acceptance_criteria"] == ""


def test_update_can_backfill_acceptance_criteria(tmp_path, monkeypatch):
    """老 todo 没写完成标准 → update_task 能补上（不必重建 todo）。"""
    _setup_isolated_db(tmp_path, monkeypatch)

    with patch("domain.todos.crud.schedule_task_wakeup"):
        from domain.todos.crud import create_task, update_task, get_task

        created = create_task(title="补验收条件的活", status="planned", source="manual")
        tid = created["task"]["id"]
        assert created["task"]["acceptance_criteria"] == ""

        criteria = "脚本跑通且输出 JSON 有效；人工抽检 3 条无报错。"
        upd = update_task(tid, acceptance_criteria=criteria)
        assert upd["ok"], f"update 失败: {upd}"

        detail = get_task(tid)
        assert detail["task"]["acceptance_criteria"] == criteria


def test_todo_tool_handler_passes_acceptance_criteria(tmp_path, monkeypatch):
    """todo(action=create) 工具 handler 能透传 acceptance_criteria 到 crud。

    模型是通过这条工具创建 todo 的，schema 里暴露了字段 → handler 必须接住。
    """
    _setup_isolated_db(tmp_path, monkeypatch)

    captured = {}

    def _capture_consume(amount):
        import types
        captured["called"] = True
        return types.SimpleNamespace(energy=99.0)

    import domain.todos.tools as tools_mod

    # 备份原 registry 并用一个假 registry 接住 register 调用，拿 handler 出来
    class _FakeRegistry:
        def __init__(self):
            self.registered = {}

        def register(self, *, name, toolset=None, schema=None, handler=None,
                     check_fn=None, emoji=None, **kw):
            self.registered[name] = {
                "schema": schema, "handler": handler, "check_fn": check_fn,
            }

    fake = _FakeRegistry()
    tools_mod.register_task_tools(registry=fake, consume_energy=_capture_consume)

    handler = fake.registered["todo"]["handler"]

    with patch("domain.todos.crud.schedule_task_wakeup"):
        import json
        raw = handler({
            "action": "create",
            "title": "工具层建 todo",
            "description": "验证 handler 接住 acceptance_criteria",
            "acceptance_criteria": "工具能读到该参数且写进 DB",
        })
        payload = json.loads(raw)

    assert payload["ok"], f"handler 返回失败: {payload}"
    task = payload["task"]
    assert task["acceptance_criteria"] == "工具能读到该参数且写进 DB", (
        f"handler 透传的 acceptance_criteria 没落地，实际: {task.get('acceptance_criteria')!r}"
    )
