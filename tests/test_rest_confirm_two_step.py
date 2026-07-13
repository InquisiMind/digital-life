"""rest 两步式（per-session once）语义回归测试。

设计契约（见 rest 工具 schema description）：
  - 同 session 第一次调 rest（不管 until / reuse / 重叠场景）→ preview + 提示卡，不进 BLOCKED
  - 同 session 第二次调（任意参数）→ 真睡（set_alarm + BLOCKED + __l4_block__）
  - 显式 confirm=true → 跳过提示卡直接睡
  - 同 session 第三次仍直接睡（不重复打扰）
"""
from __future__ import annotations

import json
from unittest.mock import patch, MagicMock
import pytest

import interfaces.tools.action_tools as _at


@pytest.fixture
def isolated_instance(monkeypatch, tmp_path):
    iid = "test-rest-session-iid"
    from infrastructure.config import set_current_instance_id, reset_current_instance_id
    token = set_current_instance_id(iid)
    # 每个 test 用新 session_id + 清理 _rest_card_shown_sessions
    _at._rest_card_shown_sessions.clear()
    yield iid
    _at._rest_card_shown_sessions.clear()
    reset_current_instance_id(token)


def _make_args(**kw):
    args = {"until": "2026-07-13T23:30:00+08:00"}
    args.update(kw)
    return args


def test_first_call_until_returns_preview(isolated_instance, monkeypatch):
    """同 session 首次调 rest(until=...) → preview，不 BLOCKED。"""
    set_alarm_calls = []
    monkeypatch.setattr("domain.lifecycle.alarms.set_alarm",
                        lambda *a, **kw: set_alarm_calls.append((a, kw)))
    monkeypatch.setattr("domain.lifecycle.alarms.list_pending_alarms", lambda *a, **kw: [])
    monkeypatch.setattr("domain.todos.crud.list_tasks", lambda **kw: [], raising=False)
    monkeypatch.setattr("domain.project.loader.load_all_projects", lambda: {}, raising=False)
    monkeypatch.setattr("domain.memory.memory.consciousness.runtime.read_insights", lambda **kw: "", raising=False)
    monkeypatch.setattr("domain.lifecycle.runtime_context.get_current_affair", lambda: "affair-1")
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.update_affair", lambda *a, **kw: None)

    out = _at._handle_rest(_make_args(), session_id="sess-A")
    data = json.loads(out) if isinstance(out, str) else out
    assert data.get("preview") is True
    assert "__l4_block__" not in data
    assert not set_alarm_calls


def test_second_call_until_in_same_session_real_sleeps(isolated_instance, monkeypatch):
    """同 session 第二次调 rest(until=...) → 真睡（不弹提示卡）。"""
    set_alarm_calls = []
    monkeypatch.setattr("domain.lifecycle.alarms.set_alarm",
                        lambda *a, **kw: set_alarm_calls.append((a, kw)))
    monkeypatch.setattr("domain.lifecycle.alarms.list_pending_alarms", lambda *a, **kw: [])
    monkeypatch.setattr("domain.memory.memory.consciousness.runtime.read_insights", lambda **kw: "", raising=False)
    monkeypatch.setattr("domain.lifecycle.runtime_context.get_current_affair", lambda: "affair-2")
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.get_affair", lambda aid: MagicMock())
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.update_affair", lambda *a, **kw: None)
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.set_wait_intent", lambda aid, intent: None)

    # 第一次：preview
    out1 = _at._handle_rest(_make_args(), session_id="sess-B")
    data1 = json.loads(out1) if isinstance(out1, str) else out1
    assert data1.get("preview") is True

    # 第二次：真睡
    out2 = _at._handle_rest(_Make_args(), session_id="sess-B")
    data2 = json.loads(out2) if isinstance(out2, str) else out2
    assert data2.get("__l4_block__") is True, "第二次调用必须真睡"
    assert data2.get("preview") is not True
    assert set_alarm_calls, "第二次调用必须 set_alarm"


def _Make_args(**kw):
    """避免命名冲突辅助"""
    args = {"until": "2026-07-13T23:30:00+08:00"}
    args.update(kw)
    return args


def test_first_call_reuse_also_returns_preview(isolated_instance, monkeypatch):
    """首次调 reuse → 也是 preview（不再像旧版默认 confirm=True 直接睡）"""
    list_pending = MagicMock(return_value=[
        {"id": 42, "fire_at": "2026-07-14T08:00:00+08:00", "payload_json": "{}"},
    ])
    monkeypatch.setattr("domain.lifecycle.alarms.list_pending_alarms", list_pending)
    monkeypatch.setattr("domain.memory.memory.consciousness.runtime.read_insights", lambda **kw: "", raising=False)
    monkeypatch.setattr("domain.todos.crud.list_tasks", lambda **kw: [], raising=False)
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.update_affair", lambda *a, **kw: None)
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.set_wait_intent", lambda aid, intent: None)
    cancel_calls = []
    monkeypatch.setattr("domain.lifecycle.alarms.cancel_alarm",
                        lambda *a, **kw: cancel_calls.append((a, kw)))

    out = _at._handle_rest({"reuse": 42}, session_id="sess-C")
    data = json.loads(out) if isinstance(out, str) else out
    assert data.get("preview") is True
    assert data.get("will_reuse_alarm_id") == 42
    assert not cancel_calls, "首次 reuse 不应取消/合并 mental"


