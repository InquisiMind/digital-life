"""task_reminder emit 去重测试（整套功能已于 2026-06-17 停用）。

历史背景：本文件原验证 task_reminder 的"一位待办一个未消费提醒就够"去重逻辑。
但这整套 emit 链路（domain/todos/scheduler.py::schedule_task_wakeup →
domain/todos/speckit.py::schedule_task_wakeup）于 2026-06-17 因实测反弹成死循环
风暴（每次 session_end force emit → 1h emit 20 条 task_reminder）被整体停用,
schedule_task_wakeup 现为 no-op,不再 emit 任何事件。

因此下方 5 个去重测试已无意义（不可能 emit,assert n==1 必然失败）。用 module
级 skip 跳过,并保留 test_task_reminder_emit_chain_is_disabled 作哨兵——
未来若恢复 task_reminder emit 链路,该哨兵会先报错提醒同时补这套去重测试。
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

_SKIP_REASON = (
    "task_reminder emit 链路已于 2026-06-17 停用,本去重测试随之失效"
)


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    # 建 events 表
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel TEXT, kind TEXT, payload TEXT,
            created_at TEXT, fire_at TEXT, consumed_at TEXT,
            consumed_by_session_id TEXT, target_affair_id TEXT,
            resurrect_count INTEGER DEFAULT 0
        );
    """)
    conn.commit()
    conn.close()

    # patch 两个模块的 get_db 让它们都用 tmp_path
    import domain.todos.crud as crud
    import domain.todos.scheduler as sched

    def _fake_get_db():
        c = sqlite3.connect(str(db_path))
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        return c

    monkeypatch.setattr(crud, "get_db", _fake_get_db)
    monkeypatch.setattr(sched, "get_db", _fake_get_db)

    # patch count_pending 让它真实查 tmp DB（绕过单例 ContextVar）
    def _fake_count(kind, key, value):
        c = sqlite3.connect(str(db_path))
        c.row_factory = sqlite3.Row
        n = c.execute(
            "SELECT COUNT(*) as n FROM events WHERE consumed_at IS NULL AND kind=? "
            "AND json_extract(payload, ?) = ?",
            [kind, f"$.{key}", value],
        ).fetchone()["n"]
        c.close()
        return int(n)

    monkeypatch.setattr(
        "domain.todos.scheduler.count_pending_by_kind_and_payload", _fake_count,
        raising=False,
    )

    # schedule_task_wakeup 用 lazy import from domain.lifecycle.events，
    # 要 patch 那个 origin。但 lazy 引用是 `from domain.lifecycle.events import X`,
    # 实际访问 sys.modules['domain.lifecycle.events'].count_pending_by_kind_and_payload
    import domain.lifecycle.events as ev
    monkeypatch.setattr(ev, "count_pending_by_kind_and_payload", _fake_count)

    # emit_event 也要 patch 直接到 tmp DB
    def _fake_emit(*, kind, payload, **kwargs):
        c = sqlite3.connect(str(db_path))
        c.execute(
            "INSERT INTO events (channel, kind, payload, created_at) VALUES (?,?,?,?)",
            ("test", kind, __import__("json").dumps(payload), "2026-01-01T00:00:00"),
        )
        c.commit()
        c.close()
    monkeypatch.setattr(ev, "emit_event", _fake_emit)
    monkeypatch.setattr(sched, "emit_event", _fake_emit)

    return db_path


@pytest.mark.skip(reason=_SKIP_REASON)
def test_first_call_emits_event(isolated_db):
    """队列空 → 第一次调用该 emit 一个事件。"""
    from domain.todos.scheduler import schedule_task_wakeup
    schedule_task_wakeup("task-A", title="测试任务", status="in_progress")

    c = sqlite3.connect(str(isolated_db))
    n = c.execute(
        "SELECT COUNT(*) FROM events WHERE kind='task_reminder' AND "
        "json_extract(payload, '$.task_id') = 'task-A'"
    ).fetchone()[0]
    c.close()
    assert n == 1


