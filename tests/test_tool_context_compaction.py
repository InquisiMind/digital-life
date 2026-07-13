"""Tool 上下文压缩 + 召回机制的测试。

覆盖（设计见 docs / commit）：
  A. _compact_old_tool_messages depth 维度：≤ depth 不动、> depth 才压
  B. 长度过滤：> depth 但 content 短 → 保留（防止压成指针反而变大）
  C. fake 免疫：sys_/narrative_/fake_ 前缀不计入、不压
  D. 召回端到端：落库 → 压缩 payload → recall_tool_result 拿回原文
  E. 阈值可 env 调（DIGITAL_LIFE_TOOL_HISTORY_DEPTH / DIGITAL_LIFE_TOOL_COMPACT_MIN_CHARS）
  F. payload 不动原 list、不动 DB

设计动机：长 ReAct 会话（tx_initiative_0705_1143）一个 wake 内 80+ 轮 tool
累积到 101K。_maybe_compress_messages 只处理 segment 范围，对长单段内累积
治理弱。本层在它之后跑，专门压"够老 + 够大"的真实 tool 行。
"""
from __future__ import annotations

from unittest.mock import Mock

import pytest


# ── 辅助：绕过 __init__ 构造最小 agent 实例 ────────────────────────────────────


def _make_agent():
    """_compact_old_tool_messages 只用实例方法本身，不需要 __init__ 全套依赖。

    用 __new__ 跳过繁重的初始化（DB / provider / config 等），直接拿一个
    可调用方法的实例。read env 走模块内的 os.environ.get，无需 self 状态。
    """
    from infrastructure.ai.agent import AIAgent
    agent = AIAgent.__new__(AIAgent)
    return agent


def _tool_msg(tid: str, content: str, name: str = "execute_code") -> dict:
    return {"role": "tool", "tool_call_id": tid, "name": name, "content": content}


def _assistant_with_tool_calls(tid: str) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": tid, "type": "function",
                        "function": {"name": "execute_code", "arguments": "{}"}}],
    }


# ── A. depth 维度 ────────────────────────────────────────────────────────────


def test_within_depth_not_compacted(monkeypatch):
    """真实 tool 数 ≤ depth → 全部保留，没有任何压缩。"""
    monkeypatch.setenv("DIGITAL_LIFE_TOOL_HISTORY_DEPTH", "8")
    monkeypatch.setenv("DIGITAL_LIFE_TOOL_COMPACT_MIN_CHARS", "100")

    agent = _make_agent()
    msgs = [
        {"role": "user", "content": "hi"},
    ]
    # 5 条真实 tool（< depth=8），每条长内容（不会因长度而保留）
    for i in range(5):
        msgs.append(_assistant_with_tool_calls(f"call_{i}"))
        msgs.append(_tool_msg(f"call_{i}", "x" * 500))

    out = agent._compact_old_tool_messages(msgs)
    # 全部保留：tool 行 content 不变
    tool_out = [m for m in out if m["role"] == "tool"]
    assert len(tool_out) == 5
    assert all(m["content"] == "x" * 500 for m in tool_out)


def test_beyond_depth_oldest_compacted(monkeypatch):
    """真实 tool 数 > depth → 最旧几条 content 被替换为指针。"""
    monkeypatch.setenv("DIGITAL_LIFE_TOOL_HISTORY_DEPTH", "3")
    monkeypatch.setenv("DIGITAL_LIFE_TOOL_COMPACT_MIN_CHARS", "50")

    agent = _make_agent()
    msgs = [{"role": "user", "content": "hi"}]
    # 6 条真实 tool,depth=3 → 最旧 3 条压,最近 3 条保留
    for i in range(6):
        msgs.append(_assistant_with_tool_calls(f"call_{i}"))
        msgs.append(_tool_msg(f"call_{i}", "x" * 200))  # > min_chars=50

    out = agent._compact_old_tool_messages(msgs)
    tool_out = [m for m in out if m["role"] == "tool"]

    # 最近 3 条(call_3/4/5)保留原文
    recent = [m for m in tool_out if m["tool_call_id"] in ("call_3", "call_4", "call_5")]
    assert all(m["content"] == "x" * 200 for m in recent)

    # 最旧 3 条(call_0/1/2)被压
    old = [m for m in tool_out if m["tool_call_id"] in ("call_0", "call_1", "call_2")]
    assert all("{CMP}" in m["content"] for m in old)
    assert all("recall_tool_result" in m["content"] for m in old)
    # 结构（role/tool_call_id/name）保留
    assert all(m["role"] == "tool" and m["name"] == "execute_code" for m in old)


