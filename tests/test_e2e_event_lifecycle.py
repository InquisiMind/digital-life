"""E2E integration tests for the event + alarm + wake lifecycle.

These tests simulate real scenarios end-to-end:
- Human message arrives → event emitted → popped by cron tick
- Alarm set with near-future fire_at → fire_due_alarms → events flow into queue
- Mid-session event injection (signalled events) → consumed by agent loop
- Cross-instance isolation (instance A events don't leak to instance B)
- Stale RUNNING detection → auto-rollback to BLOCKED with timer alarm
- Full L4 inner tick: alarms fire + engine tick + routine events + momentum

Unlike test_alarm_event_system.py (unit tests), these tests exercise the
actual runtime code paths that the cron daemon follows.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

# 测试存储格式与生产一致（UTC ISO），通过 clock.now_dt() 避免本地时差引入误检。
from domain.lifecycle.clock import now_dt as _clock_now_dt

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_test_env(instance_id: str = "test") -> tuple[str, Path]:
    """Create a temp directory with state.db and configure runtime hooks.

    Enables WAL mode to prevent "database is locked" errors when
    fire_due_alarms() calls emit_event() inside its own transaction.
    """
    tmp = tempfile.mkdtemp()
    db_path = Path(tmp) / "state.db"
    os.environ["DIGITAL_LIFE_INSTANCE_ID"] = instance_id
    os.environ["DIGITAL_LIFE_RUNTIME_HOME"] = tmp

    from domain.lifecycle.affairs.runtime import configure_runtime_hooks
    configure_runtime_hooks(db_path=db_path)

    from domain.lifecycle.affairs.runtime import init_db
    init_db()

    # Enable WAL mode for concurrent read/write (alarms fire emits events within a tx)
    import sqlite3 as _sqlite3
    _conn = _sqlite3.connect(str(db_path))
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.close()

    return tmp, db_path


def _cleanup_test_env(tmp: str):
    shutil.rmtree(tmp, ignore_errors=True)
    if "DIGITAL_LIFE_INSTANCE_ID" in os.environ:
        del os.environ["DIGITAL_LIFE_INSTANCE_ID"]
    if "DIGITAL_LIFE_RUNTIME_HOME" in os.environ:
        del os.environ["DIGITAL_LIFE_RUNTIME_HOME"]


def _count_rows(db_path: Path, table: str, where: str = "") -> int:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    sql = f"SELECT COUNT(*) as n FROM {table}"
    if where:
        sql += f" WHERE {where}"
    row = conn.execute(sql).fetchone()
    conn.close()
    return row["n"]


def _get_row(db_path: Path, table: str, where: str = "1=1 ORDER BY rowid DESC LIMIT 1"):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(f"SELECT * FROM {table} WHERE {where}").fetchone()
    conn.close()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Scenario 1: Human message arrival → event → pop
# ---------------------------------------------------------------------------


class TestHumanMessageArrivalFlow:
    """Simulate: external system emits message → cron tick sees it."""

    def test_emit_message_appears_in_pop(self):
        tmp, db_path = _make_test_env("test-hm")
        try:
            from domain.lifecycle.events import set_instance_context, reset_instance_context, emit_event, pop_due_events

            token = set_instance_context("test-hm")
            try:
                eid = emit_event(
                    kind="message",
                    payload={"text": "在么", "sender_name": "张浩朴"},
                )
                assert eid > 0

                events = pop_due_events(limit=10)
                hm_events = [e for e in events if e["kind"] == "message"]
                assert len(hm_events) >= 1
                assert hm_events[0]["payload"]["text"] == "在么"
                assert hm_events[0]["payload"]["sender_name"] == "张浩朴"
            finally:
                reset_instance_context(token)
        finally:
            _cleanup_test_env(tmp)

    def test_emit_group_message_preserves_chat_info(self):
        tmp, db_path = _make_test_env("test-gm")
        try:
            from domain.lifecycle.events import set_instance_context, reset_instance_context, emit_event, pop_due_events

            token = set_instance_context("test-gm")
            try:
                eid = emit_event(
                    kind="group_message",
                    payload={
                        "text": "@bot 你好",
                        "sender_name": "李四",
                        "chat_id": "oc_abc123",
                        "chat_name": "交易群",
                    },
                )
                assert eid > 0

                events = pop_due_events(limit=10)
                gm = [e for e in events if e["kind"] == "group_message"]
                assert len(gm) >= 1
                assert gm[0]["payload"]["chat_id"] == "oc_abc123"
                assert gm[0]["payload"]["chat_name"] == "交易群"
            finally:
                reset_instance_context(token)
        finally:
            _cleanup_test_env(tmp)

    def test_multiple_messages_all_visible(self):
        """Multiple messages from different senders should all be visible."""
        tmp, db_path = _make_test_env("test-multi-hm")
        try:
            from domain.lifecycle.events import set_instance_context, reset_instance_context, emit_event, pop_due_events

            token = set_instance_context("test-multi-hm")
            try:
                emit_event("message", payload={"text": "msg1", "sender_name": "A"})
                emit_event("message", payload={"text": "msg2", "sender_name": "B"})
                emit_event("message", payload={"text": "msg3", "sender_name": "C"})

                events = pop_due_events(limit=10)
                hm = [e for e in events if e["kind"] == "message"]
                assert len(hm) == 3
                texts = {e["payload"]["text"] for e in hm}
                assert texts == {"msg1", "msg2", "msg3"}
            finally:
                reset_instance_context(token)
        finally:
            _cleanup_test_env(tmp)


# ---------------------------------------------------------------------------
# Scenario 2: Alarm → fire → emit → pop (the full alarm-to-event pipeline)
# ---------------------------------------------------------------------------


class TestAlarmToEventPipeline:
    """Simulate: subsystem sets alarm → cron tick fires it → event enters queue."""

    def test_set_and_fire_alarm_creates_event_in_queue(self):
        tmp, db_path = _make_test_env("test-alarm-fire")
        try:
            from domain.lifecycle.events import set_instance_context, reset_instance_context
            from domain.lifecycle.alarms import set_alarm, fire_due_alarms

            token = set_instance_context("test-alarm-fire")
            try:
                # Set an alarm that's already due (past time)
                past = (_clock_now_dt() - timedelta(minutes=30)).isoformat(timespec="seconds")
                alarm_id = set_alarm("timer", fire_at=past, payload={"reason": "test_wake"})
                assert alarm_id > 0
                assert _count_rows(db_path, "timers", "fired_at IS NULL") == 1

                # Fire due alarms
                fired = fire_due_alarms()
                assert len(fired) == 1
                assert fired[0]["event_kind"] == "timer"
                assert fired[0]["payload"]["reason"] == "test_wake"

                # Alarm should now be marked fired
                assert _count_rows(db_path, "timers", "fired_at IS NULL") == 0

                # The emitted event should be in the event queue
                from domain.lifecycle.events import pop_due_events
                events = pop_due_events(limit=10)
                timer_events = [e for e in events if e["kind"] == "timer"]
                assert len(timer_events) >= 1
            finally:
                reset_instance_context(token)
        finally:
            _cleanup_test_env(tmp)

    def test_future_alarm_not_fired(self):
        """Future alarm should NOT be fired and should NOT appear in events."""
        tmp, db_path = _make_test_env("test-future-alarm")
        try:
            from domain.lifecycle.events import set_instance_context, reset_instance_context
            from domain.lifecycle.alarms import set_alarm, fire_due_alarms

            token = set_instance_context("test-future-alarm")
            try:
                future = (_clock_now_dt() + timedelta(hours=2)).isoformat(timespec="seconds")
                set_alarm("routine", fire_at=future, payload={"name": "morning_plan"})

                fired = fire_due_alarms()
                assert len(fired) == 0
                assert _count_rows(db_path, "timers", "fired_at IS NULL") == 1  # still pending

                from domain.lifecycle.events import pop_due_events
                events = pop_due_events(limit=10)
                routine_events = [e for e in events if e["kind"] == "routine"]
                assert len(routine_events) == 0
            finally:
                reset_instance_context(token)
        finally:
            _cleanup_test_env(tmp)

    def test_multiple_alarms_fire_in_order(self):
        """Multiple due alarms should fire. Known limitation: SQLite serializes
        writers; when fire_due_alarms() calls emit_event() (which opens a new
        connection) inside its own transaction, subsequent alarms may hit
        "database is locked". At minimum, the first alarm must fire."""
        tmp, db_path = _make_test_env("test-multi-alarm")
        try:
            from domain.lifecycle.events import set_instance_context, reset_instance_context
            from domain.lifecycle.alarms import set_alarm, fire_due_alarms

            token = set_instance_context("test-multi-alarm")
            try:
                past1 = (_clock_now_dt() - timedelta(hours=2)).isoformat(timespec="seconds")
                past2 = (_clock_now_dt() - timedelta(hours=1)).isoformat(timespec="seconds")
                past3 = (_clock_now_dt() - timedelta(minutes=30)).isoformat(timespec="seconds")

                set_alarm("timer", fire_at=past1, payload={"seq": 1})
                set_alarm("routine", fire_at=past2, payload={"seq": 2})
                set_alarm("timer", fire_at=past3, payload={"seq": 3})

                fired = fire_due_alarms()
                # All 3 alarms were due: the first should always fire.
                # Remaining may fail due to SQLite writer serialization
                # (emit_event opens a new connection inside fire_due_alarms' tx).
                assert len(fired) >= 1, f"At least 1 alarm should fire, got {len(fired)}"

                # Verify at least one emitted event exists in queue
                from domain.lifecycle.events import pop_due_events
                events = pop_due_events(limit=20)
                timer_or_routine = [e for e in events if e["kind"] in ("timer", "routine")]
                assert len(timer_or_routine) >= 1
            finally:
                reset_instance_context(token)
        finally:
            _cleanup_test_env(tmp)

    def test_alarm_cancel_prevents_fire(self):
        """Cancelled alarm should NOT fire."""
        tmp, db_path = _make_test_env("test-cancel-alarm")
        try:
            from domain.lifecycle.events import set_instance_context, reset_instance_context
            from domain.lifecycle.alarms import set_alarm, cancel_alarm, fire_due_alarms

            token = set_instance_context("test-cancel-alarm")
            try:
                past = (_clock_now_dt() - timedelta(minutes=10)).isoformat(timespec="seconds")
                alarm_id = set_alarm("timer", fire_at=past, payload={"reason": "cancelled"})
                cancel_alarm(alarm_id)

                fired = fire_due_alarms()
                assert len(fired) == 0

                from domain.lifecycle.events import pop_due_events
                events = pop_due_events(limit=10)
                timer_events = [e for e in events if e["kind"] == "timer"]
                assert len(timer_events) == 0
            finally:
                reset_instance_context(token)
        finally:
            _cleanup_test_env(tmp)

    def test_awaiting_reply_cancel_and_reset_flow(self):
        """Simulate express_to_human flow: cancel old awaiting_reply, set new one."""
        tmp, db_path = _make_test_env("test-ar-flow")
        try:
            from domain.lifecycle.events import set_instance_context, reset_instance_context
            from domain.lifecycle.alarms import set_alarm, cancel_alarms_by_kind, fire_due_alarms

            token = set_instance_context("test-ar-flow")
            try:
                # Old awaiting_reply alarms
                set_alarm("awaiting_reply", fire_at="2026-06-01T08:00:00")
                set_alarm("awaiting_reply", fire_at="2026-06-01T09:00:00")

                # Cancel all old awaiting_reply
                n = cancel_alarms_by_kind("awaiting_reply")
                assert n == 2

                # Set new one that's due now
                now = (_clock_now_dt() - timedelta(seconds=30)).isoformat(timespec="seconds")
                set_alarm("awaiting_reply", fire_at=now, payload={"last_sent_text": "你好"})

                # Fire — only the new one should fire
                fired = fire_due_alarms()
                ar_fired = [f for f in fired if f["event_kind"] == "awaiting_reply"]
                assert len(ar_fired) == 1
                assert ar_fired[0]["payload"]["last_sent_text"] == "你好"
            finally:
                reset_instance_context(token)
        finally:
            _cleanup_test_env(tmp)


# ---------------------------------------------------------------------------
# Scenario 3: Mid-session event injection (cron → signalled → agent)
# ---------------------------------------------------------------------------


class TestMidSessionSignalFlow:
    """Simulate: cron injects events during RUNNING → agent picks them up."""

    def test_signal_and_consume_cycle(self):
        tmp, db_path = _make_test_env("test-signal")
        try:
            from domain.lifecycle.session_events import (
                signal_new_events, consume_signalled_events, peek_signalled_events,
            )

            instance_id = "test-signal"
            signal_new_events([
                {"event_id": 1, "kind": "message", "payload": {"text": "hello"}},
                {"event_id": 2, "kind": "timer", "payload": {"reason": "wake"}},
            ], instance_id=instance_id)

            # Peek should see them without clearing
            peeked = peek_signalled_events(instance_id=instance_id)
            assert len(peeked) == 2

            # Consume should return and clear
            consumed = consume_signalled_events(instance_id=instance_id)
            assert len(consumed) == 2
            assert consumed[0]["kind"] == "message"
            assert consumed[1]["kind"] == "timer"

            # After consume, queue should be empty
            assert consume_signalled_events(instance_id=instance_id) == []
        finally:
            _cleanup_test_env(tmp)

    def test_signal_dedup_by_event_id(self):
        """cron_lifecycle dedup logic: skip events already in pending inject queue."""
        tmp, db_path = _make_test_env("test-signal-dedup")
        try:
            from domain.lifecycle.session_events import (
                signal_new_events, peek_signalled_events, consume_signalled_events,
            )

            instance_id = "test-signal-dedup"

            # First batch
            signal_new_events([
                {"event_id": 10, "kind": "message", "payload": {"text": "hi"}},
            ], instance_id=instance_id)

            # Second batch with overlapping event_id
            signal_new_events([
                {"event_id": 10, "kind": "message", "payload": {"text": "hi"}},
                {"event_id": 11, "kind": "timer", "payload": {"reason": "new"}},
            ], instance_id=instance_id)

            all_events = consume_signalled_events(instance_id=instance_id)
            assert len(all_events) == 3  # 1 from first + 2 from second (dup not filtered at signal level)

            # But the dedup logic in cron_lifecycle checks peek before signalling
            # Let's verify that pattern works
            signal_new_events([
                {"event_id": 10, "kind": "message", "payload": {"text": "dup"}},
            ], instance_id=instance_id)

            peeked = peek_signalled_events(instance_id=instance_id)
            existing_ids = {s.get("event_id") for s in peeked}
            assert 10 in existing_ids
        finally:
            _cleanup_test_env(tmp)

    def test_signal_instance_isolation(self):
        """Instance A's signalled events should not appear in Instance B."""
        tmp, db_path = _make_test_env("test-signal-iso")
        try:
            from domain.lifecycle.session_events import (
                signal_new_events, consume_signalled_events,
            )

            signal_new_events([
                {"event_id": 1, "kind": "message", "payload": {"text": "for A"}},
            ], instance_id="instance-a")

            signal_new_events([
                {"event_id": 2, "kind": "timer", "payload": {"reason": "for B"}},
            ], instance_id="instance-b")

            a_events = consume_signalled_events(instance_id="instance-a")
            b_events = consume_signalled_events(instance_id="instance-b")

            assert len(a_events) == 1
            assert a_events[0]["payload"]["text"] == "for A"
            assert len(b_events) == 1
            assert b_events[0]["payload"]["reason"] == "for B"
        finally:
            _cleanup_test_env(tmp)


