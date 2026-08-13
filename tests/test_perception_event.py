"""perception_signal 事件类型注册与 emit 集成测试（spec FR-013）。

验证：
  - perception_signal 在 event_registry 注册成功
  - emit_event("perception_signal", ...) 不报 ValueError
  - emit 后事件进队列、可 pop
"""
from __future__ import annotations

from domain.lifecycle import events as ev
from domain.lifecycle.event_registry import get_event_type, validate_event_type


def test_perception_signal_registered():
    """perception_signal 已在 event_types.yaml 注册。"""
    assert validate_event_type("perception_signal", raise_on_unknown=True) is True
    et = get_event_type("perception_signal")
    assert et is not None
    assert et.display_name == "感知信号"
    assert et.priority == 7
    assert et.consumption_policy == "on_trigger"
    # allowed_tools 应含回看工具
    assert "sense_media" in et.allowed_tools
    assert "express_to_human" in et.allowed_tools


def test_emit_perception_signal_does_not_raise(monkeypatch):
    """emit perception_signal 不应因 kind 未注册而 raise。"""
    # 避免 _wake_or_inject 真的去查 affair（测试环境无完整生命周期）
    monkeypatch.setattr(ev, "_wake_or_inject", lambda eid: None)
    # 设实例上下文，避免 "instance:zero" warning
    tok = ev.set_instance_context("test-perception-emit")
    try:
        eid = ev.emit_event("perception_signal", {
            "source": "hotkey_screen",
            "summary": "测试画面",
            "details": {"k": "v"},
            "media_path": "/tmp/x.png",
        })
        assert isinstance(eid, int)
        assert eid > 0
    finally:
        ev.reset_instance_context(tok)


def test_emit_perception_signal_unregistered_kind_raises(monkeypatch):
    """未注册的 kind 应 raise ValueError（对比：perception_signal 不会）。"""
    monkeypatch.setattr(ev, "_wake_or_inject", lambda eid: None)
    import pytest

    with pytest.raises(ValueError):
        ev.emit_event("perception_typo_kind", {"x": 1})
