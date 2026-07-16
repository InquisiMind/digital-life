"""P2.1 — 联想扩散(spreading activation)。

设计正确语义(对齐用户认知 2026-07-16):
  联想不是「召回结果必带回」, 而是「下次被注意到的概率显著提高」。
  命中一个点 → 它的时序邻居 + 诞生链上游经历 → 进候选池 + spread_boost,
  仍需在终排里被相关性 RRF 主分主导; 没相关支撑的邻居被压到结果底部甚至出局。

调用流程(与 facade.unified_recall 三阶段对应):
  Phase A 召回: pure vector + lexical 出 top-N, 不带任何邻居
  Phase B 扩散: 对 top-N 中每一条, depth=2 BFS 跨:
                ① 时序邻居(同 session_id, mini 段位差 ≤1)
                ② 诞生链上游(derived_from 的源 chunk)
                给每个扩散项 spread_score, 大于 RRF 主分时生效
  Phase C 终排: 候选池 =召回结果 + 扩散结果(去重),统一按
                score = rrf_score + spread_boost + cognition_bias + freshness*ε
                取 top-K。邻居可能因查的 query 不相关而排到 top-K 外。

depth=2 用户明确:邻居的邻居也参与(只一圈,避免雪崩)。
"""
from __future__ import annotations

import logging
from typing import Any

from domain.memory.memory.recall.vector import _get_db

logger = logging.getLogger("domain.memory.recall.unified.spread")

# 调参: 跑起来再调 — 用户反馈 2026-07-16: spread 不应替代主召回,只是微助
# 一开始 0.30/0.15 过重, 抢了真正相关的主召回; 调到 0.10/0.05 让 spread 只在
# 平分场景起决定作用(默认 RRF 主分 0.016 量级,spread 0.10 仍是显著助力但
# 不再压倒相关 0.5 级别的强命中)。继续跑 supervised eval 看效果。
_SPREAD_BOOST = 0.10  # 圈1 直接邻居/spread boost
_SPREAD_BOOST_DEPTH2 = 0.05  # 圈2 邻居的邻居(更弱)
_TEMPORAL_DELTA_TOLERANCE = 1  # 段位差 ≤ 1 的同 session 才算时序邻居


def fetch_temporal_neighbors(chunk_id: int) -> list[dict[str, Any]]:
    """取一个 chunk 的时序连续性邻居(同 session_id 距离 ±1 segment_index)。
    返回 list[{chunk_id, source, text, segment_index}], 不含 chunk_id 自身。
    """
    try:
        db = _get_db()
        try:
            row = db.execute(
                "SELECT session_id, segment_index FROM chunks WHERE id=?",
                (chunk_id,)
            ).fetchone()
            if not row or not row["session_id"]:
                return []
            sid = row["session_id"]
            seg = row["segment_index"]
            if seg is None:
                return []
            # 同 session、segment_index 相邻 ±1
            lower = seg - 1
            upper = seg + 1
            rows = db.execute(
                """SELECT id as chunk_id, source, text, segment_index
                   FROM chunks
                   WHERE session_id=? AND segment_index IS NOT NULL
                     AND id != ? AND segment_index BETWEEN ? AND ?
                   ORDER BY segment_index ASC""",
                (sid, chunk_id, lower, upper)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            db.close()
    except Exception as e:
        logger.debug("fetch_temporal_neighbors(%s) failed: %s", chunk_id, e)
        return []


def fetch_derived_upstream(chunk_id: int) -> list[dict[str, Any]]:
    """取一个 chunk 的诞生链上游(derived_from 里的源 chunk)。
    返回 list[{chunk_id, source, text}]。
    """
    try:
        db = _get_db()
        try:
            row = db.execute(
                "SELECT derived_from FROM chunks WHERE id=?",
                (chunk_id,)
            ).fetchone()
            if not row or not row["derived_from"]:
                return []
            import json
            try:
                ids = json.loads(row["derived_from"])
            except Exception:
                return []
            if not isinstance(ids, list) or not ids:
                return []
            # 拉 ids 对应的 chunks
            placeholders = ",".join("?" * len(ids))
            rows = db.execute(
                f"SELECT id as chunk_id, source, text FROM chunks WHERE id IN ({placeholders})",
                tuple(int(i) for i in ids)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            db.close()
    except Exception as e:
        logger.debug("fetch_derived_upstream(%s) failed: %s", chunk_id, e)
        return []


def spread_to_candidates(
    seed_ids: list[int],
    *,
    depth: int = 2,
) -> list[dict[str, Any]]:
    """对一批 seed chunk_id 做 depth 圈联想扩散。
    返回 list[{chunk_id, source, text, spread_score, spread_origin, spread_depth}]。
    不含 seed 本身(它们已进候选池)。

    每圈 BFS:
      圈1: 时序邻居 + derived 上游 → spread_boost=_SPREAD_BOOST (0.30)
      圈2: 圈1 邻居的邻居 → spread_boost=_SPREAD_BOOST_DEPTH2 (0.15)
    """
    if not seed_ids or depth <= 0:
        return []
    visited: set[int] = set(seed_ids)
    candidates: list[dict[str, Any]] = []
    current_frontier: list[int] = list(seed_ids)

    for d in range(1, depth + 1):
        next_frontier: list[int] = []
        boost = _SPREAD_BOOST if d == 1 else _SPREAD_BOOST_DEPTH2
        for sid in current_frontier:
            # 时序邻居
            for nb in fetch_temporal_neighbors(sid):
                cid = nb["chunk_id"]
                if cid in visited:
                    continue
                visited.add(cid)
                candidates.append({
                    "chunk_id": cid,
                    "source": nb["source"],
                    "text": nb["text"],
                    "spread_score": boost,
                    "spread_origin": sid,
                    "spread_depth": d,
                })
                next_frontier.append(cid)
            # 诞生链
            for up in fetch_derived_upstream(sid):
                cid = up["chunk_id"]
                if cid in visited:
                    continue
                visited.add(cid)
                candidates.append({
                    "chunk_id": cid,
                    "source": up["source"],
                    "text": up["text"],
                    "spread_score": boost,
                    "spread_origin": sid,
                    "spread_depth": d,
                    "spread_kind": "derived",
                })
                next_frontier.append(cid)
        current_frontier = next_frontier
        if not current_frontier:
            break  # 没有下一圈可扩,提前停

    # 合并同 chunk_id(可能被多个 seed 扩到) → 取最大 spread_score
    merged: dict[int, dict[str, Any]] = {}
    for c in candidates:
        cid = c["chunk_id"]
        if cid not in merged or c["spread_score"] > merged[cid]["spread_score"]:
            merged[cid] = c
    return list(merged.values())


__all__ = [
    "fetch_temporal_neighbors",
    "fetch_derived_upstream",
    "spread_to_candidates",
]
