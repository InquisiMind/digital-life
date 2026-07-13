"""Unit tests for :func:`domain.lifecycle.wake_liveness.evaluate_wake_alive`.

This is the function that fixes the alpha "wake #1181 still running but #1182
fired as a new wake" defect: cron now asks *whether the wake is alive* instead
of guessing from an ``updated_at`` timestamp that never moves while a wake runs.
"""

from __future__ import annotations

import domain.lifecycle.wake_liveness as wl


class _FakeAudit:
    def __init__(self, last_turn, last_wake_start) -> None:
        self._last_turn = last_turn
        self._last_wake_start = last_wake_start

    def last_turn_at(self):
        return self._last_turn

    def last_wake_started_at(self):
        return self._last_wake_start


def _evaluate(monkeypatch, **kwargs):
    """Call evaluate_wake_alive with both signal lookouts monkeypatched.

    The module imports ``_is_wake_in_progress`` from scheduler lazily; we stub
    that import path plus the audit factory import.
    """
    import sys
    import types

    fake_scheduler = types.ModuleType("domain.lifecycle.scheduler")
    fake_scheduler._is_wake_in_progress = lambda _iid: kwargs.get("in_progress", False)
    monkeypatch.setitem(sys.modules, "domain.lifecycle.scheduler", fake_scheduler)

    fake_pkg = types.ModuleType("infrastructure.persistence.instance")
    fake_pkg.get_audit = lambda _iid: _FakeAudit(
        kwargs.get("last_turn", None), kwargs.get("last_wake_start", None)
    )
    monkeypatch.setitem(sys.modules, "infrastructure.persistence.instance", fake_pkg)

    return wl.evaluate_wake_alive(
        "inst",
        stale_threshold_s=kwargs.get("stale_threshold_s", 1800.0),
        now=kwargs.get("now", 10_000.0),
    )


# --- Signal 1: in-process flag -------------------------------------------


def test_alive_when_wake_in_progress_flag_set(monkeypatch):
    alive, reason, signals = _evaluate(monkeypatch, in_progress=True)
    assert alive is True
    assert reason == "wake_in_progress"
    assert signals["wake_in_progress"] is True


# --- Signal 2: turn heartbeat --------------------------------------------


def test_alive_when_turn_heartbeat_recent(monkeypatch):
    alive, reason, signals = _evaluate(
        monkeypatch, last_turn=9_500.0, now=10_000.0, stale_threshold_s=1800.0
    )
    assert alive is True
    assert reason == "turn_heartbeat"
    assert signals["turn_age_s"] == 500.0


def test_dead_when_turn_heartbeat_stale(monkeypatch):
    alive, reason, signals = _evaluate(
        monkeypatch, last_turn=1_000.0, now=10_000.0, stale_threshold_s=1800.0
    )
    assert alive is False
    assert reason == "turn_stale"
    assert signals["turn_age_s"] == 9000.0


def test_turn_stale_boundary_is_inclusive_on_threshold(monkeypatch):
    # age == threshold exactly → still alive (<=)
    alive, _, _ = _evaluate(
        monkeypatch, last_turn=8_200.0, now=10_000.0, stale_threshold_s=1800.0
    )
    assert alive is True


# --- Signal 3: wake-just-started fallback (no turn yet) ------------------


def test_alive_when_wake_just_started_no_turn_yet(monkeypatch):
    alive, reason, signals = _evaluate(
        monkeypatch, last_wake_start=9_900.0, now=10_000.0, stale_threshold_s=1800.0
    )
    assert alive is True
    assert reason == "wake_just_started"
    assert signals["last_turn_at"] is None


def test_dead_when_wake_started_long_ago_and_no_turn(monkeypatch):
    # A wake that started 2h ago with zero turns is definitely dead.
    alive, reason, _ = _evaluate(
        monkeypatch, last_wake_start=2_800.0, now=10_000.0, stale_threshold_s=1800.0
    )
    assert alive is False
    assert reason == "wake_started_but_no_turn"


# --- Signal priority -----------------------------------------------------


def test_in_progress_flag_wins_even_if_turn_stale(monkeypatch):
    """The in-process flag is authoritative: if the wake thread holds the lock,
    we must not roll back — even if for some reason turns look old."""
    alive, reason, _ = _evaluate(
        monkeypatch, in_progress=True, last_turn=0.0, now=10_000.0
    )
    assert alive is True
    assert reason == "wake_in_progress"


# --- Default threshold reads the env knob --------------------------------


def test_threshold_default_reads_env(monkeypatch):
    monkeypatch.setenv("DIGITAL_LIFE_STALE_RUNNING_SECONDS", "600")
    import importlib

    reloaded = importlib.reload(wl)
    try:
        assert reloaded.STALE_RUNNING_SECONDS == 600.0
    finally:
        # Restore the module to the repo default so later tests are unaffected.
        monkeypatch.delenv("DIGITAL_LIFE_STALE_RUNNING_SECONDS", raising=False)
        importlib.reload(wl)


def test_threshold_defaults_to_1800_when_env_unset(monkeypatch):
    monkeypatch.delenv("DIGITAL_LIFE_STALE_RUNNING_SECONDS", raising=False)
    import importlib

    reloaded = importlib.reload(wl)
    try:
        assert reloaded.STALE_RUNNING_SECONDS == 1800.0
    finally:
        importlib.reload(wl)
