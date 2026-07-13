"""Regression tests for state.db integrity self-check (改动 B).

背景：原 ``init_all_schemas`` 启动时完全不自检 state.db。当主库损坏时，
所有 EMIT 失败但 handler 用 ``event_id=-1`` 当成 "FAILED-or-merged"
静默吞掉 → 实例变植物人但 supervisor 看不出来（本次故障的精确现象）。

改动 B：``init_all_schemas`` 开头跑一次 ``PRAGMA integrity_check``，
损坏时抛 ``StateDbCorruptError``，由 supervisor 决定后续。

注意：测试不依赖 instance context/环境变量，而是直接通过
``_check_state_db_integrity(db_path=...)`` 注入路径——这样避免污染
``apps/`` 目录或与别的测试串扰。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


def _write_corrupt_db(path: Path) -> None:
    """造一个有效的 SQLite 文件，再用字节覆盖中间 page 制造 b-tree 损坏。

    模拟现实触发条件：「Tree page cell: Rowid out of order」类故障。
    """
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA page_size=4096;")
    conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, x TEXT);")
    conn.executemany(
        "INSERT INTO events (x) VALUES (?);",
        [(f"row-{i}",) for i in range(2000)],
    )
    conn.commit()
    conn.close()

    raw = path.read_bytes()
    # 覆盖中间若干 page 的内容（保留头尾以确保 header 仍识别为 sqlite）
    mid = len(raw) // 2
    corrupted = raw[: mid - 4096] + (b"\xff" * 4096) + raw[mid:]
    path.write_bytes(corrupted)


def _db_is_corrupted(path: Path) -> bool:
    """确认 fixture 真的损坏了（任一变体都算）。

    SQLite 损坏有两种已观察到的变体：
      1) integrity_check 仍能执行但返回非 'ok'（如 'Tree ... Rowid out of order'）；
      2) 损坏到无法执行 integrity_check，sqlite 直接抛 DatabaseError(11)。
    """
    try:
        conn = sqlite3.connect(str(path))
        result = conn.execute("PRAGMA integrity_check;").fetchone()[0]
        conn.close()
        return result != "ok"
    except sqlite3.DatabaseError:
        return True


def test_check_integrity_raises_on_corrupt_db(tmp_path: Path) -> None:
    """state.db 损坏时 _check_state_db_integrity 必须抛 StateDbCorruptError。"""
    from domain.lifecycle.schema import StateDbCorruptError, _check_state_db_integrity

    db_path = tmp_path / "state.db"
    _write_corrupt_db(db_path)
    assert _db_is_corrupted(db_path), "test fixture 未损坏，测试无效"

    with pytest.raises(StateDbCorruptError):
        _check_state_db_integrity(db_path=db_path)


def test_check_integrity_skips_when_db_missing(tmp_path: Path) -> None:
    """新实例（state.db 不存在时）不应触发自检抛错。"""
    from domain.lifecycle.schema import _check_state_db_integrity

    db_path = tmp_path / "state.db"
    assert not db_path.exists()
    _check_state_db_integrity(db_path=db_path)  # 无异常即通过


def test_check_integrity_passes_on_healthy_db(tmp_path: Path) -> None:
    """state.db 正常时自检应静默通过。"""
    from domain.lifecycle.schema import _check_state_db_integrity

    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY);")
    conn.commit()
    conn.close()

    _check_state_db_integrity(db_path=db_path)  # 无异常即通过
