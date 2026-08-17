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
    """_split_monolith_segment_by_gap：锚点切分优先于时间。"""
    seg = [_msg("user", "first", ts=100.0)]
    for i in range(20):
        seg.append(_msg("assistant", f"m{i}", ts=100.0 + i))  # 连续无间隙
    seg.append(_msg("tool", "evt", ts=121.0, tool_name="wake_signal"))
    for i in range(10):
        seg.append(_msg("assistant", f"n{i}", ts=121.0 + i))
    splits = scheduler._split_monolith_segment_by_gap(seg)
    assert len(splits) == 2
    assert len(splits[0]) == 21 and len(splits[1]) == 11
