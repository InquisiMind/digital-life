"""Board rendering tests — 验证 render_my_board 输出结构（按项目分组 + 完整展开）。

设计原则（见 domain/todos/board.py）：
  - todos 实体是面板的唯一真相
  - 单一渲染入口
  - 完整展开每条 todo（描述/完成标准/笔记/步骤）

用例覆盖：
  - 多状态 + 多 project 分组渲染
  - acceptance_criteria 缺失 → ⚠️ 提示
  - 完成标准已写 → ✅ 显示
  - 笔记/步骤行渲染
  - done/cancelled 不上面板
  - 单条项目分组 + 多项目分组
  - 排序：进行中 → 过期 → ... 
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone, timedelta
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
    import domain.todos.board as board
    import domain.todos.crud as crud
    monkeypatch.setattr(infra, "get_db", _fake_get_db)
    monkeypatch.setattr(board, "get_db", _fake_get_db)
    monkeypatch.setattr(crud, "get_db", _fake_get_db)

    return db_path


_NOW = datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc)


def _insert_todo(db_path, id, title, *, status="planned", priority="medium",
                 description="", acceptance_criteria="", project_id="",
                 assignee_instance="alpha", deadline=None, has_workspace=0,
                 created_at="2026-06-10T00:00:00+00:00",
                 updated_at="2026-06-13T00:00:00+00:00"):
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO todos (id, title, description, acceptance_criteria, status, "
        "priority, deadline, project_id, assignee_instance, has_workspace, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (id, title, description, acceptance_criteria, status, priority,
         deadline, project_id, assignee_instance, has_workspace, created_at, updated_at),
    )
    conn.commit()
    conn.close()


def _insert_note(db_path, task_id, content, created_at):
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO todo_notes (task_id, content, created_at) VALUES (?, ?, ?)",
        (task_id, content, created_at),
    )
    conn.commit()
    conn.close()


def _insert_plan(db_path, task_id, content, status="pending", order_num=0):
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO todo_plans (task_id, content, status, order_num, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (task_id, content, status, order_num, "2026-06-10T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()


# ── 1. 基本结构 ──────


def test_empty_returns_empty(tmp_path, monkeypatch):
    """无任何活跃 todo → 空字符串（不要输出空标题）。"""
    _setup_isolated_db(tmp_path, monkeypatch)
    from domain.todos.board import render_my_board

    assert render_my_board("alpha", _NOW) == ""


def test_done_cancelled_not_shown(tmp_path, monkeypatch):
    """done / cancelled 状态的 todo 不应出现在面板上。"""
    db_path = _setup_isolated_db(tmp_path, monkeypatch)
    _insert_todo(db_path, "t-done", "已完成", status="done")
    _insert_todo(db_path, "t-cancel", "已取消", status="cancelled")
    _insert_todo(db_path, "t-active", "进行中任务", status="in_progress")

    from domain.todos.board import render_my_board
    text = render_my_board("alpha", _NOW)

    assert "t-active" in text
    assert "t-done" not in text
    assert "t-cancel" not in text


# ── 2. 完整展开字段 ──────


def test_full_todo_renders_all_fields(tmp_path, monkeypatch):
    """完整展开：标题/描述/完成标准/笔记/步骤。"""
    db_path = _setup_isolated_db(tmp_path, monkeypatch)
    _insert_todo(
        db_path, "t-full", "搭建量化系统",
        status="in_progress", priority="high",
        description="用 Python 搭一套能跑 SPAC + 趋势策略的回测框架。",
        acceptance_criteria="输出可复现回测的脚本，且夏普比 > 1.2。",
    )
    _insert_note(db_path, "t-full", "已搭出 event loop 骨架，下一步接数据源。",
                 "2026-06-13T10:00:00+00:00")
    _insert_plan(db_path, "t-full", "搭事件循环骨架", status="done", order_num=1)
    _insert_plan(db_path, "t-full", "接价格数据源", status="pending", order_num=2)
    _insert_plan(db_path, "t-full", "添加夏普统计", status="pending", order_num=3)

    from domain.todos.board import render_my_board
    text = render_my_board("alpha", _NOW)

    # 标题 + id
    assert "**搭建量化系统**" in text
    assert "id=t-full" in text
    # 描述摘要
    assert "Python 搭一套" in text
    # 完成标准
    assert "✅" in text
    assert "夏普比 > 1.2" in text
    # 最近笔记
    assert "💭" in text
    assert "event loop 骨架" in text
    # 待执行步骤计数 + 下一步
    assert "📋" in text
    assert "2/3" in text  # 2 pending of 3 total
    assert "接价格数据源" in text


def test_missing_acceptance_criteria_warns(tmp_path, monkeypatch):
    """没写 acceptance_criteria → ⚠️ 提示。"""
    db_path = _setup_isolated_db(tmp_path, monkeypatch)
    _insert_todo(db_path, "t-no-criteria", "随便记一笔", status="planned",
                 acceptance_criteria="")

    from domain.todos.board import render_my_board
    text = render_my_board("alpha", _NOW)

    assert "⚠️" in text
    assert "完成标准未写" in text
    # 应该带上 todo_id 提示模型怎么补
    assert "t-no-criteria" in text


# ── 3. 项目分组 ──────


def test_group_by_project(tmp_path, monkeypatch):
    """按 project_id 分组，个人项目在前。"""
    db_path = _setup_isolated_db(tmp_path, monkeypatch)
    _insert_todo(db_path, "t-proj", "项目活", status="planned",
                 project_id="trading_sim")
    _insert_todo(db_path, "t-pers", "个人活", status="planned", project_id="")

    from domain.todos.board import render_my_board
    text = render_my_board("alpha", _NOW)

    # 个人在前（"个人" 在 "项目:trading_sim" 之前）
    idx_personal = text.find("个人")
    idx_proj = text.find("trading_sim")
    assert idx_personal >= 0 and idx_proj >= 0
    assert idx_personal < idx_proj


def test_other_assignee_not_shown(tmp_path, monkeypatch):
    """assignee 不是我 → 不应该出现在面板上。"""
    db_path = _setup_isolated_db(tmp_path, monkeypatch)
    _insert_todo(db_path, "t-mine", "我的", status="planned",
                 assignee_instance="alpha")
    _insert_todo(db_path, "t-yours", "别人的", status="planned",
                 assignee_instance="beta")

    from domain.todos.board import render_my_board
    text = render_my_board("alpha", _NOW)

    assert "t-mine" in text
    assert "t-yours" not in text


# ── 4. 末尾统计 ──────


def test_stats_line(tmp_path, monkeypatch):
    """末尾应该有「面板状态」统计行，包含条数与缺失完成标准的数量。"""
    db_path = _setup_isolated_db(tmp_path, monkeypatch)
    _insert_todo(db_path, "t-a", "A", status="in_progress",
                 acceptance_criteria="A 标准")
    _insert_todo(db_path, "t-b", "B", status="planned", acceptance_criteria="")
    _insert_plan(db_path, "t-a", "步骤1", status="pending")

    from domain.todos.board import render_my_board
    text = render_my_board("alpha", _NOW)

    assert "面板状态" in text
    assert "共 2 条" in text
    assert "进行中 1" in text
    assert "1 条缺完成标准" in text
    assert "1 个待执行步骤" in text


# ── 5. 排序 ──────


def test_in_progress_sorts_first(tmp_path, monkeypatch):
    """in_progress 状态应该排到同项目内的最前面（updated_at DESC 之外）。"""
    db_path = _setup_isolated_db(tmp_path, monkeypatch)
    # planned 的 updated_at 比较新，但 in_progress 应当靠前
    _insert_todo(db_path, "t-planned", "Planned", status="planned",
                 updated_at="2026-06-14T00:00:00+00:00",
                 acceptance_criteria="x")
    _insert_todo(db_path, "t-inprog", "InProgress", status="in_progress",
                 updated_at="2026-06-12T00:00:00+00:00",
                 acceptance_criteria="y")

    from domain.todos.board import render_my_board
    text = render_my_board("alpha", _NOW)

    idx_planned = text.find("t-planned")
    idx_inprog = text.find("t-inprog")
    assert idx_inprog < idx_planned, "in_progress 应该排到 planned 前面"


# ── 6. 单一渲染入口：get_wake_context 透传 ──────


def test_get_wake_context_delegates_to_board(tmp_path, monkeypatch):
    """get_wake_context()（旧入口）应该等价于 render_my_board()。"""
    db_path = _setup_isolated_db(tmp_path, monkeypatch)
    _insert_todo(db_path, "x", "测试 todo", status="in_progress")

    # patch get_app_instance_id 让 wake_context 拿到 alpha
    from infrastructure.config import get_app_instance_id
    monkeypatch.setattr("infrastructure.config.get_app_instance_id", lambda: "alpha")
    # 注意 wake_context 是 import-time fetch，所以 patch 模块属性
    import domain.todos.wake_context as wc
    monkeypatch.setattr("infrastructure.config.get_app_instance_id", lambda: "alpha",
                        raising=True)

    # 也 patch clock.now_dt 让时间固定
    from domain.lifecycle import clock
    monkeypatch.setattr(clock, "now_dt", lambda: _NOW)

    result = wc.get_wake_context()
    assert "测试 todo" in result
    assert "我的待办" in result