# ── B. 长度过滤（核心 v2 改动）────────────────────────────────────────────


def test_short_result_not_compacted(monkeypatch):
    """> depth 但 content 短（< min_chars）→ 保留原样。

    关键动机：短结果（如 '{"ok": true}'）压成 ~150 字符指针反而变大，失去意义。
    """
    monkeypatch.setenv("DIGITAL_LIFE_TOOL_HISTORY_DEPTH", "2")
    monkeypatch.setenv("DIGITAL_LIFE_TOOL_COMPACT_MIN_CHARS", "100")

    agent = _make_agent()
    msgs = [{"role": "user", "content": "hi"}]
    # 5 条真实 tool,depth=2；前 3 条候选压,但 content 都仅 30 字符
    for i in range(5):
        msgs.append(_assistant_with_tool_calls(f"call_{i}"))
        msgs.append(_tool_msg(f"call_{i}", "y" * 30))  # < min_chars=100

    out = agent._compact_old_tool_messages(msgs)
    tool_out = [m for m in out if m["role"] == "tool"]
    # 全部保留原文
    assert all(m["content"] == "y" * 30 for m in tool_out)


def test_mixed_short_and_long(monkeypatch):
    """一批 > depth 的候选里:短的保留,长的压缩——只动真正有收益的。"""
    monkeypatch.setenv("DIGITAL_LIFE_TOOL_HISTORY_DEPTH", "1")
    monkeypatch.setenv("DIGITAL_LIFE_TOOL_COMPACT_MIN_CHARS", "100")

    agent = _make_agent()
    msgs = [{"role": "user", "content": "hi"}]
    # call_0 短(50) → 候选但不压 | call_1 长(200) → 候选且压 | call_2 最近保留
    msgs.append(_assistant_with_tool_calls("call_0"))
    msgs.append(_tool_msg("call_0", "s" * 50))
    msgs.append(_assistant_with_tool_calls("call_1"))
    msgs.append(_tool_msg("call_1", "L" * 200))
    msgs.append(_assistant_with_tool_calls("call_2"))
    msgs.append(_tool_msg("call_2", "X" * 200))

    out = agent._compact_old_tool_messages(msgs)
    tool_by_id = {m["tool_call_id"]: m for m in out if m["role"] == "tool"}

    assert tool_by_id["call_0"]["content"] == "s" * 50      # 短,保留
    assert "{CMP}" in tool_by_id["call_1"]["content"]  # 长,压
    assert tool_by_id["call_2"]["content"] == "X" * 200     # depth 内,保留


def test_compaction_never_increases_total_chars(monkeypatch):
    """回归守护：压缩后总字符数必须小于压缩前。

    特别针对短结果——若不加 min_chars 过滤,压成指针会反变大,这是反效果。
    """
    monkeypatch.setenv("DIGITAL_LIFE_TOOL_HISTORY_DEPTH", "2")
    monkeypatch.setenv("DIGITAL_LIFE_TOOL_COMPACT_MIN_CHARS", "100")

    agent = _make_agent()
    msgs = [{"role": "user", "content": "hi"}]
    # 包括短结果与长结果混合,depth=2 → 候选压的是 call_0/call_1
    msgs.append(_assistant_with_tool_calls("call_0"))
    msgs.append(_tool_msg("call_0", "s" * 20))   # 短,候选但不动
    msgs.append(_assistant_with_tool_calls("call_1"))
    msgs.append(_tool_msg("call_1", "L" * 500))  # 长,候选且压
    msgs.append(_assistant_with_tool_calls("call_2"))
    msgs.append(_tool_msg("call_2", "X" * 100))  # 最近,不动
    msgs.append(_assistant_with_tool_calls("call_3"))
    msgs.append(_tool_msg("call_3", "Y" * 100))  # 最近,不动

    before = sum(len(str(m.get("content") or "")) for m in msgs)
    out = agent._compact_old_tool_messages(msgs)
    after = sum(len(str(m.get("content") or "")) for m in out)
    assert after < before, f"压缩后总字符({after}) 应小于压缩前({before})"


