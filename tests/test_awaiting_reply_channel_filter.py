"""awaiting_reply 按通道精确取消测试。

设计意图：实例可能在多个通道（群 A、群 B、私聊 DM）同时等回复。
之前的实现用 cancel_alarms_by_kind("awaiting_reply") 全局清，导致
跨通道误取消。2026-06-16 改按 payload.channel 精确过滤。

核心场景：
  1. 群 A 发消息 → 群 A 有 awaiting_reply
  2. 群 B 发消息 → 群 B 有 awaiting_reply；群 A 的**仍然在**
  3. 收到群 B 的回复 → 群 B awaiting_reply 消失；群 A **仍然在**
  4. 重发群 B → 清旧的群 B + 建新的（计时重置）
  5. Sibling broadcast 通道独立维护
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


def _setup_isolated_db(tmp_path, monkeypatch):
    """隔离 state.db + 让 alarms._conn() 指向它。"""
    db_path = tmp_path / "state.db"
    # 建空 schema 包含 timers 表
    with sqlite3.connect(str(db_path)) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS timers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_kind TEXT NOT NULL,
                fire_at TEXT NOT NULL,
                payload_json TEXT DEFAULT '{}',
                fired_at TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                status TEXT DEFAULT 'pending'
            );
        """)
        conn.commit()

    def _fake_conn():
        c = sqlite3.connect(str(db_path))
        c.row_factory = sqlite3.Row
        return c

    # Patch alarms._conn
    import domain.lifecycle.alarms as alarms_mod
    monkeypatch.setattr(alarms_mod, "_conn", _fake_conn)
    # Patch _now_iso 为固定时间（避免不同时区影响）
    monkeypatch.setattr(alarms_mod, "_now_iso", lambda: "2026-06-16T03:00:00+00:00")

    return db_path


def _set_awaiting_reply(db_path, channel: str, *, fire_at="2026-06-16T03:10:00+00:00"):
    """直接写一条 awaiting_reply timer 到 DB。"""
    import json
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO timers (event_kind, fire_at, payload_json, fired_at) VALUES (?, ?, ?, NULL)",
            ("awaiting_reply", fire_at, json.dumps({
                "channel": channel,
                "last_sent_text": "测试消息",
                "hint": "...",
            })),
        )
        conn.commit()


def _count_unfired_by_channel(db_path, channel: str) -> int:
    """数 awaiting_reply 里某 channel 还有多少未触发。"""
    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute(
            "SELECT payload_json FROM timers WHERE event_kind='awaiting_reply' AND fired_at IS NULL",
        ).fetchall()
    import json
    n = 0
    for r in rows:
        try:
            p = json.loads(r[0])
            if p.get("channel") == channel:
                n += 1
        except Exception:
            continue
    return n


def _count_all_unfired(db_path) -> int:
    with sqlite3.connect(str(db_path)) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM timers WHERE event_kind='awaiting_reply' AND fired_at IS NULL"
        ).fetchone()[0]


# ── Tests ───────────────────────────────────────────────────────────


def test_filter_cancels_only_matching_channel(tmp_path, monkeypatch):
    """群 A、群 B 都有等待，收到群 B 的回复 → 只清群 B。"""
    db_path = _setup_isolated_db(tmp_path, monkeypatch)
    _set_awaiting_reply(db_path, "lark:group:oc_AAAA")
    _set_awaiting_reply(db_path, "lark:group:oc_BBBB")

    from domain.lifecycle.alarms import cancel_alarms_by_filter

    n = cancel_alarms_by_filter(
        "awaiting_reply",
        payload_filter={"channel": "lark:group:oc_BBBB"},
    )

    assert n == 1, f"应取消群 B 1 个，实际 {n}"
    assert _count_unfired_by_channel(db_path, "lark:group:oc_BBBB") == 0
    assert _count_unfired_by_channel(db_path, "lark:group:oc_AAAA") == 1, "群 A 的等待应保留"


def test_filter_no_match_returns_zero(tmp_path, monkeypatch):
    """没有任何等待在通道 C → cancel C 返回 0，不影响他人。"""
    db_path = _setup_isolated_db(tmp_path, monkeypatch)
    _set_awaiting_reply(db_path, "lark:group:oc_AAAA")

    from domain.lifecycle.alarms import cancel_alarms_by_filter

    n = cancel_alarms_by_filter(
        "awaiting_reply",
        payload_filter={"channel": "lark:group:oc_CCCC"},
    )

    assert n == 0
    assert _count_all_unfired(db_path) == 1, "群 A 的等待应保留"


def test_global_cancel_still_works_when_no_filter(tmp_path, monkeypatch):
    """没传 payload_filter 时 fallback 到全局 cancel（兜底场景）。"""
    db_path = _setup_isolated_db(tmp_path, monkeypatch)
    _set_awaiting_reply(db_path, "lark:group:oc_AAAA")
    _set_awaiting_reply(db_path, "lark:group:oc_BBBB")

    from domain.lifecycle.alarms import cancel_alarms_by_filter

    n = cancel_alarms_by_filter("awaiting_reply")

    assert n == 2


