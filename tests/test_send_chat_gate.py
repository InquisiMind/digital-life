"""send_chat_message 发送前关卡（2026-10-23 zhp 设计对齐：与 express_to_human
共用 check_before_send 这一道闸）。

目标窗口本 session 没看过 → 拦下并附窗口近期流水（模型看完重发）——保证
开口前上下文里有连贯消息历史，服务型主动汇报不再"初次开场"式盲发。
锁三件事：首发拦截带流水、同 session 重发放行、窗口无历史直接放行。
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture()
def gate_env(tmp_path, monkeypatch):
    monkeypatch.setattr("infrastructure.config.get_project_root", lambda: tmp_path)
    return tmp_path


def _seed_customer_window(sid: str, n: int = 3) -> None:
    """给 sid 的客户窗口种几条历史（服务上下文路由到它自己的 messages.db）。"""
    from business.consulting.social import _service_context
    from domain.messages import record_inbound

    with _service_context(sid):
        for i in range(n):
            record_inbound(
                chat_id="customer:c1", sender_id="cust-1", sender_name="刘明君",
                text=f"历史消息 {i}", msg_id=f"seed-{i}", source="customer",
                sender_kind="human")


def _call_tool(sid: str, text: str, session_id: str = "sess-gate-1") -> dict:
    """服务上下文 + 唤醒 session contextvar（scheduler 同款）下调工具。"""
    from business.consulting.collab_tools import _handle_send_chat_message
    from infrastructure.config import (
        reset_current_instance_id,
        reset_current_session_id,
        set_current_instance_id,
        set_current_session_id,
    )

    t_inst = set_current_instance_id(sid)
    t_sess = set_current_session_id(session_id)
    try:
        return json.loads(
            _handle_send_chat_message({"chat_id": "customer:c1", "text": text}))
    finally:
        reset_current_instance_id(t_inst)
        reset_current_session_id(t_sess)


def test_first_send_blocked_with_window_log(gate_env):
    sid = "svc-gate-a"
    _seed_customer_window(sid)
    out = _call_tool(sid, "刘明君您好，简报 v1", session_id="s-a")
    assert out["sent"] is False
    assert "recent_chat_log" in out
    assert "历史消息" in out["recent_chat_log"]


def test_resend_passes_in_same_session(gate_env):
    sid = "svc-gate-b"
    _seed_customer_window(sid)
    first = _call_tool(sid, "刘明君您好，简报 v1", session_id="s-b")
    assert first["sent"] is False  # 首发被拦（窗口已喂给模型）
    second = _call_tool(sid, "刘明君您好，简报 v1", session_id="s-b")
    assert second.get("ok") is True and second.get("kind") == "customer"


def test_empty_window_passes_through(gate_env):
    out = _call_tool("svc-gate-c", "你好", session_id="s-c")
    assert out.get("ok") is True and out.get("kind") == "customer"


def test_new_session_checks_again(gate_env):
    sid = "svc-gate-d"
    _seed_customer_window(sid)
    assert _call_tool(sid, "一", session_id="s1")["sent"] is False
    assert _call_tool(sid, "二", session_id="s1").get("ok") is True
    again = _call_tool(sid, "三", session_id="s2")  # 新 session 重新看窗口
    assert again["sent"] is False and "recent_chat_log" in again