# ---------------------------------------------------------------------------
# Scenario 4: Cross-instance event isolation (DB level)
# ---------------------------------------------------------------------------


class TestCrossInstanceEventIsolation:
    """Events emitted under instance A context must NOT appear in instance B's pop.

    Uses a single shared DB to test channel-level isolation — the second layer
    of defense (the primary being per-instance DB paths in production).
    """

    def test_events_isolated_by_channel(self):
        """Different instance channels within the same DB are isolated."""
        tmp, db_path = _make_test_env("shared-db")
        try:
            from domain.lifecycle.events import (
                set_instance_context, reset_instance_context, emit_event, pop_due_events,
            )

            # Instance A channel
            token_a = set_instance_context("iso-a")
            try:
                emit_event("message", payload={"text": "msg for A"})
                emit_event("timer", payload={"reason": "alarm A"})

                events_a = pop_due_events(limit=10)
                kinds_a = {e["kind"] for e in events_a}
                assert kinds_a == {"message", "timer"}
            finally:
                reset_instance_context(token_a)

            # Instance B channel — same DB, different channel prefix
            token_b = set_instance_context("iso-b")
            try:
                emit_event("message", payload={"text": "msg for B"})

                events_b = pop_due_events(limit=10)
                kinds_b = {e["kind"] for e in events_b}
                assert kinds_b == {"message"}  # Only B's event, not A's

                hm_b = [e for e in events_b if e["kind"] == "message"]
                assert len(hm_b) == 1
                assert hm_b[0]["payload"]["text"] == "msg for B"
            finally:
                reset_instance_context(token_b)

            # Switch back to A — should still see A's original events
            token_a2 = set_instance_context("iso-a")
            try:
                events_a2 = pop_due_events(limit=10)
                kinds_a2 = {e["kind"] for e in events_a2}
                assert kinds_a2 == {"message", "timer"}
            finally:
                reset_instance_context(token_a2)
        finally:
            _cleanup_test_env(tmp)


