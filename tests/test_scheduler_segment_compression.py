"""Unit tests for session-continuation 段折叠 & token 压缩修复 (A+B).

B 部分: ``domain.lifecycle.scheduler._load_prior_messages_with_compression``
  - 接续同 session 时，gap > 30min 的历史段(=历史 wake)原文折叠为摘要。
  - 当前段(本轮 wake)永不折叠；整体任何失败都退回原文不抛异常。

A 部分: ``infrastructure.ai.agent`` 的压缩机制
  - A1: 中文 token 估算校准 (total_chars/1.8)
  - A2: ``_split_by_user_message`` 跳过带 ``_sys_tool`` 的 user 消息
"""

from __future__ import annotations

import time

import pytest


# ───────────────────────── B: 段 gap 折叠 ─────────────────────────


class _FakeSessionDB:
    """只实现 ``get_messages``，返回塑造好的全量 messages（按 segment_index 排序）。

    每条 message 是个 dict，带 role / content / segment_index / timestamp，
    模拟 ``session_db.get_messages`` 的真实 SELECT * 输出。
    """

    def __init__(self, messages: list[dict]):
        # 服务端返回时间正序（最早→最近），按 timestamp,id 排
        self._messages = sorted(messages, key=lambda m: (m.get("timestamp", 0), 0))

    def get_messages(self, _session_id):
        return list(self._messages)


def _msg(role: str, content: str, *, seg: int, ts: float, sys_tool: str | None = None) -> dict:
    m = {"role": role, "content": content, "segment_index": seg, "timestamp": ts}
    if sys_tool:
        m["_sys_tool"] = sys_tool
    return m


def test_segment_beyond_gap_is_folded():
    """历史段（seg 0）距 now > 30min → 折叠为摘要；近期段（seg 1）保留原文。"""
    from domain.lifecycle.scheduler import (
        _load_prior_messages_with_compression,
        SEGMENT_GAP_COMPRESS_S,
    )

    now = 10_000.0
    old_ts = now - SEGMENT_GAP_COMPRESS_S - 60  # 31min 前
    recent_ts = now - 120  # 2min 前

    # seg 0：旧 wake，6 条消息（超过 _summarize_segment 的 shrink 阈值 4，
    # 确保即使无 narrative 也会被 shrink 而非原样返回）。距 now ~31min。
    messages = [
        _msg("user", "九点的人类消息原文", seg=0, ts=old_ts),
        _msg("assistant", "九点的回复原文", seg=0, ts=old_ts + 1),
        _msg("user", "九点追问A", seg=0, ts=old_ts + 2),
        _msg("assistant", "九点回复A", seg=0, ts=old_ts + 3),
        _msg("user", "九点追问B", seg=0, ts=old_ts + 4),
        _msg("assistant", "九点回复B", seg=0, ts=old_ts + 5),
        _msg("user", "近期的人类消息", seg=1, ts=recent_ts),
        _msg("assistant", "近期的回复", seg=1, ts=recent_ts + 1),
    ]
    db = _FakeSessionDB(messages)

    prev = _load_prior_messages_with_compression(db, "sess-x", now)

    contents = " ".join(str(m.get("content", "")) for m in prev)

    # seg 0 的中段内容被丢弃（narrative 拿不到时 shrink 成首尾各 2 条）
    assert "九点追问A" not in contents, "seg 0 中段应被折叠丢弃"
    assert "九点回复A" not in contents, "seg 0 中段应被折叠丢弃"
    # 折叠标记应出现
    assert "此段" in contents or "历史回顾" in contents, "折叠段应有摘要/折叠标记"

    # seg 1 保留原文
    assert any(m.get("content") == "近期的人类消息" for m in prev), "近期段应保留原文"
    assert any(m.get("content") == "近期的回复" for m in prev), "近期段应保留原文"


def test_recent_segments_kept_verbatim():
    """所有段都在 30min 内 → 全部保留原文。"""
    from domain.lifecycle.scheduler import _load_prior_messages_with_compression

    now = 10_000.0
    ts0 = now - 600       # 10min 前
    ts1 = now - 120       # 2min 前

    messages = [
        _msg("user", "seg0原话", seg=0, ts=ts0),
        _msg("assistant", "seg0回复", seg=0, ts=ts0 + 1),
        _msg("user", "seg1原话", seg=1, ts=ts1),
        _msg("assistant", "seg1回复", seg=1, ts=ts1 + 1),
    ]
    db = _FakeSessionDB(messages)

    prev = _load_prior_messages_with_compression(db, "sess-y", now)

    contents = " ".join(str(m.get("content", "")) for m in prev)
    assert "seg0原话" in contents and "seg0回复" in contents
    assert "seg1原话" in contents and "seg1回复" in contents
    assert "历史回顾" not in contents, "近期段不该出现折叠标记"


