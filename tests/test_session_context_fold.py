"""接续上下文折叠测试：虚拟切段 + 硬上限 + 历史回顾标记。

场景覆盖（对着 tx_group_message_0817_1616 真实 bug 写）：
  - 单段巨石（段号 bug 遗留）按 wake_signal 虚拟切段后正常折叠
  - 硬上限：超限时从最旧段强制折叠，最后段永不折
  - 兜底路径的段首 wake prompt 必须裹「历史回顾」标记（防误读为新事件）
  - 时间间隙 fallback（无 wake_signal 锚点时）
  - 正常短 session 不折叠（回归）
"""
from __future__ import annotations

from domain.lifecycle import scheduler


def _msg(role, content, *, ts=0.0, tool_name=None):
    return {"role": role, "content": content, "timestamp": ts,
            "tool_name": tool_name, "tool_call_id": None, "tool_calls": None}


class FakeDB:
    def __init__(self, msgs):
        self._msgs = msgs

    def get_messages(self, sid):
        return list(self._msgs)


def test_monolith_split_by_wake_signal():
    """单段巨石按 wake_signal 切段；>30min 的段被折叠。"""
    import time
    now = time.time()
    msgs = []
    # wake1（95 分钟前，已超折叠窗）：prompt + 对话
    msgs.append(_msg("user", "wake1 prompt", ts=now - 95 * 60))
    for i in range(10):
        msgs.append(_msg("assistant", f"a1-{i}", ts=now - 94 * 60))
    # wake2（5 分钟前，保留）：wake_signal 锚点 + 对话
    msgs.append(_msg("tool", "#evt2 新消息到达", ts=now - 5 * 60, tool_name="wake_signal"))
    for i in range(6):
        msgs.append(_msg("assistant", f"a2-{i}", ts=now - 4 * 60))

    out = scheduler._load_prior_messages_with_compression(FakeDB(msgs), "s1", now)
    # wake1 被折叠（摘要或段首尾兜底），wake2 原文保留
    assert any(m.get("content") == "a2-5" for m in out)       # 最新原文在
    # 兜底语义：段首2+段尾2保留，中段折掉——a1-5 在中段，不应存在
    assert not any(m.get("content") == "a1-5" for m in out)
    # 旧 prompt 若被兜底保留必须裹历史回顾标记
    for m in out:
        c = str(m.get("content") or "")
        if "wake1 prompt" in c:
            assert c.startswith("[历史回顾"), "兜底保留的旧 wake prompt 必须裹历史回顾标记"


def test_hard_limit_forces_fold_from_oldest():
    """总量超硬上限：最旧段强制折叠，最后段永不折。

    场景用带 segment_index 的多段数据（= 段号修复后的正常形态）。
    全部段在 30min 窗口内（时间折叠不触发），只有硬上限兜底。
    """
    import time
    now = time.time()
    msgs = []
    # 5 个段 × 50 条 = 250 条（>160 上限）
    for w in range(5):
        seg_idx = w + 1
        msgs.append({**_msg("tool", f"#evt{w}", ts=now - (25 - w) * 60,
                            tool_name="wake_signal"), "segment_index": seg_idx})
        for i in range(50):
            msgs.append({**_msg("assistant", f"w{w}-m{i}", ts=now - (24 - w) * 60),
                         "segment_index": seg_idx})
    out = scheduler._load_prior_messages_with_compression(FakeDB(msgs), "s2", now)
    # 最后一段全部保留
    assert any(m.get("content") == "w4-m49" for m in out)
    # 最旧的 w0/w1 被强制折叠（中段原文不在）
    assert not any(m.get("content") == "w0-m25" for m in out)
    assert not any(m.get("content") == "w1-m25" for m in out)
    # 总量降到上限附近
    assert len(out) < 200


def test_time_gap_fallback_split():
    """无 wake_signal 锚点时按时间间隙（>600s）切段。"""
    import time
    now = time.time()
    msgs = []
    msgs.append(_msg("user", "old wake", ts=now - 90 * 60))
    for i in range(10):
        msgs.append(_msg("assistant", f"old-{i}", ts=now - 89 * 60))
    # 84 分钟间隙 → 切段 → old 段距 now 89min > 30min → 折叠
    msgs.append(_msg("user", "recent wake", ts=now - 5 * 60))
    for i in range(6):
        msgs.append(_msg("assistant", f"new-{i}", ts=now - 4 * 60))
    out = scheduler._load_prior_messages_with_compression(FakeDB(msgs), "s3", now)
    assert any(m.get("content") == "new-5" for m in out)
    # old 段中段折掉（段首尾兜底保留 old-8/old-9 是预期）
    assert not any(m.get("content") == "old-5" for m in out)


def test_short_session_untouched():
    """短 session（少量消息）不过虚拟切段/折叠，全量原文返回。"""
    import time
    now = time.time()
    msgs = [
        _msg("system", "sys", ts=now - 60),
        _msg("user", "q1", ts=now - 50),
        _msg("assistant", "a1", ts=now - 40),
        _msg("tool", "r1", ts=now - 30, tool_name="t"),
        _msg("assistant", "a2", ts=now - 20),
    ]
    out = scheduler._load_prior_messages_with_compression(FakeDB(msgs), "s4", now)
    assert len(out) == 5
    assert any(m.get("content") == "q1" for m in out)


