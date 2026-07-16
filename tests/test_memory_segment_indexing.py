"""P1 — segment narrative 必须被索引 (FR-101 / SC-002)。

驱动问题: `_generate_segment_narratives_worker` 写了 `memory_layers(layer='segment')` 行，
但从不调 `_index_digest_to_vectors(text, "segment", period)`，因此 segment narrative
在检索池里不存在(spec §User Story 1)。同时 `_DYNAMIC_SOURCES` 没有 `digest_segment`
项，就算写了也拿不到 weight/threshold。

要求(P1 验收):
  T007 / SC-002: consolidate 后 chunks 表有一行 source='digest_segment' 且 embedding 非空
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest


def _redirect_instance_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 infrastructure.config.get_instance_dir 重定向到 tmp_path/apps/<id>,
    并重置 vector / consolidation 的 _mem_dir_cache。"""
    instance_id = "test-seg-idx"
    apps_root = tmp_path / "apps"
    apps_root.mkdir(parents=True, exist_ok=True)
    expected_dir = apps_root / instance_id
    (expected_dir / "data" / "memories").mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("DIGITAL_LIFE_INSTANCE_ID", instance_id)
    import infrastructure.config as cfg

    monkeypatch.setattr(
        cfg,
        "get_instance_dir",
        lambda iid=None: expected_dir,
    )
    import domain.memory.memory.recall.vector as vec_mod
    import domain.memory.memory.summaries.consolidation_runtime as cons_mod

    vec_mod._mem_dir_cache = None
    cons_mod._mem_dir_cache = None
    return expected_dir / "data" / "memories"


def test_digest_segment_in_dynamic_sources() -> None:
    """FR-101 prereq: `digest_segment` 必须在 _DYNAMIC_SOURCES 里,
       否则即使写了 source='digest_segment' 的 chunks 也会被 recall() 跳过。"""
    from domain.memory.memory.recall.vector import _DYNAMIC_SOURCES, _ALL_SOURCES

    assert "digest_segment" in _DYNAMIC_SOURCES, (
        "MUST entry `digest_segment` with weight/threshold/decay; "
        "spec §User Story 1 — segment narrative must be retrievable"
    )
    assert "digest_segment" in _ALL_SOURCES
    cfg = _DYNAMIC_SOURCES["digest_segment"]
    assert {"weight", "threshold", "decay_hours"} <= set(cfg.keys())


def test_segment_narrative_is_indexed_after_consolidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SC-002 / T007: consolidate_after_session 后,
       memory_vectors.chunks 表应有 source='digest_segment' 的行。"""
    _redirect_instance_dir(tmp_path, monkeypatch)

    from domain.memory.memory.recall.vector import _get_db_path, _embedding_to_blob, _get_db
    from domain.memory.memory.summaries.consolidation_runtime import (
        _generate_segment_narratives_worker,
    )
    from infrastructure.ai.session_db import SessionDB

    # 准备 memory_layers:一条 segment narrative 行
    layers_db_path = _get_db_path().parent / "memory_layers.db"
    layers_db_path.parent.mkdir(parents=True, exist_ok=True)
    layers_conn = sqlite3.connect(str(layers_db_path))
    # 完整建表(consolidation 模块首次联接会确保这些列,这里直接写全)
    layers_conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS memory_layers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            layer TEXT NOT NULL,
            period TEXT NOT NULL,
            digest TEXT,
            llm_summary TEXT,
            tool_summary TEXT,
            start_time REAL,
            end_time REAL,
            parent_ids TEXT,
            created_at REAL,
            fallback INTEGER DEFAULT 0,
            UNIQUE(layer, period)
        );
        CREATE INDEX IF NOT EXISTS idx_ml_layer ON memory_layers(layer);
        CREATE INDEX IF NOT EXISTS idx_ml_period ON memory_layers(period);
        """
    )
    session_id = "tx_seg_test_0716"
    layers_conn.execute(
        "INSERT OR REPLACE INTO memory_layers (layer, period, llm_summary, created_at) "
        "VALUES (?, ?, ?, ?)",
        ("segment", f"{session_id}#0", "和 ZHP 探讨了 A+ 策略的有效性", 1752600000.0),
    )
    layers_conn.commit()
    layers_conn.close()

    # 初始化 vectors 表(空)
    db = _get_db()
    db.close()

    # SessionDB mock: worker 用 session_db 仅做 SELECT FROM sessions/messages(测试场景无)
    sess_db = SessionDB(tmp_path / "state.db")
    # 不妨模拟"已生成 narrative"——直接调 worker 会触发 _generate_all_segment_narratives
    # 这里改为直接调底层操作:绕过 LLM 调用,直接验证 segment 索引触发

    # Patch:让 _generate_all_segment_narratives 直接报告"已生成"并跳过 LLM;
    # 同时 mock _embed_single 让 _index_digest_to_vectors 在无 real API key 下也能写入。
    fake_embedding = [0.1] * 2048
    with patch(
        "domain.memory.memory.summaries.consolidation_runtime._generate_all_segment_narratives",
        return_value=1,
    ), patch(
        "domain.memory.memory.recall.vector._embed_single",
        return_value=fake_embedding,
    ):
        _generate_segment_narratives_worker(sess_db, session_id, str(layers_db_path))

    sess_db._conn.close() if hasattr(sess_db, "_conn") else None

    # 断言:memory_vectors.db.chunks 有 source='digest_segment' 行
    vec_db = sqlite3.connect(str(_get_db_path()))
    vec_db.row_factory = sqlite3.Row
    rows = vec_db.execute(
        "SELECT source, text, embedding FROM chunks WHERE source='digest_segment'"
    ).fetchall()
    vec_db.close()

    assert len(rows) >= 1, (
        "segment narrative MUST be indexed with source='digest_segment' after worker runs; "
        "this is the FR-101 / SC-002 / spec §User Story 1 fix"
    )
    assert any(r["text"] for r in rows), "text MUST be populated"
