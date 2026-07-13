"""task_reminder / task_momentum 的停用状态回归保护。

历史背景（已废弃）：本文件原是 PR-1 的 task_reminder 死锁 + momentum 死条件修复的
验证测试。但那套修复在 2026-06-17 因实测反弹（每次 session_end force emit →
1h emit 20 条 task_reminder 的死循环风暴）被整体停用：

  - domain/todos/scheduler.py:81   schedule_task_wakeup     → no-op
  - domain/todos/scheduler.py:148  check_task_momentum      → 直接 return None
  - domain/todos/session_tracking.py:126  on_session_end     → 不再 re-arm

下面的三处测试改成反映"已停用"的现状（反向断言）——这样既不让废弃实现静默走掉,
又留下哨兵:未来若有人恢复这套功能而不更新测试,这些断言会立刻失败提醒。
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch
from datetime import datetime, timezone, timedelta


def _setup_task_db(tmp_path):
    """在 tmp_path 下创建隔离的 tasks.db，含测试任务。"""
    import domain.todos._infra as infra

    db_path = tmp_path / "tasks.db"
    # monkeypatch get_db 返回隔离 DB
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # 最小 schema（只建测试需要的表）
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS todos (
            id TEXT PRIMARY KEY, title TEXT, description TEXT,
            status TEXT DEFAULT 'idea', priority INTEGER DEFAULT 3,
            deadline TEXT, tags TEXT, source TEXT,
            linked_deliverable_id TEXT, type TEXT,
            created_at TEXT, updated_at TEXT,
            notes TEXT
        );
        CREATE TABLE IF NOT EXISTS todo_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
            content TEXT, deadline TEXT, status TEXT DEFAULT 'pending',
            order_num INTEGER DEFAULT 0, created_at TEXT
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
            content TEXT, trigger_type TEXT, trigger_condition TEXT,
            due_at TEXT, status TEXT DEFAULT 'pending',
            assignee TEXT, note TEXT,
            created_at TEXT, updated_at TEXT
        );
    """)
    conn.commit()

    # 插入测试数据：一个 stale 三周的 in_progress 任务
    now_iso = datetime.now(timezone.utc).isoformat()
    old_iso = (datetime.now(timezone.utc) - timedelta(days=21)).isoformat()
    conn.execute(
        "INSERT INTO todos (id, title, status, created_at, updated_at) "
        "VALUES ('test_stale', 'Stale Task', 'in_progress', ?, ?)",
        (old_iso, old_iso),
    )
    # 插入大量 session（模拟 921 个，超过 _max_total_sessions=8）
    for i in range(10):
        conn.execute(
            "INSERT INTO todo_sessions (task_id, session_id, digest, started_at, ended_at) "
            "VALUES ('test_stale', ?, '', ?, ?)",
            (f"tx_old_{i}", old_iso, old_iso),
        )
    conn.commit()
    conn.close()

    # patch _infra.get_db 返回这个隔离 DB
    def _fake_get_db():
        c = sqlite3.connect(str(db_path))
        c.row_factory = sqlite3.Row
        return c

    return db_path, _fake_get_db


# ── 1. session_end re-arm 已停用（2026-06-17）──────


def test_session_end_does_not_rearm_after_deprecation(tmp_path, monkeypatch):
    """session_end 后不再 re-arm task_reminder。

    历史逻辑：任何 in_progress 任务的 session 结束都 force schedule_task_wakeup →
    实测反弹成死循环风暴（6-14 实测 1h emit 20 条 task_reminder）。2026-06-17
    整体停用。本测试钉住"已停用"现状:若未来恢复 re-arm,这里会先报错提醒。
    """
    db_path, fake_get_db = _setup_task_db(tmp_path)

    import domain.todos._infra as infra
    import domain.todos.session_tracking as st

    monkeypatch.setattr(infra, "get_db", fake_get_db)
    monkeypatch.setattr(st, "get_db", fake_get_db)

    with patch("domain.todos.scheduler.schedule_task_wakeup") as mock_sched:
        mock_sched.return_value = None
        monkeypatch.setattr(st, "pop_due_events", lambda limit=20: [])
        monkeypatch.setattr(st, "get_task", lambda tid: {"id": tid, "title": "T"})
        monkeypatch.setattr(st, "read_notes", lambda tid, limit=5: ["note"])
        monkeypatch.setattr(st, "_session_has_successful_execution_tool", lambda sid: True)

        st.on_session_end("tx_initiative_abc12345", "digest")

        assert not mock_sched.called, (
            "schedule_task_wakeup 不应再被调用（re-arm 已于 2026-06-17 整体停用）"
        )


# ── 2. momentum 已停用（2026-06-17）──────


def test_check_task_momentum_returns_none_when_disabled():
    """check_task_momentum 已于 2026-06-17 停用,直接返回 None。

    历史逻辑：每 cron tick（60s）检 in_progress 任务停滞 → emit task_momentum,
    但反弹成每分钟一条重复催促。停用后返回 None。本测试钉住停用现状。
    """
    import domain.todos.scheduler as tsch

    tsch._momentum_last_fired.clear()
    assert tsch.check_task_momentum() is None, (
        "check_task_momentum 应返回 None（task_momentum 已于 2026-06-17 停用）"
    )


# ── 3. schedule_task_wakeup 已停用（no-op）──────


def test_schedule_task_wakeup_is_noop():
    """schedule_task_wakeup 已于 2026-06-17 停用,函数体 no-op。

    保留签名与调用方不动（避免大范围改动）,但不再安排任何唤醒。
    本测试钉住 no-op 现状。
    """
    import domain.todos.scheduler as tsch

    # 任何参数调用都不应抛异常、也不应返回非 None 的任何东西
    result = tsch.schedule_task_wakeup("any_task", title="T", status="in_progress")
    assert result is None


# ── 4. workspace 绝对路径注入 ──────


def test_workspace_intro_contains_absolute_path():
    """_render_workspace_intro 应该包含绝对 repo root，不只是相对路径。"""
    from domain.lifecycle.scheduler import _render_workspace_intro

    intro = _render_workspace_intro("test_instance")
    # 应该包含绝对路径标记和项目根
    assert "项目根目录" in intro or "绝对路径" in intro, "应注明绝对路径"
    # 应该包含 /Users/ 或类似绝对路径前缀
    assert "/" in intro and "apps/" in intro
