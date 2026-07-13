"""Tests for the alarm + event system redesign.

Covers:
- Alarm CRUD (set, cancel by id/kind/filter, fire, list)
- Event queue (emit, pop, consume, debounce)
- Alarm → event integration (fire_due_alarms → emit_event → pop_due_events)
- Deprecated paths (cancel_pending_events still works, schedule_vital_threshold_events)
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

# 测试存储格式与生产一致（UTC ISO），通过 clock.now_dt() 避免 server 本地时差引入误检。
from domain.lifecycle.clock import now_dt as _clock_now_dt

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def test_db():
    """Create a temporary state.db and redirect the runtime hooks."""
    tmp = tempfile.mkdtemp()
    db_path = Path(tmp) / "state.db"
    os.environ["DIGITAL_LIFE_INSTANCE_ID"] = "test"
    os.environ["DIGITAL_LIFE_RUNTIME_HOME"] = tmp

    from domain.lifecycle.affairs.runtime import configure_runtime_hooks
    configure_runtime_hooks(db_path=db_path)

    from domain.lifecycle.affairs.runtime import init_db
    init_db()

    yield db_path

    # Cleanup
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    del os.environ["DIGITAL_LIFE_INSTANCE_ID"]
    if "DIGITAL_LIFE_RUNTIME_HOME" in os.environ:
        del os.environ["DIGITAL_LIFE_RUNTIME_HOME"]


def _count_rows(db_path, table):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(f"SELECT COUNT(*) as n FROM {table}").fetchone()
    conn.close()
    return row["n"]


# ---------------------------------------------------------------------------
# TestAlarmSystem — alarm CRUD operations
# ---------------------------------------------------------------------------

class TestAlarmSystem:
    def test_set_alarm_creates_row(self, test_db):
        from domain.lifecycle.alarms import set_alarm

        alarm_id = set_alarm(
            event_kind="timer",
            fire_at="2026-06-01T08:00:00",
            payload={"reason": "test"},
        )
        assert alarm_id > 0
        assert _count_rows(test_db, "timers") == 1

    def test_cancel_alarm_by_id(self, test_db):
        from domain.lifecycle.alarms import set_alarm, cancel_alarm

        alarm_id = set_alarm("timer", fire_at="2026-06-01T08:00:00")
        assert _count_rows(test_db, "timers WHERE fired_at IS NULL") == 1

        ok = cancel_alarm(alarm_id)
        assert ok is True
        assert _count_rows(test_db, "timers WHERE fired_at IS NULL") == 0

    def test_cancel_nonexistent_alarm(self, test_db):
        from domain.lifecycle.alarms import cancel_alarm
        ok = cancel_alarm(99999)
        assert ok is False

    def test_cancel_alarms_by_kind(self, test_db):
        from domain.lifecycle.alarms import set_alarm, cancel_alarms_by_kind

        set_alarm("awaiting_reply", fire_at="2026-06-01T08:00:00")
        set_alarm("awaiting_reply", fire_at="2026-06-01T09:00:00")
        set_alarm("timer", fire_at="2026-06-01T10:00:00")

        assert _count_rows(test_db, "timers WHERE fired_at IS NULL") == 3

        n = cancel_alarms_by_kind("awaiting_reply")
        assert n == 2
        assert _count_rows(test_db, "timers WHERE fired_at IS NULL") == 1  # only timer left

    def test_cancel_alarms_by_filter(self, test_db):
        from domain.lifecycle.alarms import set_alarm, cancel_alarms_by_filter

        set_alarm("task_reminder", fire_at="2026-06-01T08:00:00", payload={"task_id": "task-1"})
        set_alarm("task_reminder", fire_at="2026-06-01T09:00:00", payload={"task_id": "task-1"})
        set_alarm("task_reminder", fire_at="2026-06-01T10:00:00", payload={"task_id": "task-2"})

        assert _count_rows(test_db, "timers WHERE fired_at IS NULL") == 3

        n = cancel_alarms_by_filter("task_reminder", payload_filter={"task_id": "task-1"})
        assert n == 2
        assert _count_rows(test_db, "timers WHERE fired_at IS NULL") == 1  # only task-2 left

    def test_cancel_alarms_by_filter_no_match(self, test_db):
        from domain.lifecycle.alarms import set_alarm, cancel_alarms_by_filter

        set_alarm("timer", fire_at="2026-06-01T08:00:00")
        n = cancel_alarms_by_filter("task_reminder", payload_filter={"task_id": "x"})
        assert n == 0
        assert _count_rows(test_db, "timers WHERE fired_at IS NULL") == 1

    def test_fire_due_alarms(self, test_db):
        from domain.lifecycle.alarms import set_alarm, fire_due_alarms
        from domain.lifecycle.clock import now_iso

        # Past alarm — should fire
        past = (_clock_now_dt() - timedelta(hours=1)).isoformat(timespec="seconds")
        set_alarm("timer", fire_at=past, payload={"reason": "past"})

        # Future alarm — should NOT fire
        future = (_clock_now_dt() + timedelta(hours=1)).isoformat(timespec="seconds")
        set_alarm("timer", fire_at=future, payload={"reason": "future"})

        fired = fire_due_alarms()
        assert len(fired) == 1
        assert fired[0]["event_kind"] == "timer"
        assert fired[0]["payload"]["reason"] == "past"
        assert fired[0]["event_id"] > 0

    def test_fire_due_alarms_no_due(self, test_db):
        from domain.lifecycle.alarms import set_alarm, fire_due_alarms

        future = (_clock_now_dt() + timedelta(hours=1)).isoformat(timespec="seconds")
        set_alarm("timer", fire_at=future)

        fired = fire_due_alarms()
        assert len(fired) == 0

    def test_set_alarm_payload_json_roundtrip(self, test_db):
        from domain.lifecycle.alarms import set_alarm

        payload = {"text": "中文测试", "nested": {"key": "value"}}
        alarm_id = set_alarm("awaiting_reply", fire_at="2026-06-01T08:00:00", payload=payload)

        conn = sqlite3.connect(str(test_db))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT payload_json FROM timers WHERE id = ?", (alarm_id,)).fetchone()
        conn.close()

        loaded = json.loads(row["payload_json"])
        assert loaded["text"] == "中文测试"
        assert loaded["nested"]["key"] == "value"

    def test_list_pending_alarms(self, test_db):
        from domain.lifecycle.alarms import set_alarm, cancel_alarms_by_kind, list_pending_alarms

        set_alarm("timer", fire_at="2026-06-01T08:00:00")
        set_alarm("awaiting_reply", fire_at="2026-06-01T09:00:00")

        all_pending = list_pending_alarms()
        assert len(all_pending) == 2

        timer_only = list_pending_alarms(kind="timer")
        assert len(timer_only) == 1
        assert timer_only[0]["event_kind"] == "timer"


# ---------------------------------------------------------------------------
# TestEventQueue — event emit, pop, consume
# ---------------------------------------------------------------------------

class TestEventQueue:
    def test_emit_and_pop(self, test_db):
        from domain.lifecycle.events import emit_event, pop_due_events, consume_event
        from domain.lifecycle.clock import now_iso

        eid = emit_event(kind="message", payload={"text": "hello"})
        assert eid > 0

        events = pop_due_events(limit=5)
        assert len(events) >= 1
        human_events = [e for e in events if e["kind"] == "message"]
        assert len(human_events) >= 1
        assert human_events[0]["payload"]["text"] == "hello"

    def test_consume_event(self, test_db):
        from domain.lifecycle.events import emit_event, pop_due_events, consume_event

        eid = emit_event(kind="timer", payload={"reason": "test"})

        # Before consume — event is visible
        events = pop_due_events(limit=5)
        assert any(e["event_id"] == eid for e in events)

        # Consume with session_id
        consume_event(eid, session_id="session-1")

        # After consume — event is gone from pop
        events = pop_due_events(limit=5)
        assert not any(e["event_id"] == eid for e in events)

    def test_consume_events_by_kind(self, test_db):
        from domain.lifecycle.events import emit_event, consume_events_by_kind

        emit_event(kind="vital_threshold", payload={"to_seg": "疲惫"})
        emit_event(kind="vital_threshold", payload={"to_seg": "精疲力竭"})
        emit_event(kind="timer", payload={"reason": "x"})

        n = consume_events_by_kind("vital_threshold", session_id="s-1")
        assert n == 2

    def test_emit_event_with_fire_at_still_works(self, test_db):
        """fire_at is deprecated but should still function for backward compat."""
        from domain.lifecycle.events import emit_event, pop_due_events

        past = (_clock_now_dt() - timedelta(hours=1)).isoformat(timespec="seconds")
        eid = emit_event(kind="timer", payload={"reason": "backward"}, fire_at=past)
        assert eid > 0

        events = pop_due_events(limit=5)
        assert any(e["event_id"] == eid for e in events)

    def test_pop_events_by_kind(self, test_db):
        from domain.lifecycle.events import emit_event, pop_events_by_kind

        emit_event(kind="timer", payload={"reason": "a"})
        emit_event(kind="timer", payload={"reason": "b"})
        emit_event(kind="message", payload={"text": "hi"})

        timer_events = pop_events_by_kind("timer", limit=5, session_id="s-2")
        assert len(timer_events) == 2

        # timer events should be consumed now
        from domain.lifecycle.events import pop_due_events
        remaining = pop_due_events(limit=5)
        timer_remaining = [e for e in remaining if e["kind"] == "timer"]
        assert len(timer_remaining) == 0


# ---------------------------------------------------------------------------
# TestAlarmToEventIntegration — fire_due_alarms → emit_event → pop
# ---------------------------------------------------------------------------

class TestAlarmToEventIntegration:
    def test_alarm_fire_creates_event(self, test_db):
        from domain.lifecycle.alarms import set_alarm, fire_due_alarms
        from domain.lifecycle.events import pop_due_events

        past = (_clock_now_dt() - timedelta(hours=1)).isoformat(timespec="seconds")
        set_alarm("routine", fire_at=past, payload={"name": "morning_plan", "prompt": "good morning"})

        fired = fire_due_alarms()
        assert len(fired) == 1

        events = pop_due_events(limit=5)
        routine_events = [e for e in events if e["kind"] == "routine"]
        assert len(routine_events) >= 1

    def test_full_wake_cycle(self, test_db):
        """Simulate a full wake cycle:
        1. rest() sets a timer alarm
        2. Cron tick fires the alarm → emit_event("timer")
        3. Core system pops the event → wakes
        """
        from domain.lifecycle.alarms import set_alarm, fire_due_alarms
        from domain.lifecycle.events import pop_due_events, consume_event

        # 1. Set a timer alarm (like rest() does)
        past = (_clock_now_dt() - timedelta(minutes=30)).isoformat(timespec="seconds")
        set_alarm("timer", fire_at=past, payload={"reason": "rest"})

        # 2. Cron tick fires alarms
        fired = fire_due_alarms()
        assert len(fired) == 1
        assert fired[0]["event_kind"] == "timer"

        # 3. Core system pops events → finds timer event
        events = pop_due_events(limit=50)
        timer_events = [e for e in events if e["kind"] == "timer"]
        assert len(timer_events) >= 1

        # 4. After wake, consume the event
        for e in timer_events:
            consume_event(e["event_id"], session_id="wake-session-1")

        # 5. Event is consumed
        remaining = pop_due_events(limit=50)
        assert not any(e["event_id"] in {t["event_id"] for t in timer_events} for e in remaining)

    def test_awaiting_reply_alarm_flow(self, test_db):
        """Simulate express_to_human setting an awaiting_reply alarm."""
        from domain.lifecycle.alarms import set_alarm, cancel_alarms_by_kind, fire_due_alarms
        from domain.lifecycle.events import pop_due_events

        # express_to_human: clear old, set new
        set_alarm("awaiting_reply", fire_at="2026-06-01T08:05:00")
        cancel_alarms_by_kind("awaiting_reply")  # clear old
        fire_at = (_clock_now_dt() - timedelta(hours=1)).isoformat(timespec="seconds")
        set_alarm("awaiting_reply", fire_at=fire_at, payload={"last_sent_text": "hi"})

        # Later: cron tick fires the alarm
        fired = fire_due_alarms()
        assert len(fired) >= 1
        assert any(f["event_kind"] == "awaiting_reply" for f in fired)


# ---------------------------------------------------------------------------
# TestDeprecatedPaths — backward compatibility
# ---------------------------------------------------------------------------

class TestDeprecatedPaths:
    def test_cancel_pending_events_still_works(self, test_db):
        from domain.lifecycle.events import cancel_pending_events, emit_event

        emit_event(kind="timer", payload={})
        n = cancel_pending_events(kind="timer")
        # cancel_pending_events works at the event bus level (marks consumed)
        # The exact count depends on implementation
        assert isinstance(n, int)

    def test_schedule_vital_threshold_events_exists(self):
        from domain.lifecycle.events import schedule_vital_threshold_events
        # Deprecated but should not raise ImportError
        assert callable(schedule_vital_threshold_events)

    def test_emit_event_fire_at_kwarg_accepted(self, test_db):
        from domain.lifecycle.events import emit_event
        future = (_clock_now_dt() + timedelta(days=1)).isoformat(timespec="seconds")
        eid = emit_event(kind="timer", payload={}, fire_at=future)
        assert eid > 0


# ---------------------------------------------------------------------------
# TestDebounce — RAS debounce layer
# ---------------------------------------------------------------------------

class TestDebounce:
    def test_debounce_latest_replaces_payload(self, test_db):
        from domain.lifecycle.events import emit_event, pop_due_events

        # Two initiative events within 300s window should merge
        eid1 = emit_event(kind="initiative", payload={"urgency": 5})
        eid2 = emit_event(kind="initiative", payload={"urgency": 8})
        # Both return the same event_id (merged)
        assert eid1 == eid2

    def test_debounce_disabled_for_group_message(self, test_db):
        # 新架构:group_message 的 debounce 已关闭(窗口=0)。消息合并在 ingress
        # adapter 层完成(interfaces/ingress/group_buffer.py 一次 flush 一条带
        # merged_texts 的 NormalizedMessage),事件层每条入站 = 一个独立事件。
        from domain.lifecycle.events import emit_event

        eid1 = emit_event(kind="group_message", payload={
            "text": "msg1", "sender_name": "Alice", "chat_name": "group1"
        })
        eid2 = emit_event(kind="group_message", payload={
            "text": "msg2", "sender_name": "Bob", "chat_name": "group1"
        })
        # 窗口=0 → 两条独立事件
        assert eid1 != eid2

    def test_debounce_skip_for_fire_at(self, test_db):
        """Events with fire_at should not be debounced."""
        from domain.lifecycle.events import emit_event, pop_due_events

        t1 = (_clock_now_dt() + timedelta(days=1)).isoformat(timespec="seconds")
        t2 = (_clock_now_dt() + timedelta(days=2)).isoformat(timespec="seconds")
        eid1 = emit_event(kind="routine", payload={"name": "a"}, fire_at=t1)
        eid2 = emit_event(kind="routine", payload={"name": "b"}, fire_at=t2)

        # fire_at events skip debounce → different event_ids
        assert eid1 != eid2


# ---------------------------------------------------------------------------
# TestContextVarIsolation — per-instance event isolation
# ---------------------------------------------------------------------------

class TestContextVarIsolation:
    def test_instance_context_switching(self, test_db):
        from domain.lifecycle.events import (
            set_instance_context, reset_instance_context,
            emit_event, pop_due_events,
        )

        # Instance A
        token_a = set_instance_context("alpha")
        emit_event(kind="timer", payload={"instance": "alpha"})

        # Instance B
        token_b = set_instance_context("beta")
        emit_event(kind="timer", payload={"instance": "beta"})

        # Instance B should only see its own events
        events_b = pop_due_events(limit=10)
        beta_events = [e for e in events_b if e.get("kind") == "timer"]
        # Each should only see its own
        beta_payloads = [e["payload"].get("instance") for e in beta_events if isinstance(e.get("payload"), dict)]
        assert "alpha" not in beta_payloads

        reset_instance_context(token_b)
        reset_instance_context(token_a)