# ── C. fake 注入免疫 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("prefix", ["sys_", "narrative_", "fake_"])
def test_fake_tool_ids_skipped(prefix, monkeypatch):
    """sys_/narrative_/fake_ 前缀:不计入 depth、不参与压缩。

    这些是注入项(_sys_tool 的 slow_ctx / segment narrative / assembly 审计侧
    fake pair)——本来就在每次 wake 临时拼装,DB 里不存在,召回机制对它们毫无意义。
    """
    monkeypatch.setenv("DIGITAL_LIFE_TOOL_HISTORY_DEPTH", "2")
    monkeypatch.setenv("DIGITAL_LIFE_TOOL_COMPACT_MIN_CHARS", "50")

    agent = _make_agent()
    # depth=2 时,5 条 fake(每条都长)> depth,但全应跳过
    msgs = [{"role": "user", "content": "hi"}]
    for i in range(5):
        tid = f"{prefix}{i:03d}_xxx"
        msgs.append(_assistant_with_tool_calls(tid))
        msgs.append(_tool_msg(tid, "F" * 500, name="session_digest"))

    out = agent._compact_old_tool_messages(msgs)
    tool_out = [m for m in out if m["role"] == "tool"]
    assert len(tool_out) == 5
    assert all(m["content"] == "F" * 500 for m in tool_out)


def test_fake_does_not_count_toward_depth(monkeypatch):
    """fake 行不只跳过压缩,也不计入 depth 计数——不挤占真实 tool 的活跃窗口。

    场景:2 条 fake + 4 条真实,depth=3。若 fake 不计入,真实有 4 条 → 最旧
    1 条压。若错误地把 fake 计入,总数 6-3=3 压,会误伤更多真实 tool。
    """
    monkeypatch.setenv("DIGITAL_LIFE_TOOL_HISTORY_DEPTH", "3")
    monkeypatch.setenv("DIGITAL_LIFE_TOOL_COMPACT_MIN_CHARS", "50")

    agent = _make_agent()
    msgs = [{"role": "user", "content": "hi"}]
    # 2 条 fake
    for i in range(2):
        msgs.append(_assistant_with_tool_calls(f"sys_{i:03d}"))
        msgs.append(_tool_msg(f"sys_{i:03d}", "F" * 500, name="task_board"))
    # 4 条真实
    for i in range(4):
        msgs.append(_assistant_with_tool_calls(f"call_{i}"))
        msgs.append(_tool_msg(f"call_{i}", "R" * 200))

    out = agent._compact_old_tool_messages(msgs)
    tool_out = {m["tool_call_id"]: m for m in out if m["role"] == "tool"}

    # fake 全保留
    assert all(tool_out[f"sys_{i:03d}"]["content"] == "F" * 500 for i in range(2))
    # 真实:depth=3 时最旧 1 条(call_0)压,其它3条(call_1/2/3)保留
    assert "{CMP}" in tool_out["call_0"]["content"]
    assert all(tool_out[f"call_{i}"]["content"] == "R" * 200 for i in (1, 2, 3))


def test_is_fake_marker_takes_precedence(monkeypatch):
    """即使 ID 是 call_ 格式,_is_fake=True 标记强制视为 fake。

    设计:assembly.py / runtime_log.py 审计侧构造的 fake pair 即使复用了
    看似真实的 ID 命名,也明确打了显式 _is_fake=True 标记。主导意图是
    「这是注入项,不要回放为真实调用」——白名单不应越权覆盖。
    """
    monkeypatch.setenv("DIGITAL_LIFE_TOOL_HISTORY_DEPTH", "1")
    monkeypatch.setenv("DIGITAL_LIFE_TOOL_COMPACT_MIN_CHARS", "50")

    agent = _make_agent()
    msgs = [
        {"role": "user", "content": "hi"},
        _assistant_with_tool_calls("call_marked"),
        # _is_fake=True 即使命中 call_ 白名单也不压缩
        {**_tool_msg("call_marked", "FAKE" * 100), "_is_fake": True},
        _assistant_with_tool_calls("call_real"),
        _tool_msg("call_real", "REAL" * 100),
    ]

    out = agent._compact_old_tool_messages(msgs)
    tool = {m["tool_call_id"]: m for m in out if m["role"] == "tool"}

    # call_marked 因 _is_fake 标记免疫 → 内容保留
    assert tool["call_marked"]["content"] == "FAKE" * 100
    # call_real 是深度 1 内最近 → 保留
    assert tool["call_real"]["content"] == "REAL" * 100


