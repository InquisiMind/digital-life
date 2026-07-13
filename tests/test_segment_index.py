"""段号语义重构（segment_index == wake 序号）的回归测试。

历史 bug：SessionDB 的 _current_segment 在 4 处被不一致地读写，导致"读后写"的
所有消息都落到 segment_index = -1、user 全落在段 0。表现：把这种错位历史
喂给 LLM 时（_load_prior_messages_with_compression 按数字序排段），所有
回答被排到所有提问之前——因果错乱。

修复后契约：
  1. 每个 wake 是一段，段号单调递增、永不回退
  2. 同一 wake 内 user/assistant/tool/sys_tool 注入共享同一段号
  3. create_session 续接（INSERT OR IGNORE）不重置 _current_segment
  4. get_messages 中途被调用后再 append 不会污染段位（关键回归）
  5. _restore_segment_index 把 _current_segment 恢复到 DB 里 MAX(seg)，
     下一个 advance_segment → MAX+1，新人接续自然续号
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def session_db(tmp_path: Path):
    """每个测试用独立的 tmp state.db。"""
    from infrastructure.ai.session_db import SessionDB
    db = SessionDB(tmp_path / "state.db")
    yield db
    db._conn.close()


SID = "tx_test_session"


# ─────────────────────────────────────────────────────────────────────
# 1. 单 wake 段内统一段号
# ─────────────────────────────────────────────────────────────────────


def test_advance_segment_then_append_all_share_same_segment(session_db):
    """一个 wake 内所有消息共享同一段号（核心契约）。"""
    session_db.create_session(SID, source="test")
    session_db.advance_segment(SID)  # wake 开始
    session_db.append_message(SID, "user", "alpha 报告候选池")
    session_db.append_message(SID, "assistant", "好，看一下")
    session_db.append_message(SID, "tool", '{"result":"ok"}', tool_name="sense_event_detail")

    msgs = session_db.get_messages(SID)
    user_seg = [m["segment_index"] for m in msgs if m["role"] == "user"]
    asst_seg = [m["segment_index"] for m in msgs if m["role"] == "assistant"]
    tool_seg = [m["segment_index"] for m in msgs if m["role"] == "tool"]
    assert user_seg == [1], f"user 应在 segment 1，实际 {user_seg}"
    assert asst_seg == [1], f"assistant 应在 segment 1，实际 {asst_seg}"
    assert tool_seg == [1], f"tool 应在 segment 1，实际 {tool_seg}"


def test_segments_are_monotonic_across_wakes(session_db):
    """两个 wake → 段号 1, 2 单调递增。"""
    session_db.create_session(SID, source="test")

    # wake 1
    session_db.advance_segment(SID)
    session_db.append_message(SID, "user", "wake 1 prompt")
    session_db.append_message(SID, "assistant", "wake 1 response")
    # wake 2
    session_db.advance_segment(SID)
    session_db.append_message(SID, "user", "wake 2 prompt")
    session_db.append_message(SID, "assistant", "wake 2 response")

    msgs = session_db.get_messages(SID)
    wake1 = [m for m in msgs if "wake 1" in (m.get("content") or "")]
    wake2 = [m for m in msgs if "wake 2" in (m.get("content") or "")]
    assert all(m["segment_index"] == 1 for m in wake1), "wake 1 必须都是 segment 1"
    assert all(m["segment_index"] == 2 for m in wake2), "wake 2 必须都是 segment 2"


# ─────────────────────────────────────────────────────────────────────
# 2. 续接 session 不重置段号
# ─────────────────────────────────────────────────────────────────────


def test_create_session_idempotent_does_not_reset_segment(tmp_path):
    """create_session 二次调用（INSERT OR IGNORE）不应重置 _current_segment。

    这是续接 wake 的关键场景：is_continuation=True 的 wake 不会重建，
    create_session 看见 session 已存在必须保持当前段号。
    """
    from infrastructure.ai.session_db import SessionDB

    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session(SID, source="test")
        db.advance_segment(SID)
        db.append_message(SID, "user", "wake 1")
        assert db._current_segment[SID] == 1

        # 模拟 agent.__init__ 续接场景：再次 create_session 同一 SID
        db.create_session(SID, source="test")
        # 不应被重置成 0
        assert db._current_segment[SID] == 1, "续接 create_session 不能重置段号"

        # 后续 wake 继续 advance_segment 应该接到 1 后面 → 2
        db.advance_segment(SID)
        assert db._current_segment[SID] == 2
        db.append_message(SID, "user", "wake 2")
        msgs = db.get_messages(SID)
        wake2 = [m for m in msgs if m.get("content") == "wake 2"]
        assert wake2[0]["segment_index"] == 2
    finally:
        db._conn.close()


# ─────────────────────────────────────────────────────────────────────
# 3. 关键 regression：get_messages 中途调用不污染后续 append
#    （这是历史 bug 的核心特征）
# ─────────────────────────────────────────────────────────────────────


def test_get_messages_midway_does_not_poison_subsequent_append(session_db):
    """读历史后再 append，新消息段号不能落到 -1 或回退。

    历史 bug：get_messages 调 _restore_segment_index 把 _current_segment reset
    到 MAX(seg)。下一个 append_message 在旧算法下走 `current_segment - 1`，
    写出 -1。本测试锁定新算法不会这样。
    """
    session_db.create_session(SID, source="test")
    session_db.advance_segment(SID)
    session_db.append_message(SID, "user", "wake 1 prompt")
    session_db.append_message(SID, "assistant", "wake 1 response")
    # 中途读历史（模拟 _load_prior_messages_with_compression）
    _ = session_db.get_messages(SID)
    # 接着写——新算法应该依然获得正确段号 1
    session_db.append_message(SID, "tool", '{"k":"v"}', tool_name="test_tool")
    msgs = session_db.get_messages(SID)
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    assert tool_msgs, "应该有 tool 行"
    assert tool_msgs[0]["segment_index"] == 1, (
        f"中途 get_messages 后 append 不能落到 -1，实际: {tool_msgs[0]['segment_index']}"
    )


def test_no_negative_segments_after_live_simulated_load(session_db):
    """模拟一段真实 wake（多次 get_messages + append 混合），结果是干净的非负号。"""
    session_db.create_session(SID, source="test")
    session_db.advance_segment(SID)
    for i in range(5):
        session_db.append_message(SID, "user", f"u{i}")
        # 模拟 agent.py:789 的中间读
        _ = session_db.get_messages(SID)
        session_db.append_message(SID, "assistant", f"a{i}")
        _ = session_db.get_messages(SID)
        session_db.append_message(SID, "tool", f"t{i}", tool_name=f"tool{i}")
        _ = session_db.get_messages(SID)

    msgs = session_db.get_messages(SID)
    segs = {m["segment_index"] for m in msgs}
    assert all(s >= 0 for s in segs), f"不应该有负段号，实际: {segs}"
    assert segs == {1}, f"同一个 wake 内所有消息都应在 segment 1，实际: {segs}"


# ─────────────────────────────────────────────────────────────────────
# 4. store_tool_call_summary 与 append_message 同一段号
# ─────────────────────────────────────────────────────────────────────


def test_store_tool_call_summary_shares_segment_with_append(session_db):
    """sys_tool 慢变量注入（replace_sys_tool_messages）与同 wake 的真实 append 共享段号。"""
    session_db.create_session(SID, source="test")
    session_db.advance_segment(SID)
    session_db.append_message(SID, "user", "wake 1 prompt")

    # 模拟 slow_ctx 注入：构造 sys_tool tool_call pair 并 store_tool_call_summary
    assistant_msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "sys_001",
                "type": "function",
                "function": {"name": "session_digest", "arguments": "{}"},
            }
        ],
    }
    session_db.replace_sys_tool_messages(
        SID, "session_digest", assistant_msg, '{"digest":"latest"}'
    )

    msgs = session_db.get_messages(SID)
    user_seg = next(m["segment_index"] for m in msgs if m["role"] == "user")
    assistant_seg = next(
        m["segment_index"]
        for m in msgs
        if m["role"] == "assistant" and m.get("tool_calls")
    )
    tool_seg = next(
        m["segment_index"]
        for m in msgs
        if m["role"] == "tool" and m.get("tool_name") == "session_digest"
    )
    assert user_seg == assistant_seg == tool_seg == 1, (
        f"sys_tool 注入应与同 wake 真实 append 同段号，"
        f"实际 user={user_seg} asst={assistant_seg} tool={tool_seg}"
    )


# ─────────────────────────────────────────────────────────────────────
# 5. _restore_segment_index 在 advance 后正确递增
# ─────────────────────────────────────────────────────────────────────


def test_restore_then_advance_gives_max_plus_one(session_db):
    """续接 wake：读历史恢复到 MAX(seg) → advance_segment → MAX+1。"""
    session_db.create_session(SID, source="test")
    # wake 1, 2, 3
    for _ in range(3):
        session_db.advance_segment(SID)
        session_db.append_message(SID, "user", "x")
    assert session_db._current_segment[SID] == 3

    # 模拟进程重启——所有 in-memory 状态丢失，但 DB 行还在
    session_db._current_segment.clear()

    # 续接 wake：先读历史（_restore_segment_index 把 current 设到 MAX(seg)=3）
    _ = session_db.get_messages(SID)
    assert session_db._current_segment[SID] == 3, "恢复后应等于 DB 里 MAX(seg)"

    # 然后新 wake advance
    session_db.advance_segment(SID)
    assert session_db._current_segment[SID] == 4, "续接 advance 应到 MAX+1=4"
    session_db.append_message(SID, "user", "wake 4 prompt")
    msgs = session_db.get_messages(SID)
    wake4 = [m for m in msgs if m.get("content") == "wake 4 prompt"]
    assert wake4[0]["segment_index"] == 4


# ─────────────────────────────────────────────────────────────────────
# 6. 迁移脚本纯函数（recompute_segments）
# ─────────────────────────────────────────────────────────────────────


def test_recompute_segments_handles_polluted_data():
    """recompute_segments：按时间自然顺序 + 按 user 切段。"""
    from scripts.fix_segment_index_history import recompute_segments

    # 模拟污染 DB：3 个 wake 共 6 行，但 segment_index 全打错
    rows = [
        {"id": 1, "role": "user", "segment_index": -1, "timestamp": 100.0},
        {"id": 2, "role": "assistant", "segment_index": -1, "timestamp": 101.0},
        {"id": 3, "role": "tool", "segment_index": -1, "timestamp": 102.0},
        {"id": 4, "role": "user", "segment_index": 0, "timestamp": 200.0},
        {"id": 5, "role": "assistant", "segment_index": 0, "timestamp": 201.0},
        {"id": 6, "role": "user", "segment_index": 0, "timestamp": 300.0},
    ]
    out = recompute_segments(rows)
    # 3 个 user → 3 段（段号 1, 2, 3）；assistant/tool 沿用最近 user 的段
    assert out == [1, 1, 1, 2, 2, 3], f"实际: {out}"


def test_fix_session_db_writes_correct_segments(tmp_path):
    """fix_session_db 在被污染的 DB 上 apply 后，messages.segment_index 修正。"""
    import sqlite3
    from scripts.fix_segment_index_history import fix_session_db

    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            session_id TEXT, role TEXT, content TEXT, tool_call_id TEXT,
            tool_calls TEXT, tool_name TEXT, timestamp REAL,
            token_count INTEGER, finish_reason TEXT, reasoning TEXT,
            reasoning_details TEXT, codex_reasoning_items TEXT,
            segment_index INTEGER DEFAULT 0, chat_id TEXT DEFAULT ''
        )"""
    )
    # 故意污染：3 个 wake 的 message，但 segment_index 全错
    conn.executemany(
        "INSERT INTO messages (session_id, role, timestamp, segment_index) VALUES (?, ?, ?, ?)",
        [
            ("tx_test", "user", 100.0, -1),
            ("tx_test", "assistant", 101.0, -1),
            ("tx_test", "tool", 102.0, -1),
            ("tx_test", "user", 200.0, 0),
            ("tx_test", "assistant", 201.0, 0),
            ("tx_test", "user", 300.0, 0),
        ],
    )
    conn.commit()
    conn.close()

    reports = fix_session_db(db_path, apply=True)
    assert len(reports) == 1
    r = reports[0]
    assert r.rows_total == 6
    assert r.rows_changed == 6  # 全部行段号都错了
    assert r.had_bad_segments

    # 读回验证
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = [
        dict(r) for r in conn.execute(
            "SELECT role, segment_index FROM messages WHERE session_id=? ORDER BY timestamp, id",
            ("tx_test",),
        )
    ]
    conn.close()
    segs = [r["segment_index"] for r in rows]
    assert segs == [1, 1, 1, 2, 2, 3], f"修复后段号: {segs}"


def test_fix_session_db_is_idempotent(tmp_path):
    """fix_session_db 跑两次结果一致（幂等）。"""
    import sqlite3
    from scripts.fix_segment_index_history import fix_session_db

    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """CREATE TABLE messages (
            id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,
            content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT,
            timestamp REAL, token_count INTEGER, finish_reason TEXT,
            reasoning TEXT, reasoning_details TEXT, codex_reasoning_items TEXT,
            segment_index INTEGER DEFAULT 0, chat_id TEXT DEFAULT ''
        )"""
    )
    conn.executemany(
        "INSERT INTO messages (session_id, role, timestamp, segment_index) VALUES (?, ?, ?, ?)",
        [
            ("tx_test", "user", 100.0, -1),
            ("tx_test", "assistant", 101.0, -1),
            ("tx_test", "user", 200.0, 0),
        ],
    )
    conn.commit()
    conn.close()

    fix_session_db(db_path, apply=True)
    reports2 = fix_session_db(db_path, apply=True)
    assert reports2[0].rows_changed == 0, "第二次跑应 0 行改动（幂等）"
