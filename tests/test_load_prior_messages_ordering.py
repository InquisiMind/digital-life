"""段间按时间排序防御测试。

历史 bug：当 SessionDB 的 segment_index 写入算法出错（曾经 -1 段 + 0 段混合）时，
_load_prior_messages_with_compression 旧逻辑按 sorted(seg_indices) 排段，
导致 -1 < 0 让所有 assistant 被压到所有 user 之前——因果错乱。

修复后：段间排序按"每段最早消息 timeastamp" 而非段号数字序，确保即便写盘
侧再出 bug，读取端仍能正确按真实时间排段。
"""
from __future__ import annotations


class _FakeSessionDB:
    """只实现 get_messages，返回塑造好的全量 messages。

    每条 message 是个 dict，带 role / content / segment_index / timestamp，
    模拟 session_db.get_messages 的真实 SELECT * 输出。
    """

    def __init__(self, messages: list[dict]):
        self._messages = sorted(messages, key=lambda m: (m.get("timestamp", 0), 0))

    def get_messages(self, _session_id):
        return list(self._messages)


def _msg(role: str, content: str, *, seg: int, ts: float) -> dict:
    return {"role": role, "content": content, "segment_index": seg, "timestamp": ts}


# ─────────────────────────────────────────────────────────────────────
# 核心场景：段号乱但时间因果正确 → 输出按真实时间
# ─────────────────────────────────────────────────────────────────────


def test_segments_ordered_by_time_not_by_index_when_indices_messy():
    """段号 -1（确定污染过）和段号 0 混合时，读取端应按真实时间正确排序。

    这是 wake #1923 raw 错乱的复现：alpha 报告（最早）应在前、模型回复（晚）
    应在后；段号是 -1 vs 0 的乱序也挡不住正确排序。
    """
    from domain.lifecycle.scheduler import _load_prior_messages_with_compression

    # 故意制造污染：alpha 报告在 seg=-1（早）、模型回复在 seg=0（晚）
    # 按数字序 sorted([-1, 0]) = [-1, 0]，会先输出报告、再输出回复——
    # 但段排序按数字时，seg=-1 这一段反而被排在 seg=0 之前 = 报告被排到回复之前？
    # 我们要的是"按真实时间"，所以报告（ts 早）必须出现在回复（ts 晚）之前。
    messages = [
        _msg("user", "alpha 报告候选池", seg=-1, ts=100.0),
        _msg("assistant", "@alpha 板块分散度确认 ✅", seg=-1, ts=101.0),
        _msg("tool", '{"sent":true}', seg=-1, ts=102.0),
        # 5 分钟后模型进一步响应（同一对话回合的延续）
        _msg("assistant", "已确认对齐，进入休息", seg=0, ts=400.0),
        _msg("tool", '{"started":true}', seg=0, ts=401.0),
    ]

    fake_db = _FakeSessionDB(messages)
    out = _load_prior_messages_with_compression(fake_db, "sid", 5000.0)

    # 期望：所有内容保留（gap < 30min 不折叠），且按真实时间排序输出
    assert len(out) == 5
    contents = [m["content"] for m in out]
    # alpha 报告（user）必须出现在所有 assistant 回复之前
    user_idx = contents.index("alpha 报告候选池")
    assert all(contents.index(c) > user_idx for c in [
        "@alpha 板块分散度确认 ✅",
        "已确认对齐，进入休息",
    ]), "user 报告必须在回复之前——按时间因果序"


def test_segments_with_negative_then_positive_index_keep_time_order():
    """-1 段是已污染历史数据。读取不应让 -1 排到 0 之前。"""
    from domain.lifecycle.scheduler import _load_prior_messages_with_compression

    messages = [
        # seg=0 段：早期（ts 早）
        _msg("user", "wake-1 早 prompt", seg=0, ts=100.0),
        _msg("assistant", "wake-1 response", seg=0, ts=110.0),
        # seg=-1 段：稍后（ts 晚）— 读后写污染产生
        _msg("user", "wake-2 晚 prompt", seg=-1, ts=200.0),
        _msg("assistant", "wake-2 response", seg=-1, ts=210.0),
    ]
    fake_db = _FakeSessionDB(messages)
    out = _load_prior_messages_with_compression(fake_db, "sid", 5000.0)
    contents = [m["content"] for m in out]
    # wake-1 在 wake-2 之前（按 ts）
    assert contents[0].startswith("wake-1")
    assert contents[-1].startswith("wake-2"), "实际顺序应该是 wake-1 在前、wake-2 在后"


def test_normal_monotonic_segments_still_sorted_correctly():
    """健康数据（段号 != 时间序单调）依然正常工作。"""
    from domain.lifecycle.scheduler import _load_prior_messages_with_compression

    messages = [
        _msg("user", "wake1", seg=1, ts=100.0),
        _msg("assistant", "r1", seg=1, ts=101.0),
        _msg("user", "wake2", seg=2, ts=200.0),
        _msg("assistant", "r2", seg=2, ts=201.0),
        _msg("user", "wake3", seg=3, ts=300.0),
        _msg("assistant", "r3", seg=3, ts=301.0),
    ]
    fake_db = _FakeSessionDB(messages)
    out = _load_prior_messages_with_compression(fake_db, "sid", 5000.0)
    contents = "".join(m["content"] for m in out)
    # 段间是 1→2→3 顺序
    assert contents.index("wake1") < contents.index("wake2") < contents.index("wake3")
    # 段内 user 在 assistant 之前
    for wake_n, resp_n in [("wake1", "r1"), ("wake2", "r2"), ("wake3", "r3")]:
        assert contents.index(wake_n) < contents.index(resp_n)


def test_old_segments_get_folded_but_correctly_positioned(monkeypatch):
    """远段（gap > 30min）会折叠：折叠机制仍依赖真实 SessionDB（_conn），
    这里跳过用 FakeSessionDB 触发，转而只验证——在同一秒内（gap=0）的污染段不
    被错误折叠 + 正确按时间排序。折叠本身的正确性已有 test_scheduler_segment_compression 锁定。
    """
    from domain.lifecycle.scheduler import _load_prior_messages_with_compression

    # 全部在 gap 窗口内（不折叠），但段号混乱
    messages = [
        _msg("user", "wake-1 早", seg=-1, ts=100.0),
        _msg("assistant", "r-1", seg=-1, ts=101.0),
        _msg("user", "wake-2 晚", seg=0, ts=105.0),
        _msg("assistant", "r-2", seg=0, ts=106.0),
    ]
    fake_db = _FakeSessionDB(messages)
    out = _load_prior_messages_with_compression(fake_db, "sid", 200.0)
    contents = [m.get("content") for m in out]
    # 不折叠 → 全部 4 条；按时间排序：wake-1 在 wake-2 之前
    assert len(out) == 4
    assert contents.index("wake-1 早") < contents.index("wake-2 晚")
    for prompt, resp in [("wake-1 早", "r-1"), ("wake-2 晚", "r-2")]:
        assert contents.index(prompt) < contents.index(resp), "段内顺序保留"