def test_chatcmpl_id_is_real(monkeypatch):
    """chatcmpl-xxx 是 OpenAI 原生格式,应算作真实 tool 可被压缩。

    DB 实测 f8f19689 实例有 292 条 chatcmpl- 格式工具结果(某些 provider 选用)。
    白名单必须覆盖这种格式,否则真实调用会被误判为 fake 而不压。
    """
    monkeypatch.setenv("DIGITAL_LIFE_TOOL_HISTORY_DEPTH", "1")
    monkeypatch.setenv("DIGITAL_LIFE_TOOL_COMPACT_MIN_CHARS", "50")

    agent = _make_agent()
    msgs = [
        {"role": "user", "content": "hi"},
        _assistant_with_tool_calls("chatcmpl-abc123"),
        _tool_msg("chatcmpl-abc123", "O" * 200),
        _assistant_with_tool_calls("chatcmpl-def"),
        _tool_msg("chatcmpl-def", "P" * 200),
    ]

    out = agent._compact_old_tool_messages(msgs)
    tool = {m["tool_call_id"]: m for m in out if m["role"] == "tool"}
    # depth=1 → 最旧那条(chatcmpl-abc123)应被压
    assert "{CMP}" in tool["chatcmpl-abc123"]["content"]


def test_unknown_id_format_immune(monkeypatch):
    """未知厂商 ID 格式默认免疫(保守策略)。

    接入新 LLM 厂商时若没把其 tool_call_id 格式加入白名单,真实调用会被
    误判为 fake 而不压——这是显式的"安全偏向",避免误压系统投入。
    接入新厂商时需要同时更新 _REAL_TOOL_ID_PATTERNS。
    """
    monkeypatch.setenv("DIGITAL_LIFE_TOOL_HISTORY_DEPTH", "1")
    monkeypatch.setenv("DIGITAL_LIFE_TOOL_COMPACT_MIN_CHARS", "50")

    agent = _make_agent()
    msgs = [
        {"role": "user", "content": "hi"},
        _assistant_with_tool_calls("claude_toolu_abc"),   # 未注册的厂商格式
        _tool_msg("claude_toolu_abc", "X" * 500),
        _assistant_with_tool_calls("gemini-123"),
        _tool_msg("gemini-123", "Y" * 500),
    ]

    out = agent._compact_old_tool_messages(msgs)
    tool_out = [m for m in out if m["role"] == "tool"]
    # 任一未知格式 → 全部免疫
    assert all("{CMP}" not in m["content"] for m in tool_out)


def test_is_real_tool_call_unit():
    """_is_real_tool_call 直接单测,覆盖四象限判定逻辑。"""
    agent = _make_agent()

    # 真 + 无 fake 标记 → True
    assert agent._is_real_tool_call({"role": "tool", "tool_call_id": "call_x", "content": ""})
    assert agent._is_real_tool_call({"role": "tool", "tool_call_id": "chatcmpl-y", "content": ""})

    # 真 + _is_fake=True → False(标记门强制免疫)
    assert not agent._is_real_tool_call(
        {"role": "tool", "tool_call_id": "call_x", "content": "", "_is_fake": True}
    )

    # 假 ID + 无 fake 标记 → False(白名单门拦截)
    assert not agent._is_real_tool_call({"role": "tool", "tool_call_id": "sys_001", "content": ""})
    assert not agent._is_real_tool_call({"role": "tool", "tool_call_id": "narrative_005", "content": ""})
    assert not agent._is_real_tool_call({"role": "tool", "tool_call_id": "fake_x_y", "content": ""})

    # 空 ID → False(无法判定)
    assert not agent._is_real_tool_call({"role": "tool", "tool_call_id": "", "content": ""})
    assert not agent._is_real_tool_call({"role": "tool", "content": ""})


# ── D. 召回端到端 ────────────────────────────────────────────────────────────


