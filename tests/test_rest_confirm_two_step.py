"""rest 两步式（confirm）语义回归测试。

设计契约（见 rest 工具 schema description）：
  - rest(until=..., confirm=False)（默认）→ preview，不进 BLOCKED，返回提示卡
  - rest(until=..., confirm=True) → 真的 set_alarm + BLOCKED + sentinel
  - reuse 路径默认 confirm=True（你已知有闹钟在那里）
  - reuse + confirm=False → 也是 preview

不真跑 confirm=True（会真把 in_progress 实例睡死，影响生产）。
mock 掉 set_alarm/affairs 等副作用，单独验证「是否带 sentinel」+ 「是否调 set_alarm」。
"""
from __future__ import annotations

import json
from unittest.mock import patch, MagicMock
import pytest


@pytest.fixture
def isolated_instance(monkeypatch, tmp_path):
    iid = "test-rest-confirm-iid"
    from infrastructure.config import set_current_instance_id, reset_current_instance_id
    token = set_current_instance_id(iid)
    yield iid
    reset_current_instance_id(token)


def _make_args(**kw):
    """默认参数 until=23:30，未指定 confirm 时不传 confirm key（验证 default=False）。"""
    args = {"until": "2026-07-13T23:30:00+08:00"}
    args.update(kw)
    return args


def test_until_without_confirm_returns_preview_no_sentinel(isolated_instance, monkeypatch):
    """rest(until=...) 不传 confirm → 预览：不进 BLOCKED（无 __l4_block__）、不调 set_alarm。"""
    set_alarm_calls = []
    monkeypatch.setattr("domain.lifecycle.alarms.set_alarm",
                        lambda *a, **kw: set_alarm_calls.append((a, kw)))
    monkeypatch.setattr("domain.lifecycle.alarms.list_pending_alarms", lambda *a, **kw: [])
    # 干掉 pre_rest_card 让返回简洁
    monkeypatch.setattr("domain.todos.crud.list_tasks", lambda **kw: [], raising=False)
    monkeypatch.setattr("domain.project.loader.load_all_projects", lambda: {}, raising=False)
    monkeypatch.setattr("domain.memory.memory.consciousness.runtime.read_insights", lambda **kw: "", raising=False)
    # 模拟 affairs/affair 存在但 update 不影响——验证 update_affair 没被调
    monkeypatch.setattr("domain.lifecycle.runtime_context.get_current_affair",
                        lambda: "test-affair-1")
    update_calls = []
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.update_affair",
                        lambda *a, **kw: update_calls.append((a, kw)))

    from interfaces.tools.action_tools import _handle_rest
    out = _handle_rest(_make_args())
    data = json.loads(out) if isinstance(out, str) else out

    assert data.get("preview") is True, "默认 should 返回 preview 模式"
    assert "__l4_block__" not in data, "preview 不应该带 sentinel"
    assert "pre_rest_card" in data, "preview 应该带提示卡"
    assert not set_alarm_calls, "preview 不应该 set_alarm"
    assert not update_calls, "preview 不应该 update_affair（不进 BLOCKED）"


def test_until_with_confirm_true_calls_alarm_and_blocks(isolated_instance, monkeypatch):
    """rest(until=..., confirm=true) → 真睡：set_alarm + update_affair + __l4_block__=True。"""
    set_alarm_calls = []
    monkeypatch.setattr("domain.lifecycle.alarms.set_alarm",
                        lambda *a, **kw: set_alarm_calls.append((a, kw)))
    monkeypatch.setattr("domain.lifecycle.alarms.list_pending_alarms", lambda *a, **kw: [])
    monkeypatch.setattr("domain.memory.memory.consciousness.runtime.read_insights", lambda **kw: "", raising=False)
    monkeypatch.setattr("domain.lifecycle.runtime_context.get_current_affair",
                        lambda: "test-affair-2")
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.get_affair",
                        lambda aid: MagicMock())
    update_calls = []
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.update_affair",
                        lambda *a, **kw: update_calls.append((a, kw)))
    set_wait_calls = []
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.set_wait_intent",
                        lambda aid, intent: set_wait_calls.append((aid, intent)))

    from interfaces.tools.action_tools import _handle_rest
    out = _handle_rest(_make_args(confirm=True))
    data = json.loads(out) if isinstance(out, str) else out

    assert data.get("__l4_block__") is True, "confirm=true 应进 BLOCKED"
    assert not data.get("preview"), "confirm=true 不应进 preview 模式"
    assert set_alarm_calls, "confirm=true 必须真 set_alarm"
    assert update_calls, "confirm=true 必须真 update_affair 进 BLOCKED"