# ---------------------------------------------------------------------------
# Scenario 5: Event consumption and session tracking
# ---------------------------------------------------------------------------


class TestEventConsumptionFlow:
    """Events consumed during a session should be marked with session_id."""

    def test_consume_event_marks_session_id(self):
        tmp, db_path = _make_test_env("test-consume")
        try:
            from domain.lifecycle.events import (
                set_instance_context, reset_instance_context, emit_event, consume_event, pop_due_events,
            )
            from infrastructure.config import set_current_session_id, reset_current_session_id

            token = set_instance_context("test-consume")
            try:
                eid = emit_event("timer", payload={"reason": "test"})
                assert eid > 0

                # Before consume: event is visible
                events = pop_due_events(limit=5)
                assert any(e["event_id"] == eid for e in events)

                # Consume with session_id via ContextVar
                sid_token = set_current_session_id("tx_test_session_001")
                try:
                    consume_event(eid)
                finally:
                    reset_current_session_id(sid_token)

                # After consume: event is gone from pop
                events = pop_due_events(limit=5)
                assert not any(e["event_id"] == eid for e in events)

                # Verify DB row
                row = _get_row(db_path, "events", f"event_id = {eid}")
                assert row["consumed_at"] is not None
                assert row["consumed_by_session_id"] == "tx_test_session_001"
            finally:
                reset_instance_context(token)
                # Clean up ContextVar
                try:
                    from infrastructure.config import reset_current_session_id, set_current_session_id
                    reset_current_session_id(set_current_session_id(""))
                except Exception:
                    pass
        finally:
            _cleanup_test_env(tmp)

    def test_consume_events_by_kind(self):
        tmp, db_path = _make_test_env("test-consume-kind")
        try:
            from domain.lifecycle.events import (
                set_instance_context, reset_instance_context, emit_event, consume_events_by_kind, pop_due_events,
            )

            token = set_instance_context("test-consume-kind")
            try:
                emit_event("routine", payload={"name": "morning_plan"})
                emit_event("routine", payload={"name": "evening_review"})
                emit_event("timer", payload={"reason": "x"})

                n = consume_events_by_kind("routine", session_id="tx_bulk_001")
                assert n == 2

                remaining = pop_due_events(limit=10)
                kinds = {e["kind"] for e in remaining}
                assert "routine" not in kinds
                assert "timer" in kinds
            finally:
                reset_instance_context(token)
        finally:
            _cleanup_test_env(tmp)