def test_snapshot_scenario_channel_independence(tmp_path, monkeypatch):
    """端到端组合场景：
    1. 群 A 发消息 → 群 A 等待在
    2. 群 B 发消息 → 群 B 等待在，群 A 仍在
    3. 收群 B 回复 → 群 B 清，群 A 仍在
    4. 收群 A 回复 → 群 A 清，双方干净
    """
    db_path = _setup_isolated_db(tmp_path, monkeypatch)

    from domain.lifecycle.alarms import cancel_alarms_by_filter

    # 1. 群 A 发消息 → 通道 lark:group:oc_AAAA 设等待
    _set_awaiting_reply(db_path, "lark:group:oc_AAAA")
    assert _count_all_unfired(db_path) == 1

    # 2. 群 B 发消息 → 通道 lark:group:oc_BBBB 设等待
    _set_awaiting_reply(db_path, "lark:group:oc_BBBB")
    assert _count_all_unfired(db_path) == 2
    assert _count_unfired_by_channel(db_path, "lark:group:oc_AAAA") == 1
    assert _count_unfired_by_channel(db_path, "lark:group:oc_BBBB") == 1

    # 3. 收群 B 回复 → 模拟 handler 调 cancel_alarms_by_filter
    n_b = cancel_alarms_by_filter(
        "awaiting_reply",
        payload_filter={"channel": "lark:group:oc_BBBB"},
    )
    assert n_b == 1
    assert _count_unfired_by_channel(db_path, "lark:group:oc_BBBB") == 0
    assert _count_unfired_by_channel(db_path, "lark:group:oc_AAAA") == 1, "群 A 应仍在"

    # 4. 收群 A 回复 → 清
    n_a = cancel_alarms_by_filter(
        "awaiting_reply",
        payload_filter={"channel": "lark:group:oc_AAAA"},
    )
    assert n_a == 1
    assert _count_all_unfired(db_path) == 0, "双方都清干净"


def test_resend_to_same_channel_resets_timer(tmp_path, monkeypatch):
    """同通道重发 → 清旧的 + 建新的（计时重置）。

    express_to_human 实现：先 cancel_alarms_by_filter("awaiting_reply", {channel})
    再 set_alarm 新的。这里直接模拟这两个步骤。
    """
    db_path = _setup_isolated_db(tmp_path, monkeypatch)

    from domain.lifecycle.alarms import cancel_alarms_by_filter, set_alarm

    # 1. 第一次发群 A → 设等待 @ fire_at="2026-06-16T03:10:00+00:00"
    set_alarm(
        "awaiting_reply",
        fire_at="2026-06-16T03:10:00+00:00",
        payload={"channel": "lark:group:oc_AAAA", "last_sent_text": "A1"},
    )
    assert _count_unfired_by_channel(db_path, "lark:group:oc_AAAA") == 1

    # 2. 重发群 A → 清旧 + 设新（fire_at=03:20:00）
    n_cleared = cancel_alarms_by_filter(
        "awaiting_reply",
        payload_filter={"channel": "lark:group:oc_AAAA"},
    )
    assert n_cleared == 1
    set_alarm(
        "awaiting_reply",
        fire_at="2026-06-16T03:20:00+00:00",
        payload={"channel": "lark:group:oc_AAAA", "last_sent_text": "A2"},
    )
    assert _count_unfired_by_channel(db_path, "lark:group:oc_AAAA") == 1, "重发后只剩 1 条（重置）"


def test_sibling_broadcast_channel_isolated(tmp_path, monkeypatch):
    """Sibling broadcast 通道独立维护，跟群-group 不互相清。"""
    db_path = _setup_isolated_db(tmp_path, monkeypatch)
    _set_awaiting_reply(db_path, "lark:group:oc_AAAA")
    _set_awaiting_reply(db_path, "broadcast:5052c33a-e700-44dd-aea3-00e04a661ab1")

    from domain.lifecycle.alarms import cancel_alarms_by_filter

    # Sibling 推过来一条消息 → 取消 broadcast 通道的等待
    n = cancel_alarms_by_filter(
        "awaiting_reply",
        payload_filter={"channel": "broadcast:5052c33a-e700-44dd-aea3-00e04a661ab1"},
    )
    assert n == 1
    assert _count_unfired_by_channel(db_path, "lark:group:oc_AAAA") == 1, "群组等待不能被 sibling 消息清"


def test_dm_vs_group_in_same_chat_id_independent(tmp_path, monkeypatch):
    """同样 oc_xxx 通道，一个是 dm 一个是 group → 各自独立。

    极端边界：DM oc_xxx 跟 Group oc_xxx 不应互相取消。
    （实际飞书 chat_id 不会重叠，但测试覆盖语义。）
    """
    db_path = _setup_isolated_db(tmp_path, monkeypatch)
    _set_awaiting_reply(db_path, "lark:dm:oc_SHARED")
    _set_awaiting_reply(db_path, "lark:group:oc_SHARED")

    from domain.lifecycle.alarms import cancel_alarms_by_filter

    # 入站 DM 该取消 DM 的，不动 Group 的
    n = cancel_alarms_by_filter(
        "awaiting_reply",
        payload_filter={"channel": "lark:dm:oc_SHARED"},
    )
    assert n == 1
    assert _count_unfired_by_channel(db_path, "lark:group:oc_SHARED") == 1