def test_recall_tool_result_end_to_end(tmp_path, monkeypatch):
    """落库 tool 行 → 压缩 payload → recall_tool_result 拿回原文一致。

    模拟真实流程:agent dispatch 时调 _append_message 落库(原文),_compact_
    old_tool_messages 只在 payload 里压缩;LLM 调 recall_tool_result 时查 DB
    拿到的是原始 content。

    recall_tool_result handler 内部用 SessionDB() 无参 → 走 get_runtime_state
    db_path。测试必须 patch 让它指向 tmp_path DB,否则会读到默认实例路径。
    """
    monkeypatch.setenv("DIGITAL_LIFE_TOOL_HISTORY_DEPTH", "1")
    monkeypatch.setenv("DIGITAL_LIFE_TOOL_COMPACT_MIN_CHARS", "50")

    # 关键:patch runtime state db 路径,handler 内部 SessionDB() 即读到 tmp_path。
    # session_db.py 在自己模块作用域 import 了 get_runtime_state_db_path,
    # 所以要 patch 它内部持有的引用,而非 infrastructure.config 的源头。
    state_db_path = tmp_path / "state.db"
    import infrastructure.ai.session_db as session_db_mod
    monkeypatch.setattr(
        session_db_mod,
        "get_runtime_state_db_path",
        lambda: state_db_path,
    )

    # 用独立 tmp DB 避免污染实例
    from infrastructure.ai.session_db import SessionDB
    db = SessionDB(state_db_path)
    sid = "test-recall-session"
    db.create_session(sid, source="test", model="test-model")
    original_content = "RESULT_" + "x" * 300  # 长内容
    db.append_message(
        sid, role="tool", content=original_content,
        tool_name="execute_code", tool_call_id="call_abc123",
    )

    # 1. DB 原文可被 get_tool_message_by_call_id 取回
    row = db.get_tool_message_by_call_id(sid, "call_abc123")
    assert row is not None
    assert row["content"] == original_content
    assert row["tool_name"] == "execute_code"

    # 2. 工具 handler 通过 context 拿 session_id,返回原文
    from interfaces.tools.action_tools import _handle_recall_tool_result
    result_str = _handle_recall_tool_result(
        {"tool_call_id": "call_abc123"},
        session_id=sid,
    )
    import json
    payload = json.loads(result_str)
    assert payload["content"] == original_content
    assert payload["tool_call_id"] == "call_abc123"
    assert payload["tool_name"] == "execute_code"


def test_recall_tool_result_missing_id_returns_error():
    """不存在的 tool_call_id → 友好错误,不抛异常。"""
    from interfaces.tools.action_tools import _handle_recall_tool_result
    result = _handle_recall_tool_result(
        {"tool_call_id": "call_does_not_exist_xyz"},
        session_id="any-session",
    )
    assert "未找到" in result or "error" in result.lower()


def test_recall_tool_result_no_session_context():
    """session 上下文缺失 → 报错而非隐式跨 session 扫描。"""
    from interfaces.tools.action_tools import _handle_recall_tool_result
    result = _handle_recall_tool_result(
        {"tool_call_id": "call_abc"},
        session_id="",
    )
    assert "session 上下文缺失" in result or "error" in result.lower()


def test_recall_tool_result_missing_id_param():
    """没传 tool_call_id → 报错。"""
    from interfaces.tools.action_tools import _handle_recall_tool_result
    result = _handle_recall_tool_result({}, session_id="sid")
    assert "required" in result.lower() or "error" in result.lower()


# ── E. 阈值可 env 调 ──────────────────────────────────────────────────────────


def test_depth_zero_compacts_all(monkeypatch):
    """depth=0 → 所有真实 tool 行都候选(无活跃窗口)。"""
    monkeypatch.setenv("DIGITAL_LIFE_TOOL_HISTORY_DEPTH", "0")
    monkeypatch.setenv("DIGITAL_LIFE_TOOL_COMPACT_MIN_CHARS", "50")

    agent = _make_agent()
    msgs = [{"role": "user", "content": "hi"}]
    for i in range(3):
        msgs.append(_assistant_with_tool_calls(f"call_{i}"))
        msgs.append(_tool_msg(f"call_{i}", "L" * 200))

    out = agent._compact_old_tool_messages(msgs)
    tool_out = [m for m in out if m["role"] == "tool"]
    assert all("{CMP}" in m["content"] for m in tool_out)