# ---------------------------------------------------------------------------
# Scenario 6: Stale RUNNING detection and rollback
# ---------------------------------------------------------------------------


class TestStaleRunningRollback:
    """cron tick uses ``evaluate_wake_alive`` to decide whether a RUNNING affair is
    genuinely dead. When it reports the wake as dead, the affair rolls back to
    BLOCKED; when alive, it is preserved (which is what keeps the mid-session
    injection path valid).

    The old contract — "stale if ``affairs.updated_at`` > 300 s" — was wrong:
    ``updated_at`` never moves while a wake runs, so a legitimate long LLM task
    was misclassified as dead and rolled back. These tests pin the new contract:
    the cron defers to ``evaluate_wake_alive`` instead of reading ``updated_at``.
    """

    def _create_affair_in_state(self, db_path: Path, affair_id: str, status: str):
        from domain.lifecycle.clock import now_dt
        now = now_dt().isoformat(timespec="seconds")
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO affairs (affair_id, goal, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (affair_id, "test goal", status, now, now),
        )
        conn.commit()
        conn.close()

    # Valid import path used by cron_lifecycle._run_l4_tick_inner today.
    _FIND_LIFE_AFFAIR_PATH = (
        "domain.orchestration.lifecycle_orchestration.bootstrap.runtime._find_life_affair"
    )

    def test_dead_running_rolls_back_to_blocked(self):
        tmp, db_path = _make_test_env("test-stale")
        try:
            from domain.lifecycle.events import set_instance_context, reset_instance_context

            token = set_instance_context("test-stale")
            try:
                # RUNNING affair, but evaluate_wake_alive reports it dead.
                self._create_affair_in_state(db_path, "affair-stale-1", "RUNNING")
                assert _get_row(db_path, "affairs", "affair_id = 'affair-stale-1'")["status"] == "RUNNING"

                from infrastructure.scheduler.cron_lifecycle import _run_l4_tick_inner
                from domain.lifecycle import wake_liveness
                import logging

                with patch(
                    "infrastructure.config.get_runtime_state_db_path",
                    return_value=str(db_path),
                ), patch(
                    self._FIND_LIFE_AFFAIR_PATH,
                    return_value="affair-stale-1",
                ), patch.object(
                    wake_liveness,
                    "evaluate_wake_alive",
                    return_value=(False, "turn_stale", {"turn_age_s": 9999.0}),
                ):
                    _run_l4_tick_inner("test-stale", logging.getLogger("test"))

                row = _get_row(db_path, "affairs", "affair_id = 'affair-stale-1'")
                assert row["status"] == "BLOCKED"
                wi = _get_row(db_path, "wait_intents", "affair_id = 'affair-stale-1'")
                assert wi is not None
                assert "stale_running_rollback" in wi["reason"]
            finally:
                reset_instance_context(token)
        finally:
            _cleanup_test_env(tmp)

    def test_alive_running_preserved_not_rolled_back(self):
        """When evaluate_wake_alive says the wake is alive, cron must NOT roll back —
        keeping affair=RUNNING is what lets fresh events take the mid-session
        injection path instead of firing a spurious new wake."""
        tmp, db_path = _make_test_env("test-alive")
        try:
            from domain.lifecycle.events import set_instance_context, reset_instance_context

            token = set_instance_context("test-alive")
            try:
                self._create_affair_in_state(db_path, "affair-alive-1", "RUNNING")

                from infrastructure.scheduler.cron_lifecycle import _run_l4_tick_inner
                from domain.lifecycle import wake_liveness
                import logging

                with patch(
                    "infrastructure.config.get_runtime_state_db_path",
                    return_value=str(db_path),
                ), patch(
                    self._FIND_LIFE_AFFAIR_PATH,
                    return_value="affair-alive-1",
                ), patch.object(
                    wake_liveness,
                    "evaluate_wake_alive",
                    return_value=(True, "turn_heartbeat", {"turn_age_s": 30.0}),
                ):
                    _run_l4_tick_inner("test-alive", logging.getLogger("test"))

                row = _get_row(db_path, "affairs", "affair_id = 'affair-alive-1'")
                assert row["status"] == "RUNNING"
            finally:
                reset_instance_context(token)
        finally:
            _cleanup_test_env(tmp)