def test_second_call_reuse_real_sleeps(isolated_instance, monkeypatch):
    """首次 reuse preview，第二次 reuse 真睡"""
    list_pending = MagicMock(return_value=[
        {"id": 42, "fire_at": "2026-07-14T08:00:00+08:00", "payload_json": "{}"},
    ])
    monkeypatch.setattr("domain.lifecycle.alarms.list_pending_alarms", list_pending)
    monkeypatch.setattr("domain.memory.memory.consciousness.runtime.read_insights", lambda **kw: "", raising=False)
    monkeypatch.setattr("domain.todos.crud.list_tasks", lambda **kw: [], raising=False)
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.get_affair", lambda aid: MagicMock())
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.update_affair", lambda *a, **kw: None)
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.set_wait_intent", lambda aid, intent: None)
    monkeypatch.setattr("domain.lifecycle.alarms.cancel_alarm", lambda *a, **kw: None)
    monkeypatch.setattr("domain.lifecycle.alarms.set_alarm", lambda *a, **kw: None)

    out1 = _at._handle_rest({"reuse": 42}, session_id="sess-D")
    assert json.loads(out1).get("preview") is True

    out2 = _at._handle_rest({"reuse": 42}, session_id="sess-D")
    data2 = json.loads(out2) if isinstance(out2, str) else out2
    assert data2.get("__l4_block__") is True
    assert data2.get("reused_alarm_id") == 42


def test_explicit_confirm_true_skips_preview_first_call(isolated_instance, monkeypatch):
    """rest(until=..., confirm=true) 第一次就走真睡（跳过提示卡）。"""
    monkeypatch.setattr("domain.lifecycle.alarms.list_pending_alarms", lambda *a, **kw: [])
    monkeypatch.setattr("domain.memory.memory.consciousness.runtime.read_insights", lambda **kw: "", raising=False)
    monkeypatch.setattr("domain.lifecycle.runtime_context.get_current_affair", lambda: "affair-X")
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.get_affair", lambda aid: MagicMock())
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.update_affair", lambda *a, **kw: None)
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.set_wait_intent", lambda aid, intent: None)
    monkeypatch.setattr("domain.lifecycle.alarms.set_alarm", lambda *a, **kw: None)

    out = _at._handle_rest(_make_args(confirm=True), session_id="sess-E")
    data = json.loads(out) if isinstance(out, str) else out
    assert data.get("__l4_block__") is True
    assert data.get("preview") is not True


def test_overlap_with_pending_does_show_card_first_time(isolated_instance, monkeypatch):
    """until=X 和现有 timer 重叠 → 第一次也展示提示卡（不再 tool_error 后失去机会）。"""
    monkeypatch.setattr("domain.lifecycle.alarms.list_pending_alarms", lambda *a, **kw: [
        {"id": 88, "fire_at": "2026-07-13T23:35:00+08:00", "payload_json": "{}"}  # 5min 内重叠
    ])
    monkeypatch.setattr("domain.memory.memory.consciousness.runtime.read_insights", lambda **kw: "", raising=False)
    monkeypatch.setattr("domain.todos.crud.list_tasks", lambda **kw: [], raising=False)

    out = _at._handle_rest(_make_args(until="2026-07-13T23:30:00+08:00"), session_id="sess-F")
    data = json.loads(out) if isinstance(out, str) else out
    assert data.get("preview") is True, "重叠场景首次应展示提示卡"
    assert data.get("overlap") is True
    assert data.get("overlap_alarm_id") == 88
    assert "pre_rest_card" in data


def test_overlap_second_call_reuse_real_sleeps(isolated_instance, monkeypatch):
    """看完重叠提示卡后，第二次 reuse 真睡（不卡）。"""
    list_pending = MagicMock(return_value=[
        {"id": 88, "fire_at": "2026-07-13T23:35:00+08:00", "payload_json": "{}"}
    ])
    monkeypatch.setattr("domain.lifecycle.alarms.list_pending_alarms", list_pending)
    monkeypatch.setattr("domain.memory.memory.consciousness.runtime.read_insights", lambda **kw: "", raising=False)
    monkeypatch.setattr("domain.todos.crud.list_tasks", lambda **kw: [], raising=False)
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.get_affair", lambda aid: MagicMock())
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.update_affair", lambda *a, **kw: None)
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.set_wait_intent", lambda aid, intent: None)
    monkeypatch.setattr("domain.lifecycle.alarms.cancel_alarm", lambda *a, **kw: None)
    monkeypatch.setattr("domain.lifecycle.alarms.set_alarm", lambda *a, **kw: None)

    # 第一次 until 触发重叠 → preview
    out1 = _at._handle_rest(_make_args(until="2026-07-13T23:30:00+08:00"), session_id="sess-G")
    data1 = json.loads(out1) if isinstance(out1, str) else out1
    assert data1.get("preview") is True and data1.get("overlap") is True

    # 第二次 reuse=88 → 真睡
    out2 = _at._handle_rest({"reuse": 88}, session_id="sess-G")
    data2 = json.loads(out2) if isinstance(out2, str) else out2
    assert data2.get("__l4_block__") is True
    assert data2.get("reused_alarm_id") == 88


def test_different_session_each_shows_card(isolated_instance, monkeypatch):
    """两个 session 各自独立看到提示卡（per-session once，不是 process-wide once）。"""
    monkeypatch.setattr("domain.lifecycle.alarms.list_pending_alarms", lambda *a, **kw: [])
    monkeypatch.setattr("domain.memory.memory.consciousness.runtime.read_insights", lambda **kw: "", raising=False)
    monkeypatch.setattr("domain.todos.crud.list_tasks", lambda **kw: [], raising=False)

    out1 = _at._handle_rest(_make_args(), session_id="sess-H1")
    out2 = _at._handle_rest(_make_args(), session_id="sess-H2")
    d1 = json.loads(out1) if isinstance(out1, str) else out1
    d2 = json.loads(out2) if isinstance(out2, str) else out2
    assert d1.get("preview") is True
    assert d2.get("preview") is True, "不同 session 都应看到提示卡"
