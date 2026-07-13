"""账号级 LLM 429 熔断器测试。

覆盖：
  - resolve_retry_after：秒 / HTTP-date / None / 非法 / 越界 → 正确 clamp
  - trip + is_tripped：trip 后读到、过期后自动恢复
  - upsert 保护：长 retry_after 不被短值覆盖，短值被长值覆盖
  - api_key 分区：不同 key 互不影响、相同 key（指纹）共享状态
  - 跨进程语义：两个独立 DB 连接读写，WAL 跨连接可见
  - fail-open：DB 故障时 is_tripped 返回 (False, {}) 不误杀

设计依据见 module docstring of circuit_breaker.py。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

import infrastructure.budget.circuit_breaker as cb


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """每个测试用独立 tmp DB，不污染正式 data/circuit_breaker.db。

    monkeypatch circuit_breaker 内部的 _db_path / _data_dir（不是全局），
    保证并行测试互不串。
    """
    db_file = tmp_path / "cb.db"
    monkeypatch.setattr(cb, "_data_dir", lambda: tmp_path)
    monkeypatch.setattr(cb, "_db_path", lambda: db_file)
    yield


# ── resolve_retry_after ────────────────────────────────────────────────────


def test_resolve_retry_after_none_returns_default():
    assert cb.resolve_retry_after(None) == cb.DEFAULT_RETRY_AFTER_SEC


def test_resolve_retry_after_empty_string_returns_default():
    assert cb.resolve_retry_after("") == cb.DEFAULT_RETRY_AFTER_SEC
    assert cb.resolve_retry_after("   ") == cb.DEFAULT_RETRY_AFTER_SEC


def test_resolve_retry_after_integer_seconds():
    """'120' → 120.0（整数秒是最常见格式）。"""
    assert cb.resolve_retry_after("120") == 120.0


def test_resolve_retry_after_below_min_clipped():
    """'5' 被裁到 MIN_RETRY_AFTER_SEC，防恶意短头。"""
    assert cb.resolve_retry_after("5") == cb.MIN_RETRY_AFTER_SEC


def test_resolve_retry_after_above_max_clipped():
    """超大的秒数裁到 MAX，防离谱值卡死系统。"""
    assert cb.resolve_retry_after("99999") == cb.MAX_RETRY_AFTER_SEC


def test_resolve_retry_after_http_date_future():
    """HTTP-date 格式（RFC 7231）→ 未来时间换算成秒。"""
    future = datetime.now(timezone.utc) + timedelta(seconds=60)
    header = format_datetime(future, usegmt=True)
    secs = cb.resolve_retry_after(header)
    # 给 ±5s 容差（调用与构造之间的流逝）
    assert 55 <= secs <= 65


def test_resolve_retry_after_http_date_past_returns_default():
    """已是过去的 HTTP-date → 用 DEFAULT 而不是 0（过去了就当没头）。"""
    past = datetime.now(timezone.utc) - timedelta(seconds=60)
    header = format_datetime(past, usegmt=True)
    assert cb.resolve_retry_after(header) == cb.DEFAULT_RETRY_AFTER_SEC


def test_resolve_retry_after_garbage_returns_default():
    assert cb.resolve_retry_after("not-a-date-or-number") == cb.DEFAULT_RETRY_AFTER_SEC


# ── trip + is_tripped ───────────────────────────────────────────────────────


def test_tripped_key_is_tripped():
    cb.trip("key-A", retry_after_sec=120, instance_id="inst1")
    tripped, info = cb.is_tripped("key-A")
    assert tripped is True
    assert info["retry_after_sec"] == 120.0
    assert info["reason"] == "429"
    assert info["tripped_by"] == "inst1"
    assert info["expires_at"]  # 非空 ISO 字符串


def test_untripped_key_not_tripped():
    tripped, info = cb.is_tripped("never-tripped-key")
    assert tripped is False
    assert info == {}


def test_different_keys_isolated():
    """按 api_key 分区：不同 key 互不影响。"""
    cb.trip("key-A", retry_after_sec=120, instance_id="inst1")
    tripped_a, _ = cb.is_tripped("key-A")
    tripped_b, _ = cb.is_tripped("key-B")
    assert tripped_a is True
    assert tripped_b is False


def test_same_key_shares_state_different_instances():
    """共用同一把 key 的两个实例共吃一个熔断状态。"""
    cb.trip("shared-key", retry_after_sec=300, instance_id="inst1")
    # inst2 用同一把 key 也应看到熔断
    tripped, info = cb.is_tripped("shared-key")
    assert tripped is True
    assert info["tripped_by"] == "inst1"


def test_expiry_auto_recovers():
    """expires_at <= now → is_tripped 自动删行返回 (False, {})。"""
    cb.trip("key-A", retry_after_sec=120, instance_id="inst1")
    # 手工把 expires_at 推到过去，模拟过期
    conn = sqlite3.connect(str(cb.circuit_breaker_db_path()))
    conn.execute(
        "UPDATE circuit_breaker SET expires_at = ? WHERE api_key_fingerprint = ?",
        ("2020-01-01T00:00:00+00:00", cb._fingerprint("key-A")),
    )
    conn.commit()
    conn.close()

    tripped, info = cb.is_tripped("key-A")
    assert tripped is False
    assert info == {}

    # 行应已被自动删除
    conn = sqlite3.connect(str(cb.circuit_breaker_db_path()))
    count = conn.execute("SELECT COUNT(*) FROM circuit_breaker").fetchone()[0]
    conn.close()
    assert count == 0


# ── upsert 保护 ───────────────────────────────────────────────────────────


def test_short_retry_after_does_not_overwrite_long():
    """关键语义：短退避实例不能把长退避状态覆盖提前恢复。

    场景：A trip 600s，5s 后 B（同 key）trip 30s → 系统应保持 600s 退避。
    """
    cb.trip("key-A", retry_after_sec=600, instance_id="inst1")
    cb.trip("key-A", retry_after_sec=30, instance_id="inst2")
    _, info = cb.is_tripped("key-A")
    assert info["retry_after_sec"] == 600.0


def test_longer_retry_after_overwrites_shorter():
    """反方向：更长的退避应覆盖（更晚恢复更安全）。"""
    cb.trip("key-A", retry_after_sec=60, instance_id="inst1")
    cb.trip("key-A", retry_after_sec=900, instance_id="inst2")
    _, info = cb.is_tripped("key-A")
    assert info["retry_after_sec"] == 900.0


def test_trip_clamps_retry_after_to_max():
    """调用方传超 MAX 的值 → 落库时被裁。"""
    cb.trip("key-A", retry_after_sec=99999, instance_id="inst1")
    _, info = cb.is_tripped("key-A")
    assert info["retry_after_sec"] == cb.MAX_RETRY_AFTER_SEC


# ── clear ──────────────────────────────────────────────────────────────────


def test_clear_removes_trip():
    cb.trip("key-A", retry_after_sec=300, instance_id="inst1")
    deleted = cb.clear("key-A")
    assert deleted is True
    tripped, _ = cb.is_tripped("key-A")
    assert tripped is False


def test_clear_nonexistent_returns_false():
    assert cb.clear("never-tripped") is False


# ── 指纹 ───────────────────────────────────────────────────────────────────


def test_fingerprint_stable_same_key():
    assert cb._fingerprint("key-xyz") == cb._fingerprint("key-xyz")


def test_fingerprint_empty_key():
    assert cb._fingerprint("") == "_no_key"


def test_fingerprint_no_plaintext_in_db():
    """明文 key 不应落库——只有指纹。"""
    cb.trip("secret-key-12345", retry_after_sec=60, instance_id="inst1")
    conn = sqlite3.connect(str(cb.circuit_breaker_db_path()))
    rows = conn.execute(
        "SELECT api_key_fingerprint FROM circuit_breaker"
    ).fetchall()
    conn.close()
    for (fp,) in rows:
        assert "secret-key-12345" not in fp
        assert len(fp) == 16  # 16 位 hex


# ── 跨进程语义 ─────────────────────────────────────────────────────────────


def test_two_connections_wal_visible():
    """WAL 模式：进程 A trip，进程 B（独立连接）应能读到。

    这是熔断跨实例生效的物理基础——每个实例子进程各自开连接读写同一个
    circuit_breaker.db 文件。WAL 保证写不阻塞读、跨连接立即可见。
    """
    cb.trip("key-shared", retry_after_sec=300, instance_id="instA")

    # 用第二个独立连接模拟另一个进程读
    conn2 = sqlite3.connect(str(cb.circuit_breaker_db_path()))
    conn2.row_factory = sqlite3.Row
    row = conn2.execute(
        "SELECT expires_at, retry_after_sec FROM circuit_breaker "
        "WHERE api_key_fingerprint = ?",
        (cb._fingerprint("key-shared"),),
    ).fetchone()
    conn2.close()
    assert row is not None
    assert row["retry_after_sec"] == 300.0


def test_is_tripped_failopen_on_db_error(monkeypatch):
    """DB 故障时 is_tripped 必须 fail-open（返回未熔断），不能误杀所有实例。

    熔断是保护机制，自身故障宁可漏（多打一次 API）也不能让所有 wake 永久卡死。
    """
    cb.trip("key-A", retry_after_sec=300, instance_id="inst1")

    def _boom(_path):
        raise sqlite3.OperationalError("simulated DB failure")

    monkeypatch.setattr(cb._sqlite, "connect", _boom)
    tripped, info = cb.is_tripped("key-A")
    assert tripped is False
    assert info == {}


# ── 启用开关：enabled=False 时 should_block 不挡 ───────────────────────────


def test_is_tripped_respects_empty_key_safe():
    """空 key（配置缺失的边缘情况）调用 is_tripped 不抛错，返回未熔断。"""
    tripped, info = cb.is_tripped("")
    assert tripped is False
    assert info == {}
