"""风暴合批（storm debounce）测试。

2026-10-08 吴总项目群聊实测：26 个 group_message 事件产生 16 次独立唤醒，
并发 worker 把全队拖进 GLM 429。修复 = group_message 防抖窗口 45s + 空闲
派发侧风暴延后（同 kind 窗口内刚消费过 → fire_at 推后，让后续消息合并）。

语义锚点：
- 首条消息（窗口内无已消费同类）→ 即时唤醒，不延后
- 窗口内刚醒过又来一条 → 延后 fire_at=now+window，且 fire_at 不随合并滚动（延迟有上界）
- 延后期间再来同 kind → _apply_debounce 合并进同一条（accumulate 攒 _merged_texts）
- window=0 的 kind（如 message 私聊）→ 永不延后
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from datetime import timedelta
from pathlib import Path

from domain.lifecycle.clock import now_dt

import pytest


def _make_env(instance_id: str) -> tuple[str, Path]:
    tmp = tempfile.mkdtemp()
    db_path = Path(tmp) / "state.db"
    os.environ["DIGITAL_LIFE_INSTANCE_ID"] = instance_id
    os.environ["DIGITAL_LIFE_RUNTIME_HOME"] = tmp

    from domain.lifecycle.affairs.runtime import configure_runtime_hooks, init_db

    configure_runtime_hooks(db_path=db_path)
    init_db()
    return tmp, db_path


def _cleanup(tmp: str) -> None:
    shutil.rmtree(tmp, ignore_errors=True)
    os.environ.pop("DIGITAL_LIFE_INSTANCE_ID", None)
    os.environ.pop("DIGITAL_LIFE_RUNTIME_HOME", None)


class _FakeTimer:
    """拦住 _defer_storm_event 的真定时器——测试里不让它 53s 后真的起 wake。"""

    started: list[tuple[float, object, tuple]] = []

    def __init__(self, interval, function, args=None):
        _FakeTimer.started.append((interval, function, args or ()))
        self.daemon = False

    def start(self):
        pass

    def cancel(self):
        pass


@pytest.fixture()
def storm_env(monkeypatch):
    monkeypatch.setattr("threading.Timer", _FakeTimer)
    _FakeTimer.started.clear()
    tmp, db_path = _make_env("test-storm")
    yield db_path
    _cleanup(tmp)


def _ctx(instance_id: str):
    from domain.lifecycle.events import reset_instance_context, set_instance_context

    token = set_instance_context(instance_id)
    return token


def test_first_message_not_deferred(storm_env):
    """窗口内没有已消费同类 → 首条不延后（即时唤醒语义保持）。"""
    from domain.lifecycle.events import _storm_should_defer, emit_event

    token = _ctx("test-storm")
    try:
        eid = emit_event("group_message", payload={"text": "第一条", "sender_name": "合伙人"})
        ev = _peek(eid)
        assert ev is not None
        assert not _storm_should_defer(ev, "instance:test-storm")
    finally:
        from domain.lifecycle.events import reset_instance_context

        reset_instance_context(token)


def test_recent_consumption_defers_next(storm_env):
    """同 kind 刚消费过（45s 窗口内）→ 下一条该延后。"""
    from domain.lifecycle.events import (
        _storm_should_defer,
        consume_event,
        emit_event,
    )

    token = _ctx("test-storm")
    try:
        eid = emit_event("group_message", payload={"text": "第一条", "sender_name": "合伙人"})
        consume_event(eid)
        eid2 = emit_event("group_message", payload={"text": "第二条", "sender_name": "质控"})
        ev2 = _peek(eid2)
        assert _storm_should_defer(ev2, "instance:test-storm")

        # window=0 的 kind（message 私聊）永不延后
        eid3 = emit_event("message", payload={"text": "客户私聊", "sender_name": "吴总"})
        consume_event(eid3)
        eid4 = emit_event("message", payload={"text": "再一条", "sender_name": "吴总"})
        assert not _storm_should_defer(_peek(eid4), "instance:test-storm")
    finally:
        from domain.lifecycle.events import reset_instance_context

        reset_instance_context(token)


def test_defer_sets_future_fire_at_without_timer_fire(storm_env):
    """延后 = fire_at 推到 now+window；真定时器被替换不触发 wake。"""
    from domain.lifecycle.events import (
        _defer_storm_event,
        consume_event,
        emit_event,
        peek_unconsumed_event,
    )

    token = _ctx("test-storm")
    try:
        eid = emit_event("group_message", payload={"text": "首条", "sender_name": "合伙人"})
        consume_event(eid)
        eid2 = emit_event("group_message", payload={"text": "第二条", "sender_name": "质控"})
        ev2 = _peek(eid2)
        _defer_storm_event(ev2, "test-storm", "affair-x")

        stored = peek_unconsumed_event(eid2)
        assert stored is not None
        assert stored.get("fire_at"), "延后事件必须有 fire_at"
        fire_dt = _parse_ts(stored["fire_at"])
        assert timedelta(0) < fire_dt - now_dt() <= timedelta(seconds=50)
        # 只排了 kick 定时器，且没有真的跑起 wake（_FakeTimer.start 是 no-op）
        assert len(_FakeTimer.started) == 1
        assert _FakeTimer.started[0][0] > 45
    finally:
        from domain.lifecycle.events import reset_instance_context

        reset_instance_context(token)


def test_merge_into_deferred_event_keeps_fire_at(storm_env):
    """延后窗口内再来同 kind → 合并进同一条，fire_at 不滚动（延迟有上界）。"""
    from domain.lifecycle.events import (
        _defer_storm_event,
        consume_event,
        emit_event,
        peek_unconsumed_event,
        pop_due_events,
    )

    token = _ctx("test-storm")
    try:
        eid = emit_event("group_message", payload={"text": "首条", "sender_name": "合伙人"})
        consume_event(eid)
        eid2 = emit_event("group_message", payload={"text": "第二条", "sender_name": "质控"})
        _defer_storm_event(_peek(eid2), "test-storm", "affair-x")
        fire_before = peek_unconsumed_event(eid2)["fire_at"]

        eid3 = emit_event("group_message", payload={"text": "第三条", "sender_name": "设计师"})
        assert eid3 == eid2, "窗口内命中防抖必须合并进延后事件，不新建"

        merged = peek_unconsumed_event(eid2)
        import json as _json

        payload = _json.loads(merged["payload"]) if isinstance(merged["payload"], str) else merged["payload"]
        assert payload.get("_merged_count") == 2
        assert payload.get("text") == "第三条", "主字段指向最新一条"
        texts = payload.get("_merged_texts") or []
        # 历史合并条进 _merged_texts，最新一条留在主字段（wake_prompt 主行 + 历史块）
        assert texts and texts[0]["sender"] == "质控" and texts[0]["text"] == "第二条"

        assert peek_unconsumed_event(eid2)["fire_at"] == fire_before, "合并不得滚动 fire_at"
        # 到期前 pop 不到（fire_at 守卫），到期后一醒带走全部合并内容
        assert pop_due_events(limit=20) == []
    finally:
        from domain.lifecycle.events import reset_instance_context

        reset_instance_context(token)


def _peek(event_id: int):
    from domain.lifecycle.events import peek_unconsumed_event

    return peek_unconsumed_event(event_id)


def _parse_ts(v):
    from datetime import datetime

    return datetime.fromisoformat(str(v))