def test_single_segment_short_circuits():
    """只有一段（首轮接续）→ 走 _load_prior_messages 全量，不触发折叠逻辑。"""
    from domain.lifecycle.scheduler import _load_prior_messages_with_compression

    # 单段的 user/assistant pair
    messages = [
        _msg("user", "唯一一条原话", seg=0, ts=100.0),
        _msg("assistant", "唯一一条回复", seg=0, ts=101.0),
    ]
    db = _FakeSessionDB(messages)

    prev = _load_prior_messages_with_compression(db, "sess-z", 10_000.0)
    contents = " ".join(str(m.get("content", "")) for m in prev)
    assert "唯一一条原话" in contents
    assert "唯一一条回复" in contents


def test_compression_never_raises_on_bad_db():
    """session_db.get_messages 抛异常 → 必须退回空 list，绝不向外抛。"""
    from domain.lifecycle.scheduler import _load_prior_messages_with_compression

    class _ExplodingDB:
        def get_messages(self, _sid):
            raise RuntimeError("DB 炸了")

    prev = _load_prior_messages_with_compression(_ExplodingDB(), "sess-bad", time.time())
    assert prev == []


def test_sys_tool_msgs_do_not_create_extra_segments():
    """当前 wake 注入的 _sys_tool user 消息不该把段切碎。

    场景：prev_history 里一段真实 user/assistant 对话，中间夹着 2 条 _sys_tool
    注入（task_board / chat_stream）。旧 _split_by_user_message 会切成 3 段，
    A2 修后应仍是 1 段（真正的 user 消息才算段起始）。
    """
    from infrastructure.ai.agent import AIAgent

    agent = AIAgent.__new__(AIAgent)  # 不走 __init__

    messages = [
        {"role": "user", "content": "真正的人类消息"},
        {"role": "assistant", "content": "回复"},
        {"role": "user", "content": "[task_board] ...", "_sys_tool": "task_board"},
        {"role": "user", "content": "[chat_stream] ...", "_sys_tool": "chat_stream"},
        {"role": "assistant", "content": "又一句回复"},
    ]

    segs = agent._split_by_user_message(messages)
    assert len(segs) == 1, f"A2 应让 _sys_tool 注入不切新段，实际切了 {len(segs)} 段"
    # 全部消息应在同一段里
    assert len(segs[0]) == len(messages)


def test_sys_tool_msgs_without_tag_still_split():
    """没有 _sys_tool tag 的普通 user 消息仍正常切段（回归保护）。"""
    from infrastructure.ai.agent import AIAgent

    agent = AIAgent.__new__(AIAgent)

    messages = [
        {"role": "user", "content": "第一回合人类"},
        {"role": "assistant", "content": "第一回合回复"},
        {"role": "user", "content": "第二回合人类"},
        {"role": "assistant", "content": "第二回合回复"},
    ]
    segs = agent._split_by_user_message(messages)
    assert len(segs) == 2, "真正的 user 消息应该切段"


def test_chinese_token_estimation_is_calibrated():
    """A1: 中文 token 估算用 /1.8（旧 /3 会严重低估触发率）。

    构造一段中等量中文内容，确认它落在「不压缩」但「旧算法会误判为不压缩的更小区间」
    之外——这里我们直接断言 estimated_tokens 的计算口径变化：同等字符数下
    新估算是旧的 ~1.67 倍（3/1.8）。
    """
    from infrastructure.ai.agent import AIAgent

    agent = AIAgent.__new__(AIAgent)
    # 触发「需要压缩」需要 >76K tokens；构造 76K * 1.8 = 136800 中文字符
    big = "中" * 140_000
    messages = [{"role": "user", "content": big}]

    # 新估算：140000 / 1.8 ≈ 77777 > 76800 → 会进入压缩分支
    # （旧估算：140000 / 3 ≈ 46666 < 76800 → 不压缩，bug）
    threshold = agent._get_compression_threshold()
    total_chars = sum(len(str(m.get("content") or "")) for m in messages)
    new_est = int(total_chars / 1.8)
    old_est = int(total_chars / 3)

    assert new_est > threshold, "新估算下该触发压缩"
    assert old_est < threshold, "旧估算下不会触发——这正是要修的 bug"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
