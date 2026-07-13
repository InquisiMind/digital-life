"""Unit tests for the wake/liveness audit signals on ``RuntimeLogDB``.

Covers ``last_turn_at`` / ``last_wake_started_at`` — the turn-heartbeat that
the cron stale-RUNNING check (see ``wake_liveness.evaluate_wake_alive``) uses
to distinguish “wake still running a long LLM task” from “wake died”.
"""

from __future__ import annotations

import time

from infrastructure.persistence.instance.runtime_log import RuntimeLogDB


def _make_db(tmp_path) -> RuntimeLogDB:
    return RuntimeLogDB(db_path=tmp_path / "rt.db", instance_id="t1")


def test_last_turn_at_returns_none_when_table_empty(tmp_path) -> None:
    db = _make_db(tmp_path)
    assert db.last_turn_at() is None


def test_last_turn_at_returns_max_timestamp_across_turns(tmp_path) -> None:
    db = _make_db(tmp_path)
    wake_id = db.create_wake(wake_seq=1)

    db.append_turn(wake_id=wake_id, wake_seq=1, llm_call_seq=0, role="user", timestamp=100.0)
    db.append_turn(wake_id=wake_id, wake_seq=1, llm_call_seq=0, role="assistant", timestamp=250.0)
    db.append_turn(wake_id=wake_id, wake_seq=1, llm_call_seq=1, role="assistant", timestamp=400.0)

    assert db.last_turn_at() == 400.0


def test_last_turn_at_is_scoped_to_instance_id(tmp_path) -> None:
    """Two instances share one DB file path pattern but must not cross-contaminate."""
    db_a = RuntimeLogDB(db_path=tmp_path / "a.db", instance_id="alpha")
    db_b = RuntimeLogDB(db_path=tmp_path / "b.db", instance_id="beta")

    wid_a = db_a.create_wake(wake_seq=1)
    db_a.append_turn(wake_id=wid_a, wake_seq=1, llm_call_seq=0, role="user", timestamp=111.0)

    # beta has no turns yet
    assert db_b.last_turn_at() is None
    assert db_a.last_turn_at() == 111.0


def test_last_turn_at_updates_as_wake_progresses(tmp_path) -> None:
    """Heartbeat must advance as the wake keeps producing turns — this is the
    property that lets cron say “still alive, don't roll back”."""
    db = _make_db(tmp_path)
    wake_id = db.create_wake(wake_seq=1)

    db.append_turn(wake_id=wake_id, wake_seq=1, llm_call_seq=0, role="user", timestamp=1000.0)
    first = db.last_turn_at()
    assert first == 1000.0

    # Simulate a later LLM call landing a turn.
    db.append_turn(wake_id=wake_id, wake_seq=1, llm_call_seq=1, role="assistant", timestamp=1500.0)
    assert db.last_turn_at() == 1500.0


def test_last_wake_started_at_returns_none_when_empty(tmp_path) -> None:
    db = _make_db(tmp_path)
    assert db.last_wake_started_at() is None


def test_last_wake_started_at_returns_most_recent(tmp_path) -> None:
    db = _make_db(tmp_path)
    db.create_wake(wake_seq=1, started_at=100.0)
    db.create_wake(wake_seq=2, started_at=300.0)
    db.create_wake(wake_seq=3, started_at=200.0)  # out-of-order started_at

    assert db.last_wake_started_at() == 300.0


def test_last_wake_started_at_used_when_no_turn_yet(tmp_path) -> None:
    """The startup-window fallback: a brand-new wake has not produced its first
    turn yet, so ``last_turn_at`` is None but the wake is genuinely alive."""
    db = _make_db(tmp_path)
    started = time.time()
    db.create_wake(wake_seq=1, started_at=started)

    assert db.last_turn_at() is None
    assert db.last_wake_started_at() == started
