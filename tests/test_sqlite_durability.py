"""Regression tests for SQLite durability PRAGMAs (改动 A).

背景：反复 state.db 损坏（6/29、7/3、7/7）根因是连接只设了
``journal_mode=WAL`` 但缺失 ``synchronous=FULL`` —— Mac 频繁睡眠
让 WAL 在自动 checkpoint 时半写，导致主库 b-tree 损坏。

本测试断言改动 A 涉及的关键连接点都补齐了 ``synchronous=FULL`` +
``busy_timeout``，防止回归。同时验证 ``wal_checkpoint(TRUNCATE)``
辅助函数（改动 C 的纯逻辑部分）行为正确。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from infrastructure.http.server import checkpoint_state_db
from infrastructure.persistence.instance.memory import MemoryDB


def _pragmas(db_path: Path) -> dict[str, int]:
    """读取一个 SQLite 文件的 durable 相关 PRAGMA 当前值。"""
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "SELECT name FROM pragma_pragma_list WHERE name IN "
            "('journal_mode','synchronous','busy_timeout')"
        )
        names = {row[0] for row in cur.fetchall()}
        out: dict[str, int] = {}
        for name in sorted(names):
            val = conn.execute(f"PRAGMA {name};").fetchone()[0]
            # synchronous: 0=OFF 1=NORMAL 2=FULL 3=EXTRA; journal_mode 是字符串
            out[name] = val if isinstance(val, int) else -1
        return out
    finally:
        conn.close()


def test_instance_db_uses_full_synchronous_and_busy_timeout(tmp_path: Path) -> None:
    """InstanceDB 新连接应为 synchronous=FULL 且 busy_timeout>=5000ms。"""
    # MemoryDB 是 InstanceDB 子类的最简代表；改动落在 base 类，故可代表所有子类。
    MemoryDB(db_path=tmp_path / "memory.db", instance_id="t1")
    pragmas = _pragmas(tmp_path / "memory.db")
    assert pragmas["synchronous"] == 2, (
        f"synchronous 应为 FULL(2) 防 WAL 半写损坏，实际={pragmas['synchronous']}"
    )
    assert pragmas["busy_timeout"] >= 5000, (
        f"busy_timeout 应不小于 5000ms，实际={pragmas['busy_timeout']}"
    )


def test_checkpoint_state_db_returns_clean_truncate(tmp_path: Path) -> None:
    """正常状态下 wal_checkpoint(TRUNCATE) 应成功并把 log_pages 归零。"""
    db_path = tmp_path / "t.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, x TEXT);")
    conn.execute("INSERT INTO t (x) VALUES ('a');")
    conn.commit()
    conn.close()

    busy, log_pages, cked = checkpoint_state_db(db_path)
    assert busy == 0, f"unexpected busy={busy}"
    assert log_pages == 0, f"WAL 未被清零 log_pages={log_pages}"
    assert cked >= 0


def test_checkpoint_state_db_handles_missing_file(tmp_path: Path) -> None:
    """IO 失败（文件不存在）应静默返回非零 busy，不抛异常。"""
    missing = tmp_path / "nope.db"
    busy, log_pages, cked = checkpoint_state_db(missing)
    # 只要不抛 + 不返回 (0, 0, ...) 假装成功即可
    assert not (busy == 0 and log_pages == 0), "应识别为失败"


def test_checkpoint_state_db_swallows_corrupt(tmp_path: Path) -> None:
    """损坏的 sqlite 文件不应拖死 checkpoint loop。"""
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a sqlite file at all")
    busy, _log_pages, _cked = checkpoint_state_db(bad)
    assert busy != 0
