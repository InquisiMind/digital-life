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


def fetch_entity_neighbors(chunk_id: int, limit: int = 10) -> list[dict]:
    """找跟 seed 共享 entity_links 的其它 cognition chunks。

    用法: seed chunk 的 entity_links=["金开新能","止损线"]
    → 返回所有其它 cognition chunk 的 entity_links 含这两个词之一的
    → 形成"同实体 关联认知"的 spread 网络。
    """
    try:
        from domain.memory.memory.recall.vector import _get_db
        import json as _json
        db = _get_db()
        try:
            # 拿 seed 的 entity_links
            seed_row = db.execute(
                "SELECT entity_links FROM chunks WHERE id=?", (chunk_id,)
            ).fetchone()
            if not seed_row or not seed_row[0]:
                return []
            try:
                seed_links = _json.loads(seed_row[0])
            except Exception:
                return []
            if not seed_links:
                return []

            # 查所有 cognition chunks, 过滤出 entity_links 有交集的
            rows = db.execute(
                "SELECT id, source, text, entity_links FROM chunks "
                "WHERE phase='cognition' AND id != ? "
                "AND (cognition_state IS NULL OR cognition_state NOT IN ('replaced','archived')) "
                "AND entity_links != '[]' AND entity_links != ''",
                (chunk_id,),
            ).fetchall()

            neighbors = []
            for r in rows:
                try:
                    r_links = _json.loads(r[3] or "[]")
                except Exception:
                    continue
                overlap = set(seed_links) & set(r_links)
                if overlap:
                    neighbors.append({
                        "chunk_id": r[0],
                        "source": r[1],
                        "text": r[2],
                        "shared_links": list(overlap),
                    })
            neighbors.sort(key=lambda n: len(n["shared_links"]), reverse=True)
            return neighbors[:limit]
        finally:
            db.close()
    except Exception as e:
        logger.debug("fetch_entity_neighbors(%s) failed: %s", chunk_id, e)
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
                    "source": up.get("source", ""),
                    "text": up.get("text", ""),
                    "spread_score": boost,
                    "spread_origin": sid,
                    "spread_depth": d,
                    "spread_kind": "derived",
                })
                next_frontier.append(cid)
            # entity_links 关联 (仅圈1, 不扩散到圈2避免噪音)
            if d == 1:
                for en in fetch_entity_neighbors(sid):
                    cid = en["chunk_id"]
                    if cid in visited:
                        continue
                    visited.add(cid)
                    candidates.append({
                        "chunk_id": cid,
                        "source": en.get("source", ""),
                        "text": en.get("text", ""),
                        "spread_score": _SPREAD_BOOST * 0.8,
                        "spread_origin": sid,
                        "spread_depth": d,
                        "spread_kind": "entity",
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


# ───────── V3 v2 (2026-07-24): 语义圈1扩展 ─────────

# 实测数据 (2026-07-24 c2a5c8e8 实例): 24 active cognition 横截面
#   <0.3     无关占比 19%
#   0.30-0.55 **语义 bridge 占比 47%** — 当前所有路径都召不到
#   >=0.55    已能被直接召回 16%
# 所以 v2 把 circle-2 语义召回阈值定在 0.42: 低于 0.42 是噪声, 0.42-0.55 是高价值 bridge.
_SEMANTIC_NEIGHBOR_THRESHOLD = 0.42
_SEMANTIC_NEIGHBOR_LIMIT = 5  # 每个 circle-1 seed 最多拉 5 个语义邻居, 避免召回爆炸


def fetch_semantic_neighbors(
    seed_id: int, exclude_ids: set[int] | None = None
) -> list[dict[str, Any]]:
    """v2 circle-2: 用 seed chunk 的 text 做向量查询, 找语义相关但无 entity_links 硬交集的 neighbors.

    与 fetch_entity_neighbors 区别:
      entity_neighbors: 字面 entity_links 字符串交集 (集合 in 操作)
      semantic_neighbors: 向量 embedding 相似度 (语义级 bridge)

    阈值放宽到 _SEMANTIC_NEIGHBOR_THRESHOLD (0.42) — 低于直接召回 0.55, 是因为:
      circle-1 是结论性文本, 它的 embedding 与"虽然 cos 低但语义相关"的认知 (如同一标的的
      不同维度记录) 经常落在 0.42-0.55 区间. 见 docs/design/semantic-spread-v2-deferred.md §二.

    返回 list[{chunk_id, source, text, raw_cos, _semantic_seed}].
    """
    try:
        from domain.memory.memory.recall.vector import (
            _get_db, _blob_to_embedding, _embed_single, _cosine_sim,
        )
        db = _get_db()
        try:
            # 读 seed text + embedding (复用已存的)
            seed_row = db.execute(
                "SELECT text, embedding FROM chunks WHERE id=?",
                (seed_id,),
            ).fetchone()
            if not seed_row or not seed_row["embedding"]:
                return []
            seed_text = seed_row["text"] or ""
            if len(seed_text.strip()) < 12:
                return []
            seed_emb = _blob_to_embedding(seed_row["embedding"])
            if not seed_emb:
                # 已存 embedding 失效 (rare: blob 损坏) → fallback 一次 fresh embedding
                seed_emb = _embed_single(seed_text[:512])
                if not seed_emb:
                    return []

            exclude = exclude_ids or set()
            exclude.add(seed_id)

            # 扫所有 active cognition (限 500 行防全表扫)
            rows = db.execute(
                "SELECT id, source, text, embedding FROM chunks "
                "WHERE phase='cognition' "
                "AND (cognition_state IS NULL OR cognition_state NOT IN ('replaced','archived')) "
                "AND embedding IS NOT NULL "
                "LIMIT 500"
            ).fetchall()

            neighbors = []
            for r in rows:
                cid = r["id"]
                if cid in exclude:
                    continue
                r_emb = _blob_to_embedding(r["embedding"])
                if not r_emb:
                    continue
                cos = _cosine_sim(seed_emb, r_emb)
                if cos < _SEMANTIC_NEIGHBOR_THRESHOLD:
                    continue
                neighbors.append({
                    "chunk_id": cid,
                    "source": r["source"],
                    "text": (r["text"] or "")[:200],
                    "raw_cos": round(cos, 4),
                    "_semantic_seed": seed_id,
                })

            # 按 cos 降序取 top-N
            neighbors.sort(key=lambda x: x["raw_cos"], reverse=True)
            return neighbors[:_SEMANTIC_NEIGHBOR_LIMIT]
        finally:
            db.close()
    except Exception as e:
        logger.debug("fetch_semantic_neighbors(%s) failed: %s", seed_id, e)
        return []


def spread_with_semantics(
    seed_ids: list[int],
    *,
    depth: int = 2,
    semantic_top_k: int = 3,
) -> list[dict[str, Any]]:
    """v2 spread: 调用现有 spread_to_candidates + 对 top-K seed 加 semantic_neighbors.

    semantic_top_k: 取 circle-1 中 seed_ids 前 K 条做语义扩展(避免对所有 circle-1 都跑 embedding).
    实际 cost: top-K ≤ 3, 每条扩 ~5 邻居, 总 ~15 候选 / 1 turn.
    """
    if not seed_ids:
        return []

    # 先跑基础 spread (temporal + derived + entity_links)
    base = spread_to_candidates(seed_ids, depth=depth)

    # 再跑 semantic 扩展 (only for top-K circle-1 seeds)
    base_ids = set(c["chunk_id"] for c in base)
    visited = set(seed_ids) | base_ids
    semantic_results: list[dict[str, Any]] = []

    top_seeds = seed_ids[:semantic_top_k]
    for sid in top_seeds:
        sem_nbrs = fetch_semantic_neighbors(sid, exclude_ids=visited)
        for nb in sem_nbrs:
            cid = nb["chunk_id"]
            if cid in visited:
                continue
            visited.add(cid)
            semantic_results.append({
                "chunk_id": cid,
                "source": nb.get("source", ""),
                "text": nb.get("text", ""),
                # 语义 boost 弱于 temporal/derived (0.10) 和 entity (0.08)
                # 跟 _SPREAD_BOOST_DEPTH2 (0.05) 一档 — 它本质是 circle-2
                "spread_score": _SPREAD_BOOST_DEPTH2,
                "spread_origin": sid,
                "spread_depth": 2,
                "spread_kind": "semantic",
                "raw_cos": nb.get("raw_cos"),
            })

    return base + semantic_results


__all__ = [
    "fetch_temporal_neighbors",
    "fetch_derived_upstream",
    "fetch_entity_neighbors",
    "fetch_semantic_neighbors",
    "spread_to_candidates",
    "spread_with_semantics",
]