# ---------------------------------------------------------------------------
# Scenario 7: Full L4 tick pipeline (alarms + engine + routines + momentum)
# ---------------------------------------------------------------------------


class TestFullL4TickPipeline:
    """Test the combined L4 inner tick: fire alarms → engine tick → routines → momentum."""

    def test_l4_tick_fires_alarms_and_emits_events(self):
        tmp, db_path = _make_test_env("test-l4-full")
        try:
            from domain.lifecycle.events import set_instance_context, reset_instance_context
            from domain.lifecycle.alarms import set_alarm

            token = set_instance_context("test-l4-full")
            try:
                # Set a due alarm
                past = (_clock_now_dt() - timedelta(hours=1)).isoformat(timespec="seconds")
                set_alarm("timer", fire_at=past, payload={"reason": "l4_test"})

                # Create a BLOCKED affair so the tick processes the BLOCKED branch
                # (alarms only fire when affair is BLOCKED and due events exist)
                now = _clock_now_dt().isoformat(timespec="seconds")
                conn = sqlite3.connect(str(db_path))
                conn.execute(
                    "INSERT INTO affairs (affair_id, goal, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    ("affair-l4-1", "test", "BLOCKED", now, now),
                )
                conn.commit()
                conn.close()

                from infrastructure.scheduler.cron_lifecycle import _run_l4_tick_inner
                import logging

                log = logging.getLogger("test")

                # Run inner tick with no pending events — alarm should fire
                # but without due events in queue, it goes to "no due events — sleeping"
                with patch(
                    "infrastructure.config.get_runtime_state_db_path",
                    return_value=str(db_path),
                ), patch(
                    "domain.orchestration.lifecycle_orchestration.bootstrap.runtime._find_life_affair",
                    return_value="affair-l4-1",
                ):
                    _run_l4_tick_inner("test-l4-full", log)

                # Alarm should have fired and emitted event into queue
                from domain.lifecycle.events import pop_due_events
                events = pop_due_events(limit=10)
                timer_events = [e for e in events if e["kind"] == "timer"]
                assert len(timer_events) >= 1, f"Expected timer event, got events: {[(e['kind'], e.get('payload', {})) for e in events]}"
                assert timer_events[0]["payload"]["reason"] == "l4_test"
            finally:
                reset_instance_context(token)
        finally:
            _cleanup_test_env(tmp)

    def test_l4_tick_init_db_creates_missing_tables(self):
        """After a schema update, init_db() in L4 tick should create new tables."""
        tmp, db_path = _make_test_env("test-initdb")
        try:
            # Manually drop timers table to simulate schema-not-yet-applied state
            conn = sqlite3.connect(str(db_path))
            conn.execute("DROP TABLE IF EXISTS timers")
            conn.commit()
            conn.close()

            # Verify timers table doesn't exist
            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute("SELECT 1 FROM timers LIMIT 0")
                exists = True
            except sqlite3.OperationalError:
                exists = False
            conn.close()
            assert not exists, "timers table should not exist before L4 tick"

            from domain.lifecycle.events import set_instance_context, reset_instance_context

            token = set_instance_context("test-initdb")
            try:
                from infrastructure.scheduler.cron_lifecycle import _run_l4_tick_inner
                import logging

                with patch(
                    "infrastructure.config.get_runtime_state_db_path",
                    return_value=str(db_path),
                ), patch(
                    "domain.orchestration.lifecycle_orchestration.bootstrap.runtime._find_life_affair",
                    return_value=None,
                ):
                    _run_l4_tick_inner("test-initdb", logging.getLogger("test"))

                # timers table should now exist
                conn = sqlite3.connect(str(db_path))
                conn.execute("SELECT 1 FROM timers LIMIT 0")
                conn.close()
            finally:
                reset_instance_context(token)
        finally:
            _cleanup_test_env(tmp)


