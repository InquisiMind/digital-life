"""rest-boundary 消息保留的回归测试。

历史 bug（wake-469 案例 #256）：模型 rest() 返回 __l4_block__ 后，wake engine
立即 return 结束会话。若 rest 完成的瞬间内存池里恰好有未注入的新事件（真人
消息等），这些事件只能等下一个 wake 才被处理（延迟数分钟到数小时）；进程
崩溃则永久丢失（DB 已 consumed、内存池蒸发）。

修复（agent.py:run_conversation 的 session_blocked 分支）：rest 完成后检查
内存池，若有未注入事件 → 调用 _revoke_rest_and_resume 撤销 rest 副作用、
回滚 affair → RUNNING，让 for 循环进入下一次 _chat 处理新事件。

本测试绕过 AIAgent.__post_init__（避免触发 provider/registry 加载副作用），
直接测三个新 helper 方法 + rest 返回值解析契约。
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from infrastructure.ai.agent import AIAgent


def _make_bare_agent() -> AIAgent:
    """构造一个不触发 __post_init__ 的 AIAgent（跳过 provider/registry 加载）。

    由于 AIAgent 是 dataclass，常规实例化会跑 __post_init__。我们用
    object.__new__ 绕过它，手动设 _injected_signal_event_ids——这是
    _has_uninjected_signalled_events 依赖的唯一状态。
    """
    agent = object.__new__(AIAgent)
    agent._injected_signal_event_ids = set()
    return agent


# ── _has_uninjected_signalled_events ──────────────────────────────────────


def test_no_events_returns_false():
    agent = _make_bare_agent()
    with patch("domain.lifecycle.session_events.peek_signalled_events", return_value=[]):
        assert agent._has_uninjected_signalled_events() is False


def test_only_already_injected_events_returns_false():
    """已注入过的事件不应再次触发 rest 撤销（避免死循环）。"""
    agent = _make_bare_agent()
    agent._injected_signal_event_ids = {101, 102}
    events = [{"event_id": 101}, {"event_id": 102}]
    with patch("domain.lifecycle.session_events.peek_signalled_events", return_value=events):
        assert agent._has_uninjected_signalled_events() is False


def test_has_new_event_returns_true():
    agent = _make_bare_agent()
    agent._injected_signal_event_ids = {101}
    events = [{"event_id": 101}, {"event_id": 999}]  # 999 是新的
    with patch("domain.lifecycle.session_events.peek_signalled_events", return_value=events):
        assert agent._has_uninjected_signalled_events() is True


# ── _revoke_rest_and_resume 各 rest 路径的回滚 ──────────────────────────


def test_revoke_until_path_new_alarm_cancels_and_resets_affair():
    """until 路径（新建闹钟）：sentinel 缺 set_alarm 字段 → 判为新建 → cancel alarm + affair 回 RUNNING。"""
    wake_at = "2026-07-03T21:40:00+08:00"
    rest_result = {"name": "rest", "arguments": {}, "result": json.dumps({
        "__l4_block__": True,
        "started": True,
        "affair_id": "aff-xyz",
        "wake_at": wake_at,
        "mental_context": "休息",
        "message": "进入休息...",
    })}
    agent = _make_bare_agent()

    cancelled = []
    alarms_list = [{"id": 555, "event_kind": "timer", "fire_at": wake_at}]

    def fake_cancel(aid):
        cancelled.append(aid)
        return True

    affair_updates = []
    cleared_intents = []

    with patch("domain.lifecycle.alarms.list_pending_alarms", return_value=alarms_list), \
         patch("domain.lifecycle.alarms.cancel_alarm", side_effect=fake_cancel), \
         patch("domain.lifecycle.affairs.runtime.update_affair",
               side_effect=lambda aid, **kw: affair_updates.append((aid, kw))), \
         patch("domain.lifecycle.affairs.runtime.clear_wait_intent",
               side_effect=lambda aid: cleared_intents.append(aid)):
        ok = agent._revoke_rest_and_resume(rest_result)

    assert ok is True
    assert cancelled == [555]                    # 新建闹钟被取消
    assert affair_updates == [("aff-xyz", {"status": "RUNNING"})]
    assert cleared_intents == ["aff-xyz"]


def test_revoke_reuse_path_keeps_existing_alarm():
    """reuse 路径：set_alarm=False + reused_alarm_id → 复用的闹钟**不取消**，只回滚 affair。"""
    rest_result = {"name": "rest", "arguments": {}, "result": json.dumps({
        "__l4_block__": True,
        "started": True,
        "set_alarm": False,
        "reused_alarm_id": 234,
        "fire_at": "2026-07-03T21:40:00+08:00",
        "affair_id": "aff-xyz",
    })}
    agent = _make_bare_agent()

    cancelled = []
    with patch("domain.lifecycle.alarms.list_pending_alarms", return_value=[]), \
         patch("domain.lifecycle.alarms.cancel_alarm", side_effect=cancelled.append), \
         patch("domain.lifecycle.affairs.runtime.update_affair", side_effect=lambda *a, **k: None), \
         patch("domain.lifecycle.affairs.runtime.clear_wait_intent", side_effect=lambda *a: None):
        ok = agent._revoke_rest_and_resume(rest_result)

    assert ok is True
    assert cancelled == []                       # 复用闹钟不被取消


def test_revoke_invalid_json_returns_false():
    """rest result 解析失败 → 返回 False，保持原 rest 生效让 cron 兜底。"""
    agent = _make_bare_agent()
    rest_result = {"name": "rest", "arguments": {}, "result": "not json"}
    assert agent._revoke_rest_and_resume(rest_result) is False


def test_revoke_no_affair_path_only_cancels_alarm():
    """until 但 affair 不存在（兜底路径）：affair_id=None → 只取消闹钟，不碰 affair。"""
    wake_at = "2026-07-03T21:40:00+08:00"
    rest_result = {"name": "rest", "arguments": {}, "result": json.dumps({
        "__l4_block__": True,
        "started": True,
        "affair_id": None,        # 兜底路径
        "wake_at": wake_at,
    })}
    agent = _make_bare_agent()

    cancelled = []
    affair_updates = []
    with patch("domain.lifecycle.alarms.list_pending_alarms",
               return_value=[{"id": 7, "event_kind": "timer", "fire_at": wake_at}]), \
         patch("domain.lifecycle.alarms.cancel_alarm", side_effect=cancelled.append), \
         patch("domain.lifecycle.affairs.runtime.update_affair",
               side_effect=lambda *a, **k: affair_updates.append(a)), \
         patch("domain.lifecycle.affairs.runtime.clear_wait_intent", side_effect=lambda *a: None):
        ok = agent._revoke_rest_and_resume(rest_result)

    assert ok is True
    assert cancelled == [7]
    assert affair_updates == []                   # affair_id=None 不动 affair


# ── JSON 契约：rest sentinel 必须携带 revoke 依赖的字段 ──────────────────
# 万一未来有人改 rest() 的返回字段删了 wake_at/affair_id，revoke 会静默失效。
# 这个测试锁住这个契约。


def test_rest_until_sentinel_has_revoke_fields():
    """rest(until=...) 返回值必须含 affair_id + wake_at（revoke 依赖）。"""
    from interfaces.tools.action_tools import _handle_rest, _BLOCK_SENTINEL

    # 用 monkeypatch 让 rest 走 until 路径而不真正 set_alarm
    with patch("domain.lifecycle.alarms.set_alarm"), \
         patch("domain.lifecycle.alarms.list_pending_alarms", return_value=[]), \
         patch("interfaces.tools.action_tools.get_current_affair", return_value="aff-x", create=True), \
         patch("domain.lifecycle.affairs.runtime.get_affair", return_value=object()), \
         patch("domain.lifecycle.affairs.runtime.update_affair"), \
         patch("domain.lifecycle.affairs.runtime.set_wait_intent"):
        result = _handle_rest({
            "until": "2026-07-15T15:00:00+08:00",
            "reason": "test",
            "mental_context": "",
        })

    data = json.loads(result)
    assert data.get(_BLOCK_SENTINEL) is True
    assert "affair_id" in data                    # 必须有
    assert "wake_at" in data                      # 必须有
