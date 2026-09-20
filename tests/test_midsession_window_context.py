"""窗口上下文注入测试（2026-09-20, todo 9ba5191f）。

mid-session _consume_human_events 渲染 message/group_message 时，
从 conversation_log 拉同窗口最近 5 条（排除当前消息原文）附在
wake_signal 内容后——解决 RUNNING 中收到"重启呗，那就"这类指代
没有会话上文可解的问题。本测试锁定：
  1. message/group_message → 附带上下文段
  2. 其他 kind（timer 等）→ 不附带
  3. read_window_context 异常 → 静默降级，注入不炸
  4. exclude_text/platform 参数正确传递
"""
from __future__ import annotations

import pytest

import infrastructure.ai.agent as agent_mod


class _FakeAgent:
    instance_id = "test-window-ctx"
    session_id = None
    audit_ctx = None
    _effort_state = None

    def _do_consume_events(self, events):
        self.consumed = events

    def _sys_tool_call(self, name, content):
        self.captured_content = content
        return ({"role": "assistant", "tool_calls": []},
                {"role": "tool", "name": name, "content": content})

    def _append_message(self, *a, **k):
        pass


@pytest.fixture()
def capture_ctx(monkeypatch):
    calls = []

    def fake_read(conversation_id, platform="", exclude_text="", limit=5, self_name="我"):
        calls.append(dict(conversation_id=conversation_id, platform=platform,
                          exclude_text=exclude_text, limit=limit))
        return "[09-20 10:02] 张浩普：之前的方案聊到哪了"

    monkeypatch.setattr(
        "domain.lifecycle.conversation_log.read_window_context", fake_read
    )
    return calls


def _msg_event(kind="message", text="重启呗，那就", chat_id="oc_test"):
    return {"event_id": 1, "kind": kind, "display_name": "人类消息",
            "description": "", "payload": {"text": text, "chat_id": chat_id, "platform": "feishu"}}


def test_message_event_includes_window_context(capture_ctx):
    agent = _FakeAgent()
    messages = []
    agent_mod.AIAgent._consume_human_events(agent, [_msg_event()], messages)
    assert "[同窗口最近对话（注入前）]" in agent.captured_content
    assert "之前的方案聊到哪了" in agent.captured_content
    # 参数透传
    assert capture_ctx == [dict(conversation_id="oc_test", platform="feishu",
                                exclude_text="重启呗，那就", limit=5)]


def test_group_message_includes_window_context(capture_ctx):
    agent = _FakeAgent()
    agent_mod.AIAgent._consume_human_events(agent, [_msg_event(kind="group_message")], [])
    assert "[同窗口最近对话（注入前）]" in agent.captured_content


def test_non_message_kind_has_no_window_context(capture_ctx):
    agent = _FakeAgent()
    ev = _msg_event(kind="timer")
    ev["payload"] = {}
    agent_mod.AIAgent._consume_human_events(agent, [ev], [])
    assert "[同窗口最近对话（注入前）]" not in agent.captured_content
    assert capture_ctx == []


def test_read_window_context_failure_degrades_silently(monkeypatch):
    def boom(**k):
        raise RuntimeError("db down")

    monkeypatch.setattr(
        "domain.lifecycle.conversation_log.read_window_context", boom
    )
    agent = _FakeAgent()
    agent_mod.AIAgent._consume_human_events(agent, [_msg_event()], [])
    # 未崩、未附加
    assert "[同窗口最近对话（注入前）]" not in agent.captured_content
    assert "重启呗" in agent.captured_content  # 原始渲染仍在


def test_empty_context_no_section(capture_ctx, monkeypatch):
    monkeypatch.setattr(
        "domain.lifecycle.conversation_log.read_window_context",
        lambda **k: "",
    )
    agent = _FakeAgent()
    agent_mod.AIAgent._consume_human_events(agent, [_msg_event()], [])
    assert "[同窗口最近对话（注入前）]" not in agent.captured_content