def test_split_function_wake_anchor():
    """_split_monolith_segment_by_gap：锚点切分 + 碎段（≤6 条）并入前段。"""
    seg = [_msg("user", "first", ts=100.0)]
    for i in range(20):
        seg.append(_msg("assistant", f"m{i}", ts=100.0 + i))  # 连续无间隙
    seg.append(_msg("tool", "evt", ts=121.0, tool_name="wake_signal"))
    # 碎段：锚点后只有 2 条 → 并入前段
    for i in range(2):
        seg.append(_msg("assistant", f"n{i}", ts=121.0 + i))
    # 大段：新锚点 + 10 条 → 独立
    seg.append(_msg("tool", "evt2", ts=140.0, tool_name="wake_signal"))
    for i in range(10):
        seg.append(_msg("assistant", f"k{i}", ts=140.0 + i))
    splits = scheduler._split_monolith_segment_by_gap(seg)
    # 碎段被并入 → 只剩 2 个大段
    assert len(splits) == 2
    assert len(splits[0]) == 24  # 21 + 锚点 + 2 碎消息
    assert len(splits[1]) == 11


def test_rest_mental_context_is_primary_digest():
    """段的折叠摘要优先用 rest 的 mental_context（模型自己写的总结）。"""
    import json
    mc = "17:50 语音系统改造完成：①TTS 云扬 ②ASR 讯飞流式。等 zhp 回复方案。"
    seg = [
        _msg("user", "wake prompt", ts=100.0),
        _msg("assistant", "", ts=101.0, tool_name=None),
    ]
    # 模拟 assistant 的 rest 调用（tool_calls JSON 字符串形态）
    seg[1]["tool_calls"] = json.dumps([{
        "id": "call_1", "function": {
            "name": "rest",
            "arguments": json.dumps({"until": "2026-08-18T21:00:00+08:00", "mental_context": mc}),
        }
    }])
    seg.append(_msg("tool", '{"started":true}', ts=102.0, tool_name="rest"))
    out = scheduler._summarize_segment(seg, None, "test-sid", 1)
    recap = [m for m in out if str(m.get("content", "")).startswith("[历史回顾")]
    assert recap, "应产出历史回顾摘要"
    assert mc in recap[0]["content"], "rest mental_context 应成为摘要主体"


def test_rest_digest_takes_latest():
    """段内多次 rest → 取最后一次的 mental_context（最新状态最准）。"""
    import json
    from domain.lifecycle.scheduler import _segment_rest_digest
    seg = [
        {"role": "assistant", "tool_calls": json.dumps([{
            "id": "c1", "function": {"name": "rest",
             "arguments": json.dumps({"mental_context": "第一次总结"})}}]), "timestamp": 1.0},
        {"role": "assistant", "tool_calls": json.dumps([{
            "id": "c2", "function": {"name": "rest",
             "arguments": json.dumps({"mental_context": "第二次总结（最新）"})}}]), "timestamp": 2.0},
    ]
    assert "第二次总结" in _segment_rest_digest(seg)


def test_rest_digest_empty_when_no_rest():
    from domain.lifecycle.scheduler import _segment_rest_digest
    seg = [_msg("user", "q", ts=1.0), _msg("assistant", "a", ts=2.0)]
    assert _segment_rest_digest(seg) == ""


def test_revoked_rest_skipped_in_digest():
    """被 revoke 的 rest 的 mc 跳过（过时状态），取更早的有效 rest。"""
    import json
    from domain.lifecycle.scheduler import _segment_rest_digest
    seg = [
        # 第一次 rest（有效，后来被新事件打断前的工作总结）
        {"role": "assistant", "tool_calls": json.dumps([{
            "id": "call_ok", "function": {"name": "rest",
             "arguments": json.dumps({"mental_context": "完成了 A 任务"})}}]),
         "timestamp": 1.0},
        {"role": "tool", "tool_name": "rest", "tool_call_id": "call_ok",
         "content": '{"__l4_block__": true, "started": true}', "timestamp": 1.1},
        # 第二次 rest（被 revoke——打算休息时被新事件打断）
        {"role": "assistant", "tool_calls": json.dumps([{
            "id": "call_revoked", "function": {"name": "rest",
             "arguments": json.dumps({"mental_context": "打算休息，等 14:20 闹钟"})}}]),
         "timestamp": 2.0},
        {"role": "tool", "tool_name": "rest", "tool_call_id": "call_revoked",
         "content": '{"__l4_block__": true, "__revoked__": true}', "timestamp": 2.1},
        # 打断后继续干活（无第三次 rest）
        {"role": "assistant", "content": "处理了新事件", "timestamp": 2.5},
    ]
    digest = _segment_rest_digest(seg)
    assert digest == "完成了 A 任务", f"应跳过 revoked 取有效 rest，got: {digest!r}"


def test_all_rests_revoked_returns_empty():
    """全部 rest 都被 revoke → 空串（降级到 narrative）。"""
    import json
    from domain.lifecycle.scheduler import _segment_rest_digest
    seg = [
        {"role": "assistant", "tool_calls": json.dumps([{
            "id": "c1", "function": {"name": "rest",
             "arguments": json.dumps({"mental_context": "过时总结"})}}]), "timestamp": 1.0},
        {"role": "tool", "tool_name": "rest", "tool_call_id": "c1",
         "content": '{"__revoked__": true}', "timestamp": 1.1},
    ]
    assert _segment_rest_digest(seg) == ""
