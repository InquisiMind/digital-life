"""wake 首装悬空 tool_calls 修复测试（_repair_unanswered_tool_calls）。

2026-10-13 系统性重复事故后重立的安全边界在此固化：
  - 纯增量：只插入合成 tool 行，绝不删除/改写已有消息
  - GLM 常态 assistant 行（reasoning_content 非空 + content 空）原样保留
  - 幂等：补过的列表重跑零插入
"""
from __future__ import annotations

import copy

from infrastructure.ai.agent import (
    _repair_unanswered_tool_calls,
    _sanitize_payload_messages,
)


def _tc(tid: str, name: str = "project_file_read") -> dict:
    return {
        "id": tid,
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }


def _tool_row(tid: str, content: str = "ok") -> dict:
    return {"role": "tool", "tool_call_id": tid, "name": "x", "content": content}


# ── 回归守卫：GLM 常态行绝不被动 ──────────────────────

def test_glm_reasoning_carrier_rows_untouched():
    """GLM 常态：assistant 行 reasoning_content 非空 + content 空、无 tool_calls。
    上一版全局过滤删了这类行 → 模型失忆 → 全系统重复。必须原样保留。"""
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "", "tool_calls": None,
         "reasoning_content": "用户打招呼，我该简短回应"},
        {"role": "assistant", "content": "你好呀", "tool_calls": None},
    ]
    before = copy.deepcopy(messages)
    assert _repair_unanswered_tool_calls(messages) == 0
    assert messages == before


def test_no_deletion_or_mutation_of_existing_rows():
    """悬空场景下已有行也只增不改：assistant 行本体（含 tool_calls/reasoning）原样。"""
    messages = [
        {"role": "user", "content": "读文件"},
        {"role": "assistant", "content": "", "tool_calls": [_tc("call_1")],
         "reasoning_content": "需要先读文件"},
    ]
    n = _repair_unanswered_tool_calls(messages)
    assert n == 1
    assert messages[1]["tool_calls"] == [_tc("call_1")]  # 原行未动
    assert messages[1]["reasoning_content"] == "需要先读文件"
    assert messages[1]["content"] == ""
    assert messages[2]["role"] == "tool" and messages[2]["tool_call_id"] == "call_1"


# ── 修复行为 ────────────────────────────────────────

def test_tail_dangling_gets_placeholder_after_assistant():
    messages = [
        {"role": "user", "content": "读文件"},
        {"role": "assistant", "content": "", "tool_calls": [_tc("call_a")]},
    ]
    assert _repair_unanswered_tool_calls(messages) == 1
    row = messages[2]
    assert row["role"] == "tool" and row["tool_call_id"] == "call_a"
    assert "中断" in row["content"]  # 如实告知，模型可自行重调


def test_partial_answers_completed():
    """assistant 发两个调用，wake 只落了一个结果就中断 → 只补缺的那个。"""
    messages = [
        {"role": "user", "content": "两件事"},
        {"role": "assistant", "content": "", "tool_calls": [_tc("a"), _tc("b")]},
        _tool_row("a"),
    ]
    assert _repair_unanswered_tool_calls(messages) == 1
    assert [m.get("tool_call_id") for m in messages if m["role"] == "tool"] == ["a", "b"]
    assert messages[2] == _tool_row("a")  # 已有结果行原样（合成行插在 assistant 后）


def test_fully_answered_untouched():
    messages = [
        {"role": "user", "content": "读文件"},
        {"role": "assistant", "content": "", "tool_calls": [_tc("a")]},
        _tool_row("a"),
        {"role": "assistant", "content": "内容是……", "tool_calls": None},
    ]
    before = copy.deepcopy(messages)
    assert _repair_unanswered_tool_calls(messages) == 0
    assert messages == before


def test_mid_history_dangling_inserts_before_next_message():
    """悬空不在尾部（后面还有别的消息）→ 合成行插在其 assistant 之后、后续消息之前。"""
    messages = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "", "tool_calls": [_tc("dangling")]},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "done", "tool_calls": None},
    ]
    assert _repair_unanswered_tool_calls(messages) == 1
    assert messages[2]["role"] == "tool" and messages[2]["tool_call_id"] == "dangling"
    assert messages[3]["content"] == "u2"
    assert len(messages) == 5


def test_multiple_dangling_assistants_positions_stable():
    """多处悬空时从后往前插，插入点不漂移。"""
    messages = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "", "tool_calls": [_tc("t1")]},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "", "tool_calls": [_tc("t2"), _tc("t3")]},
    ]
    assert _repair_unanswered_tool_calls(messages) == 3
    # t1 补在 idx1 后，t3/t2 补在 idx3 后（合成行顺序按插入倒序，配对不受影响）
    ids = [m["tool_call_id"] for m in messages if m["role"] == "tool"]
    assert set(ids) == {"t1", "t2", "t3"}
    assert messages[1]["tool_calls"][0]["id"] == "t1"


def test_idempotent():
    messages = [
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "tool_calls": [_tc("x")]},
    ]
    assert _repair_unanswered_tool_calls(messages) == 1
    snapshot = copy.deepcopy(messages)
    assert _repair_unanswered_tool_calls(messages) == 0
    assert messages == snapshot


def test_repair_then_sanitize_leaves_clean_pairing():
    """修复后过 _sanitize_payload_messages（每调用路径的孤儿剔除）不再有任何动作——
    两道工序凑齐配对不变式且互不重复处理。"""
    messages = [
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "tool_calls": [_tc("y")]},
    ]
    _repair_unanswered_tool_calls(messages)
    _sanitize_payload_messages(messages)
    roles = [m["role"] for m in messages]
    # 无孤儿被删（长度不变）、头不变式未被触发（首条非 system 仍是 user）
    assert roles == ["user", "assistant", "tool"]