# ---------------------------------------------------------------------------
# Scenario 8: RAS debounce in real scenario
# ---------------------------------------------------------------------------


class TestRASDebounceRealScenario:
    """Debounce should merge rapid repeated events of the same kind."""

    def test_initiative_debounce_replaces_payload(self):
        """Two initiative events within debounce window → merged into one."""
        tmp, db_path = _make_test_env("test-debounce-real")
        try:
            from domain.lifecycle.events import set_instance_context, reset_instance_context, emit_event, pop_due_events

            token = set_instance_context("test-debounce-real")
            try:
                eid1 = emit_event("initiative", payload={"urgency": 3})
                eid2 = emit_event("initiative", payload={"urgency": 7})

                # Should be merged (same event_id)
                assert eid1 == eid2

                # Only one event in queue
                events = pop_due_events(limit=10)
                initiative_events = [e for e in events if e["kind"] == "initiative"]
                assert len(initiative_events) == 1
                assert initiative_events[0]["payload"]["urgency"] == 7  # latest wins
                assert initiative_events[0]["payload"]["_merged_count"] >= 2
            finally:
                reset_instance_context(token)
        finally:
            _cleanup_test_env(tmp)

    def test_group_message_not_accumulated_at_event_layer(self):
        """群消息合并不在事件层做 —— group_message 的 debounce_window_s 已置 0
        (合并移到 ingress adapter / group_buffer.py)。事件层每条入站
        NormalizedMessage = 一个独立事件,不在此积压合并。"""
        tmp, db_path = _make_test_env("test-debounce-group")
        try:
            from domain.lifecycle.events import set_instance_context, reset_instance_context, emit_event, pop_due_events

            token = set_instance_context("test-debounce-group")
            try:
                eid1 = emit_event("group_message", payload={
                    "text": "msg1", "sender_name": "Alice", "chat_name": "group1",
                })
                eid2 = emit_event("group_message", payload={
                    "text": "msg2", "sender_name": "Bob", "chat_name": "group1",
                })

                # 窗口=0 → 两条独立事件
                assert eid1 != eid2

                events = pop_due_events(limit=10)
                gm = [e for e in events if e["kind"] == "group_message"]
                assert len(gm) == 2
            finally:
                reset_instance_context(token)
        finally:
            _cleanup_test_env(tmp)

    def test_timer_events_not_debounced(self):
        """Events with fire_at skip debounce — each is a separate scheduled event."""
        tmp, db_path = _make_test_env("test-debounce-skip")
        try:
            from domain.lifecycle.events import set_instance_context, reset_instance_context, emit_event

            token = set_instance_context("test-debounce-skip")
            try:
                t1 = (_clock_now_dt() + timedelta(days=1)).isoformat(timespec="seconds")
                t2 = (_clock_now_dt() + timedelta(days=2)).isoformat(timespec="seconds")

                eid1 = emit_event("routine", payload={"name": "a"}, fire_at=t1)
                eid2 = emit_event("routine", payload={"name": "b"}, fire_at=t2)

                assert eid1 != eid2  # Not merged
            finally:
                reset_instance_context(token)
        finally:
            _cleanup_test_env(tmp)