def test_min_chars_zero_compacts_all_sizes(monkeypatch):
    """min_chars=0 → 不做长度过滤,所有候选都压(包括空 content)。"""
    monkeypatch.setenv("DIGITAL_LIFE_TOOL_HISTORY_DEPTH", "1")
    monkeypatch.setenv("DIGITAL_LIFE_TOOL_COMPACT_MIN_CHARS", "0")

    agent = _make_agent()
    msgs = [{"role": "user", "content": "hi"}]
    msgs.append(_assistant_with_tool_calls("call_0"))
    msgs.append(_tool_msg("call_0", "short"))   # << min_chars=0,即使短也压
    msgs.append(_assistant_with_tool_calls("call_1"))
    msgs.append(_tool_msg("call_1", "L" * 200))  # 最近,保留

    out = agent._compact_old_tool_messages(msgs)
    tool_out = {m["tool_call_id"]: m for m in out if m["role"] == "tool"}
    assert "{CMP}" in tool_out["call_0"]["content"]


def test_default_values_when_env_unset(monkeypatch):
    """env 未设时,默认 depth=8 / min_chars=100。"""
    monkeypatch.delenv("DIGITAL_LIFE_TOOL_HISTORY_DEPTH", raising=False)
    monkeypatch.delenv("DIGITAL_LIFE_TOOL_COMPACT_MIN_CHARS", raising=False)

    agent = _make_agent()
    assert agent._get_tool_history_depth() == 8
    assert agent._get_tool_compact_min_chars() == 100


# ── F. payload 不动原 list、不动 DB ────────────────────────────────────────


def test_original_list_not_mutated(monkeypatch):
    """_compact_old_tool_messages 不修改入参 list(返回新 list)。

    这是与 _maybe_compress_messages 一致的范式——内存里 messages list 是
    follow-up ReAct turn 的累积,不能被这次 _chat 的压缩破坏。
    """
    monkeypatch.setenv("DIGITAL_LIFE_TOOL_HISTORY_DEPTH", "2")
    monkeypatch.setenv("DIGITAL_LIFE_TOOL_COMPACT_MIN_CHARS", "50")

    agent = _make_agent()
    msgs = [{"role": "user", "content": "hi"}]
    for i in range(4):
        msgs.append(_assistant_with_tool_calls(f"call_{i}"))
        msgs.append(_tool_msg(f"call_{i}", "L" * 200))

    # 深拷贝原 list 作 snapshot
    import copy
    snapshot = copy.deepcopy(msgs)

    out = agent._compact_old_tool_messages(msgs)

    # 入参未变
    assert msgs == snapshot
    # 出参是另一个 list
    assert out is not msgs
    # 出参里有压缩痕迹,证明压缩发生了(out != msgs)
    assert out != snapshot


def test_db_not_mutated_after_compaction(tmp_path, monkeypatch):
    """payload 压缩后,DB messages 表里 content 仍是原文。

    关键约束:压缩只改 _chat 发给 LLM 的 payload,DB 是真相源。下次 wake
    重读 DB,从压缩状态恢复原文,recall_tool_result 也依赖此不变量。
    """
    monkeypatch.setenv("DIGITAL_LIFE_TOOL_HISTORY_DEPTH", "1")
    monkeypatch.setenv("DIGITAL_LIFE_TOOL_COMPACT_MIN_CHARS", "50")

    from infrastructure.ai.session_db import SessionDB
    db = SessionDB(tmp_path / "state.db")
    sid = "test-no-mut"
    db.create_session(sid, source="test", model="m")
    original_0 = "ORIG_0_" + "x" * 200
    original_1 = "ORIG_1_" + "x" * 200
    db.append_message(sid, role="tool", content=original_0,
                      tool_name="execute_code", tool_call_id="call_old")
    db.append_message(sid, role="tool", content=original_1,
                      tool_name="execute_code", tool_call_id="call_recent")

    # 构造含两条 tool 的 list,depth=1 → call_old 候选压、call_recent 保留
    msgs = [
        {"role": "user", "content": "hi"},
        _assistant_with_tool_calls("call_old"),
        _tool_msg("call_old", original_0),
        _assistant_with_tool_calls("call_recent"),
        _tool_msg("call_recent", original_1),
    ]
    agent = _make_agent()
    out = agent._compact_old_tool_messages(msgs)
    # payload 里有压缩痕迹(call_old 压了)
    tool_out = {m["tool_call_id"]: m for m in out if m["role"] == "tool"}
    assert "{CMP}" in tool_out["call_old"]["content"]
    assert tool_out["call_recent"]["content"] == original_1  # 最近保留

    # DB 里两条都仍是原文
    row_old = db.get_tool_message_by_call_id(sid, "call_old")
    row_recent = db.get_tool_message_by_call_id(sid, "call_recent")
    assert row_old["content"] == original_0
    assert row_recent["content"] == original_1


