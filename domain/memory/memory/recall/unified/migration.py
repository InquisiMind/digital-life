"""P3 — schema 迁移 + 历史 chunks backfill 入口。

T033 已在 vector/_get_db 幂等 ALTER 添加新列(DEFAULT)。
T034 这里做懒回填:首次访问某个 source 或 phase=='' 时,按 baseline 表
填 phase / source_kind / authority / permanence 等。

幂等: UPDATE WHERE col IS NULL OR col = ''
"""

from __future__ import annotations

import logging

from domain.memory.memory.recall.unified.slice import (
    baselines_for_source,
    register_normalizer,
    Slice,
    update_slice_dynamics,
)
from domain.memory.memory.recall.vector import _get_db

logger = logging.getLogger("domain.memory.recall.unified.migration")

_backfill_done: bool = False  # 单进程一次就够


def backfill_slice_fields_if_needed(*, force: bool = False) -> int:
    """幂等回填历史 chunks 行的 phase / source_kind / authority / permanence 等。
    返回更新的行数。仅一次(force=False 时) — 单进程启动后跑一次。
    """
    global _backfill_done
    if _backfill_done and not force:
        return 0

    try:
        db = _get_db()
    except Exception as e:
        logger.warning("backfill: cannot open db: %s", e)
        return 0

    updated = 0
    try:
        # 用 baselines 表,按 source 分组 UPDATE
        # (按 source 反推比逐行扫表更省,且 baseline 已覆盖现有所有 source)
        from domain.memory.memory.recall.unified.slice import _BASELINES

        for source, baseline in _BASELINES.items():
            cur = db.execute(
                """
                UPDATE chunks
                SET phase = ?, source_kind = ?, authority = ?, permanence = ?,
                    freshness = COALESCE(NULLIF(freshness, ''), 1.0),
                    activation = COALESCE(NULLIF(activation, ''), 0.0),
                    verification = COALESCE(NULLIF(verification, ''), 0.0),
                    derived_from = COALESCE(NULLIF(derived_from, ''), '[]'),
                    entity_links = COALESCE(NULLIF(entity_links, ''), '[]'),
                    attention_tokens = COALESCE(NULLIF(attention_tokens, ''), '[]')
                WHERE source = ?
                  AND (phase = '' OR phase IS NULL OR source_kind = '' OR source_kind IS NULL)
                """,
                (baseline["phase"], baseline["source_kind"],
                 baseline["authority"], baseline["permanence"], source),
            )
            updated += cur.rowcount if cur.rowcount > 0 else 0
        # 兜底:未在 _BASELINES 里的 source 走 experience 默认
        cur = db.execute(
            """
            UPDATE chunks
            SET phase = 'experience', source_kind = COALESCE(NULLIF(source_kind, ''), 'misc'),
                authority = COALESCE(NULLIF(authority, ''), 0.3),
                permanence = COALESCE(NULLIF(permanence, ''), 0.2),
                freshness = COALESCE(NULLIF(freshness, ''), 1.0),
                activation = COALESCE(NULLIF(activation, ''), 0.0),
                verification = COALESCE(NULLIF(verification, ''), 0.0),
                derived_from = COALESCE(NULLIF(derived_from, ''), '[]'),
                entity_links = COALESCE(NULLIF(entity_links, ''), '[]'),
                attention_tokens = COALESCE(NULLIF(attention_tokens, ''), '[]')
            WHERE (phase = '' OR phase IS NULL)
            """,
        )
        updated += cur.rowcount if cur.rowcount > 0 else 0
        db.commit()
        _backfill_done = True
        logger.info("Slice backfill: %d chunk rows updated with baseline values", updated)
        return updated
    except Exception as e:
        logger.warning("backfill_slice_fields failed: %s", e)
        return 0
    finally:
        db.close()


__all__ = ["backfill_slice_fields_if_needed", "register_normalizer"]