def test_reuse_without_confirm_defaults_to_true_blocks(isolated_instance, monkeypatch):
    """reuse 路径默认 confirm=True（你已知有闹钟在那里），直接睡。"""
    list_pending = MagicMock(return_value=[
        {"id": 42, "fire_at": "2026-07-14T08:00:00+08:00", "payload_json": "{}"},
    ])
    monkeypatch.setattr("domain.lifecycle.alarms.list_pending_alarms", list_pending)
    monkeypatch.setattr("domain.memory.memory.consciousness.runtime.read_insights", lambda **kw: "", raising=False)
    monkeypatch.setattr("domain.todos.crud.list_tasks", lambda **kw: [], raising=False)
    monkeypatch.setattr("domain.lifecycle.runtime_context.get_current_affair",
                        lambda: "test-affair-3")
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.get_affair",
                        lambda aid: MagicMock())
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.update_affair", lambda *a, **kw: None)
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.set_wait_intent", lambda aid, intent: None)

    from interfaces.tools.action_tools import _handle_rest
    out = _handle_rest({"reuse": 42})
    data = json.loads(out) if isinstance(out, str) else out

    assert data.get("__l4_block__") is True, "reuse 默认应进 BLOCKED"
    assert data.get("reused_alarm_id") == 42


def test_reuse_with_confirm_false_returns_preview(isolated_instance, monkeypatch):
    """reuse + confirm=False → 仍是 preview 模式（带提示卡 + 不 block）。"""
    list_pending = MagicMock(return_value=[
        {"id": 42, "fire_at": "2026-07-14T08:00:00+08:00", "payload_json": "{}"},
    ])
    monkeypatch.setattr("domain.lifecycle.alarms.list_pending_alarms", list_pending)
    monkeypatch.setattr("domain.memory.memory.consciousness.runtime.read_insights", lambda **kw: "", raising=False)
    monkeypatch.setattr("domain.todos.crud.list_tasks", lambda **kw: [], raising=False)
    monkeypatch.setattr("domain.lifecycle.runtime_context.get_current_affair",
                        lambda: "test-affair-4")
    update_calls = []
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.update_affair",
                        lambda *a, **kw: update_calls.append((a, kw)))
    cancel_calls = []
    monkeypatch.setattr("domain.lifecycle.alarms.cancel_alarm",
                        lambda *a, **kw: cancel_calls.append((a, kw)))
    monkeypatch.setattr("domain.lifecycle.alarms.set_alarm", lambda *a, **kw: None)

    from interfaces.tools.action_tools import _handle_rest
    out = _handle_rest({"reuse": 42, "confirm": False})
    data = json.loads(out) if isinstance(out, str) else out

    assert data.get("preview") is True, "reuse+confirm=False 应进 preview"
    assert "__l4_block__" not in data
    assert data.get("will_reuse_alarm_id") == 42
    assert not update_calls, "preview 不应该 update_affair"
    assert not cancel_calls, "preview 不应该 cancel_alarm（不合并 mental）"


def test_preview_message_guides_model_to_confirm(isolated_instance, monkeypatch):
    """preview 的 message 必须告诉模型怎么『确认睡』——给出完整 confirm=true 调用方式。"""
    monkeypatch.setattr("domain.lifecycle.alarms.list_pending_alarms", lambda *a, **kw: [])
    monkeypatch.setattr("domain.memory.memory.consciousness.runtime.read_insights", lambda **kw: "", raising=False)
    monkeypatch.setattr("domain.todos.crud.list_tasks", lambda **kw: [], raising=False)
    monkeypatch.setattr("domain.project.loader.load_all_projects", lambda: {}, raising=False)

    from interfaces.tools.action_tools import _handle_rest
    # until 路径
    out_u = _handle_rest({"until": "2026-07-14T08:00:00+08:00"})
    data_u = json.loads(out_u) if isinstance(out_u, str) else out_u
    msg_u = data_u.get("message", "")
    assert "confirm=true" in msg_u, "until preview message 必须显式告诉模型 confirm=true"
    assert "2026-07-14T08:00:00+08:00" in msg_u, "until preview 应原样带时间"