# ── 注册完整性 ─────────────────────────────────────────────────────────────


def test_recall_tool_registered():
    """recall_tool_result 在 module import 时注册成功。"""
    import interfaces.tools.action_tools  # noqa: F401  ensure import side-effects
    from interfaces.tools.registry import registry as global_registry
    defs = global_registry.get_definitions({"recall_tool_result"}, quiet=True)
    assert len(defs) == 1
    schema = defs[0]["function"]
    assert schema["name"] == "recall_tool_result"
    assert "tool_call_id" in schema["parameters"]["properties"]
    assert schema["parameters"]["required"] == ["tool_call_id"]


def test_recall_tool_in_senses_toolset():
    """看板展示用:recall_tool_result 在 senses 工具集清单里。"""
    from interfaces.tools.toolsets import TOOLSETS
    assert "recall_tool_result" in TOOLSETS["senses"]["tools"]


# ── 指针格式紧凑度回归 (防被改回长格式) ──────────────────────────────────────


def test_pointer_format_is_compact():
    """_render_tool_pointer 输出必须紧凑——历史曾因中文前缀 + 60 chars preview +
    末尾重复 tool_call_id 让单条 ~190 chars，跨段 85 条共 16K 字符 / 9K token。

    新格式 ``{CMP} name=X id=Y pv="…" → recall_tool_result(ID)`` 单条 < 200 chars，
    既保留 preview（让模型粗判语义），又省 ~24% 字符 ~2K token / call。
    本测试锁定单条上限和标识符，防被改回旧长格式。
    """
    from infrastructure.ai.agent import AIAgent

    # 极端长 content + 长 tid（call_-xxxxxxxxx 共 21 chars）
    long_content = "x" * 5000
    tid = "call_-7465874546067762793"
    ptr = AIAgent._render_tool_pointer(
        {"content": long_content}, tid, name="terminal"
    )
    assert "{CMP}" in ptr, "指针必须含 {CMP} 紧凑标志"
    assert f"recall_tool_result({tid})" in ptr, "指针必须含 recall_tool_result(ID) 调用提示"
    # 关键硬上限：单条 < 250 chars（旧版 192 + 安全边界）
    # 如果未来谁无意扩大指针（加中文前缀/重复 tid/preview 扩长）这里会立刻 fail。
    assert len(ptr) < 250, (
        f"指针单条已达 {len(ptr)} chars，超出 250 上限 —— 检查是否加了冗余\n  示例: {ptr!r}"
    )


def test_pointer_preserves_semantic_preview():
    """指针的 preview 字段必须含 content 头部信息，让模型不调 recall 就粗判语义。

    例：``{CMP} name=recall_entity id=... pv='{"entity": "模拟炒股"...' →`` 让模型
    看到 pv 知道这条结果讲的是"模拟炒股"实体。如果完全剥掉 preview，模型失去
    上下文判断能力，频繁调 recall 反而拖低效果。
    """
    from infrastructure.ai.agent import AIAgent

    content = '{"entity": "模拟炒股", "profile": {"summary": "3个月目标..."}}'
    ptr = AIAgent._render_tool_pointer(
        {"content": content},
        tid="call_-1",
        name="recall_entity",
    )
    # preview 必须 forward 时露出 entity 名（模型粗读）
    assert "模拟炒股" in ptr or "entity" in ptr, (
        f"preview 应让模型读到语义（entity 名/类型）；实际指针: {ptr!r}"
    )