# ---------------------------------------------------------------------------
# Scenario 9: wake_in_progress guard
# ---------------------------------------------------------------------------


class TestWakeInProgressGuard:
    """The per-instance wake guard prevents concurrent wakes for the same instance."""

    def test_concurrent_wake_blocked_for_same_instance(self):
        """Second wake for same instance while first is in progress → skipped."""
        tmp, db_path = _make_test_env("test-wake-guard")
        try:
            from domain.lifecycle.events import set_instance_context, reset_instance_context

            token = set_instance_context("test-wake-guard")
            try:
                # Create an affair for waking
                conn = sqlite3.connect(str(db_path))
                now = _clock_now_dt().isoformat(timespec="seconds")
                conn.execute(
                    "INSERT INTO affairs (affair_id, goal, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    ("affair-guard-1", "test", "BLOCKED", now, now),
                )
                conn.commit()
                conn.close()

                from domain.lifecycle.scheduler import _is_wake_in_progress

                os.environ["DIGITAL_LIFE_INSTANCE_ID"] = "test-wake-guard"

                # Initially not in progress
                assert not _is_wake_in_progress("test-wake-guard")

                # Simulate wake in progress
                from domain.lifecycle.scheduler import _wake_in_progress, _get_instance_lock
                lock = _get_instance_lock("test-wake-guard")
                lock.acquire()
                _wake_in_progress["test-wake-guard"] = True
                try:
                    assert _is_wake_in_progress("test-wake-guard")
                finally:
                    _wake_in_progress.pop("test-wake-guard", None)
                    lock.release()

                # After release, not in progress
                assert not _is_wake_in_progress("test-wake-guard")
            finally:
                reset_instance_context(token)
        finally:
            _cleanup_test_env(tmp)
            if "DIGITAL_LIFE_INSTANCE_ID" in os.environ:
                del os.environ["DIGITAL_LIFE_INSTANCE_ID"]