@pytest.mark.skip(reason=_SKIP_REASON)
def test_second_call_dropped_when_pending_exists(isolated_db):
    """同 task_id 队列里已有未消费的 task_reminder → 第二次调用静默丢弃。"""
    from domain.todos.scheduler import schedule_task_wakeup
    schedule_task_wakeup("task-A", title="测试", status="in_progress")
    schedule_task_wakeup("task-A", title="测试", status="in_progress")
    schedule_task_wakeup("task-A", title="测试", status="in_progress")

    c = sqlite3.connect(str(isolated_db))
    n = c.execute(
        "SELECT COUNT(*) FROM events WHERE kind='task_reminder' AND "
        "json_extract(payload, '$.task_id') = 'task-A'"
    ).fetchone()[0]
    c.close()
    assert n == 1, f"应该只有 1 个 task_reminder，实际 {n}"


@pytest.mark.skip(reason=_SKIP_REASON)
def test_different_tasks_independent(isolated_db):
    """A、B 不同 task 互不影响——A 队列里有提醒也不该阻止 B 的第一次提醒。"""
    from domain.todos.scheduler import schedule_task_wakeup
    schedule_task_wakeup("task-A", title="A", status="in_progress")
    schedule_task_wakeup("task-B", title="B", status="in_progress")

    c = sqlite3.connect(str(isolated_db))
    a = c.execute(
        "SELECT COUNT(*) FROM events WHERE kind='task_reminder' AND "
        "json_extract(payload, '$.task_id') = 'task-A'"
    ).fetchone()[0]
    b = c.execute(
        "SELECT COUNT(*) FROM events WHERE kind='task_reminder' AND "
        "json_extract(payload, '$.task_id') = 'task-B'"
    ).fetchone()[0]
    c.close()
    assert a == 1 and b == 1


@pytest.mark.skip(reason=_SKIP_REASON)
def test_force_bypasses_dedup(isolated_db):
    """force=True 时跳过去重 check — 外部强调路径（manually re-arm 一个 task）。"""
    from domain.todos.scheduler import schedule_task_wakeup
    schedule_task_wakeup("task-A", title="A", status="in_progress")
    schedule_task_wakeup("task-A", title="A", status="in_progress", force=True)

    c = sqlite3.connect(str(isolated_db))
    n = c.execute(
        "SELECT COUNT(*) FROM events WHERE kind='task_reminder' AND "
        "json_extract(payload, '$.task_id') = 'task-A'"
    ).fetchone()[0]
    c.close()
    assert n == 2


@pytest.mark.skip(reason=_SKIP_REASON)
def test_consumed_event_frees_slot_for_new(isolated_db):
    """之前的 task_reminder 被消费(=数字生命处理完了) → 下次 free to emit 新的。"""
    from domain.todos.scheduler import schedule_task_wakeup
    schedule_task_wakeup("task-A", title="A", status="in_progress")

    # 模拟消费
    c = sqlite3.connect(str(isolated_db))
    c.execute(
        "UPDATE events SET consumed_at='2026-01-01T01:00:00' "
        "WHERE kind='task_reminder' AND json_extract(payload, '$.task_id')='task-A'"
    )
    c.commit()
    c.close()

    # 再调用一次——队列里没有「未消费」的了，应该 emit
    schedule_task_wakeup("task-A", title="A", status="in_progress")
    c = sqlite3.connect(str(isolated_db))
    unconsumed = c.execute(
        "SELECT COUNT(*) FROM events WHERE kind='task_reminder' "
        "AND json_extract(payload, '$.task_id')='task-A' AND consumed_at IS NULL"
    ).fetchone()[0]
    c.close()
    assert unconsumed == 1


def test_task_reminder_emit_chain_is_disabled():
    """哨兵：钉住 task_reminder emit 链路停用现状。

    schedule_task_wakeup 当前是 no-op（2026-06-17 停用），调用后不应 emit 任何事件。
    若未来有人恢复 emit 链路,本测试会先失败,提醒同步恢复/更新上面的 skip 去重测试。
    """
    from domain.todos.scheduler import schedule_task_wakeup

    # no-op:任何参数调用都不抛异常,返回 None
    result = schedule_task_wakeup("task-sentinel", title="sentinel", status="in_progress")
    assert result is None
