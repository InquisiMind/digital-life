"""infrastructure.perception.context 视觉上下文精简器单测（spec US3 / FR-007~FR-009）。

核心断言：
  - 从 audit 取 turn，重命名 reasoning → reasoning_content
  - 剥离非协议字段（id/timestamp/segment_index/...）
  - 只保留最后一条 assistant 的 reasoning_content
  - 过滤 system 注入行（slow_ctx 伪消息），只留对话型
  - 纯只读：不写任何东西
"""
from __future__ import annotations

import pytest

from infrastructure.perception import context


@pytest.fixture
def isolated_audit(monkeypatch, tmp_path):
    """隔离一个实例的 audit DB 到 tmp_path，写入测试用 turn 数据。"""
    iid = "test-perception-ctx"
    from infrastructure.config import set_current_instance_id, reset_current_instance_id

    token = set_current_instance_id(iid)
    monkeypatch.setattr("infrastructure.config.get_project_root", lambda: tmp_path)
    # 清 factory 缓存，确保用新 tmp_path
    import infrastructure.persistence.instance.factory as fac

    fac._BUNDLE_CACHE.pop(iid, None)

    from infrastructure.persistence.instance import get_audit

    audit = get_audit(iid)
    # 初始化表（RuntimeLogDB 建表在 __init__，但 wake/turn 需要先有 wake 行）
    # 通过 create_wake 建一条 wake
    wid = audit.create_wake(
        wake_seq=1,
        session_id="sess-1",
        started_at=1000.0,
        meta={
            "trigger_type": "external",
            "trigger_chat_id": "oc_test",
            "reason": "测试任务背景",
        },
    )

    # 写入若干 turn：system 注入行 + user + assistant(带 reasoning) + tool + assistant(带 reasoning)
    audit.append_turn(wake_id=wid, wake_seq=1, llm_call_seq=0, position_in_call=0,
                      role="system", content="[slow_ctx] 这是内部上下文，不应进视觉上下文")
    audit.append_turn(wake_id=wid, wake_seq=1, llm_call_seq=0, position_in_call=1,
                      role="user", content="帮我看看这个股票")
    audit.append_turn(wake_id=wid, wake_seq=1, llm_call_seq=1, position_in_call=0,
                      role="assistant", content="好的，我先查一下行情",
                      reasoning="用户问股票，应该调 stock_quote")
    audit.append_turn(wake_id=wid, wake_seq=1, llm_call_seq=2, position_in_call=0,
                      role="tool", tool_name="stock_quote", tool_call_id="call_1",
                      content='{"price": 10.5}')
    audit.append_turn(wake_id=wid, wake_seq=1, llm_call_seq=3, position_in_call=0,
                      role="assistant", content="现价 10.5 元",
                      reasoning="查到了，回报给用户")

    try:
        yield iid
    finally:
        reset_current_instance_id(token)
        fac._BUNDLE_CACHE.pop(iid, None)


def test_build_slim_context_renames_reasoning(isolated_audit):
    """reasoning 列 → reasoning_content 字段（spec FR-008）。"""
    iid = isolated_audit
    msgs = context.build_slim_context(iid, recent_turns=10)
    assert msgs, "应能取到上下文"
    assistants = [m for m in msgs if m["role"] == "assistant"]
    assert len(assistants) == 2
    # 至少最后一条 assistant 带 reasoning_content
    last = assistants[-1]
    assert "reasoning_content" in last
    assert last["reasoning_content"] == "查到了，回报给用户"


def test_build_slim_context_strips_non_protocol_fields(isolated_audit):
    """剥离 id/timestamp/wake_id 等非协议字段（spec FR-008）。"""
    iid = isolated_audit
    msgs = context.build_slim_context(iid, recent_turns=10)
    forbidden = {"id", "timestamp", "wake_id", "wake_seq", "llm_call_seq",
                 "position_in_call", "segment_index", "chat_id", "session_id",
                 "finish_reason", "token_count", "error", "instance_id"}
    for m in msgs:
        assert not (forbidden & set(m.keys())), f"残留非协议字段: {forbidden & set(m.keys())}"


def test_build_slim_context_keeps_only_last_reasoning(isolated_audit):
    """只保留最后一条 assistant 的 reasoning，更早的摘掉（spec FR-008）。"""
    iid = isolated_audit
    msgs = context.build_slim_context(iid, recent_turns=10)
    assistants = [m for m in msgs if m["role"] == "assistant"]
    # 第一条 assistant 不应有 reasoning_content
    assert "reasoning_content" not in assistants[0], "早期 assistant 的 think 应被摘掉"
    # 最后一条保留
    assert "reasoning_content" in assistants[-1]


def test_build_slim_context_filters_system_rows(isolated_audit):
    """过滤 system 注入行（slow_ctx 伪消息不应进视觉上下文）。"""
    iid = isolated_audit
    msgs = context.build_slim_context(iid, recent_turns=10)
    roles = {m["role"] for m in msgs}
    assert "system" not in roles
    # user/assistant/tool 应都在
    assert "user" in roles
    assert "assistant" in roles
    assert "tool" in roles


def test_build_slim_context_readonly(isolated_audit):
    """纯只读：连续调用两次，audit 数据不变（不产生新写入）。"""
    iid = isolated_audit
    from infrastructure.persistence.instance import get_audit

    audit = get_audit(iid)
    before = audit.list_turns_by_session("sess-1")
    context.build_slim_context(iid, recent_turns=10)
    context.build_slim_context(iid, recent_turns=5)
    after = audit.list_turns_by_session("sess-1")
    assert len(before) == len(after), "build_slim_context 不应写入任何 turn"


def test_build_slim_context_empty_instance():
    """空 instance_id → 返回空列表，不报错。"""
    assert context.build_slim_context("") == []


def test_build_slim_context_unknown_instance():
    """未知实例 → 返回空列表（audit 表为空或不存在）。"""
    msgs = context.build_slim_context("nonexistent-iid-xxxx", recent_turns=5)
    assert msgs == []


def test_wake_meta_snapshot(isolated_audit):
    """wake_meta_snapshot 返回触发原因等精简 meta（只读）。"""
    iid = isolated_audit
    meta = context.wake_meta_snapshot(iid)
    assert meta["trigger_type"] == "external"
    assert meta["trigger_chat_id"] == "oc_test"
    assert "测试任务背景" in meta["reason"]