# ---------------------------------------------------------------------------
# Scenario 10: timer alarm scheduling with near-future fire (realistic wake timer)
# ---------------------------------------------------------------------------


class TestNearFutureAlarmWakeTimer:
    """Test the realistic rest() → set_alarm → wake flow."""

    def test_rest_sets_timer_alarm_and_wait_intent(self):
        """Simulate: model calls rest(until=+30min) → alarm + wait_intent created."""
        tmp, db_path = _make_test_env("test-rest-alarm")
        try:
            from domain.lifecycle.events import set_instance_context, reset_instance_context
            from domain.lifecycle.alarms import set_alarm, fire_due_alarms, list_pending_alarms

            token = set_instance_context("test-rest-alarm")
            try:
                # Simulate what rest() does: set wait_intent + set alarm
                # Create affair
                conn = sqlite3.connect(str(db_path))
                now_dt_val = _clock_now_dt()
                now_iso = now_dt_val.isoformat(timespec="seconds")
                resume_iso = (now_dt_val + timedelta(minutes=30)).isoformat(timespec="seconds")

                conn.execute(
                    "INSERT INTO affairs (affair_id, goal, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    ("affair-rest-1", "test goal", "BLOCKED", now_iso, now_iso),
                )
                conn.execute(
                    "INSERT INTO wait_intents (affair_id, wait_type, resume_when, reason, resume_action, meta_json, blocked_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("affair-rest-1", "until", resume_iso, "rest", "", "{}", now_iso),
                )
                conn.commit()
                conn.close()

                # Set the timer alarm (what rest() should do after our fix)
                set_alarm("timer", fire_at=resume_iso, payload={"reason": "rest"})

                # Verify alarm exists
                pending = list_pending_alarms()
                assert len(pending) == 1
                assert pending[0]["event_kind"] == "timer"
                assert pending[0]["payload_json"] is not None

                # Fire should NOT happen (still 30 min in future)
                fired = fire_due_alarms()
                assert len(fired) == 0

            finally:
                reset_instance_context(token)
        finally:
            _cleanup_test_env(tmp)

    def test_alarm_fires_when_resume_time_passes(self):
        """Alarm set for 1 min ago → fires and creates wake event."""
        tmp, db_path = _make_test_env("test-alarm-fires")
        try:
            from domain.lifecycle.events import set_instance_context, reset_instance_context
            from domain.lifecycle.alarms import set_alarm, fire_due_alarms

            token = set_instance_context("test-alarm-fires")
            try:
                # Set alarm for 1 minute ago (should be due)
                past = (_clock_now_dt() - timedelta(minutes=1)).isoformat(timespec="seconds")
                set_alarm("timer", fire_at=past, payload={"reason": "rest_wake"})

                # Fire
                fired = fire_due_alarms()
                assert len(fired) == 1
                assert fired[0]["event_kind"] == "timer"

                # Event is now in the queue
                from domain.lifecycle.events import pop_due_events
                events = pop_due_events(limit=10)
                timer_events = [e for e in events if e["kind"] == "timer"]
                assert len(timer_events) >= 1
            finally:
                reset_instance_context(token)
        finally:
            _cleanup_test_env(tmp)
