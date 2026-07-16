"""P2 — 统一记忆检索面 (single facade)。

设计:
- specs/002-unified-memory/contracts/contracts.md §1
- specs/002-unified-memory/plan.md §Implementation Approach / P2

三路融合 + 硬时间上限:
  Route V: 向量语义(domain.memory.memory.recall.vector.recall)
  Route K: FTS5 BM25(domain.memory.memory.recall.unified.fts)
  Route E: attention_tokens 提权(从 entity_index 抽 entity + 给 chunks 加权)
+ 时序邻居候选(P3 才完整启用 session_id/segment_index,P2 暂不产)
+ RRF 融合打分 = Σ 1/(k + rank_in_route),k=60
+ 硬时间上限 5s(ThreadPoolExecutor + wait(timeout=5)) 超时返回已完成部分

任一路失败 → 降级到下一路;全部失败 → 返回 [] 让常驻层兜底(FR-001)。
消费侧:
- infrastructure.ai.agent._inject_entity_recall
- domain.lifecycle.heartbeat._build_memory_context
- interfaces.tools.sense_tools(recall_memory / recall_entity)
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any, Literal

from domain.memory.memory.recall.unified import fts

logger = logging.getLogger("domain.memory.recall.unified")

# RRF 常数 + 预算
_RRF_K = 60
_DEFAULT_TIMEOUT = 5.0
_MAX_ROUTES = 3  # 向量 / 词法 / 注意力

BudgetKind = Literal["resident", "passive", "on_demand"]

_BUDGET_MAX_CHARS: dict[BudgetKind, int] = {
    "resident": 300,
    "passive": 600,
    "on_demand": 1500,
}


# ───────────────── subroute primitives ─────────────────

def _route_vector(query: str, *, extra_context: str = "", max_chars: int) -> list[dict]:
    """向量语义路 — 调 recall_structured 拿 list[dict](含真实 chunk_id)。
    返回 [{chunk_id, score, source, text}]。
    (P2 修复: 原本走 recall() 字符串解析导致 chunk_id 全 -1、RRF 对应不上。)
    """
    try:
        from domain.memory.memory.recall.vector import recall_structured
        hits = recall_structured(query, extra_context=extra_context, max_total_chars=max_chars)
    except Exception as e:
        logger.warning("vector route failed (will degrade): %s", e)
        return []
    if not hits:
        return []
    # 加上 metadata 让 lexical bonus 路径与 vector 共享同一字段
    out: list[dict] = []
    for h in hits:
        out.append({
            "chunk_id": int(h.get("chunk_id", -1)),
            "score": float(h.get("score", 0.0)),
            "source": h.get("source", ""),
            "source_kind": "",
            "text": h.get("text", ""),
        })
    return out[:20]


def _route_lexical(query: str, *, limit: int) -> list[dict]:
    """词法 BM25 路。返回 [{chunk_id, score(-bm25), source, text}]。"""
    try:
        hits = fts.fts_search(query, limit=limit)
    except Exception as e:
        logger.warning("lexical route failed (will degrade): %s", e)
        return []
    if not hits:
        return []

    # 拉对应 chunk 行 + source
    try:
        from domain.memory.memory.recall.vector import _get_db as _get_vec_db
        db = _get_vec_db()
        try:
            ids = tuple(cid for cid, _ in hits)
            placeholders = ",".join("?" * len(ids))
            rows = db.execute(
                f"""SELECT id, source, text, phase, activation, freshness,
                          cognition_state, derived_from, entity_links
                   FROM chunks WHERE id IN ({placeholders})""",
                ids,
            ).fetchall()
            meta_by_id = {
                r["id"]: {
                    "source": r["source"],
                    "text": r["text"],
                    "phase": r["phase"] or "experience",
                    "activation": float(r["activation"] or 0.0),
                    "freshness": float(r["freshness"] or 0.0),
                    "cognition_state": r["cognition_state"] or None,
                    "derived_from": r["derived_from"] or "[]",
                    "entity_links": r["entity_links"] or "[]",
                }
                for r in rows
            }
        finally:
            db.close()
    except Exception as e:
        logger.warning("lexical route: failed to load chunk text: %s", e)
        meta_by_id = {}

    out: list[dict] = []
    for cid, score in hits:
        meta = meta_by_id.get(cid, {})
        # E2E 修复: lexical base + 新鲜度/活度 bonus,
        # 让刚写进来的晋升认知/项目/待办能挤进 RRF top(没这个 bonus 时新切片
        # 因 base score 低、被高频老对话挤出)。
        bonus = (0.4 * meta.get("activation", 0.0)
                 + 0.2 * meta.get("freshness", 0.0))
        # 认知类微 boost(规则/教训/晋升结果应有更高召回权重)
        if meta.get("phase") == "cognition":
            bonus += 0.5
        out.append({
            "chunk_id": cid,
            "score": float(score) + bonus,  # 已转正 + 新鲜度/活度 bonus
            "source": meta.get("source", ""),
            "source_kind": "",
            "text": meta.get("text", ""),
            "meta_phase": meta.get("phase", "experience"),
        })
    return out


def _boost_attention(
    candidates: list[dict],
    *,
    attention_tokens: list[str],
    boost_delta: float = 0.15,
) -> list[dict]:
    """Route E:attention_tokens 命中 → chunk 文本里命中之一 → 提权。
    (P2 简化版:P3 改用 entity_links JSON 字段提权更准。)
    """
    if not attention_tokens:
        return candidates
    lowered = [t.lower() for t in attention_tokens if t]
    if not lowered:
        return candidates
    for c in candidates:
        text = (c.get("text") or "").lower()
        if any(tok in text for tok in lowered):
            c["score"] += boost_delta
            c["matched_attention_token"] = next(
                (attention_tokens[i] for i, t in enumerate(lowered) if t in text),
                None,
            )
        else:
            c.setdefault("matched_attention_token", None)
    return candidates


# ───────────────── RRF 融合 + 去重 ─────────────────

def _rrf_fuse(
    routes: dict[str, list[dict]],
    *,
    k: int = _RRF_K,
) -> list[dict]:
    """倒数排名融合。同 chunk_id 的候选合并/RRF 打分。
    chunk_id == -1 表示无 id(vector 路只产文本),按文本指纹去重。
    """
    # 收每个候选: rr contribution + 维持原 score 作 release-time tiebreaker
    fused: dict[Any, dict] = {}
    for route_name, items in routes.items():
        # route 内按 score 排序分 rank,rank=0 最高
        sorted_items = sorted(items, key=lambda x: x.get("score", 0.0), reverse=True)
        for rank_i, c in enumerate(sorted_items):
            key = c["chunk_id"] if c.get("chunk_id", -1) >= 0 else _texthash(c.get("text", ""))
            contribution = 1.0 / (k + rank_i + 1)
            if key not in fused:
                fused[key] = {
                    "chunk_id": c.get("chunk_id", -1),
                    "text": c.get("text", ""),
                    "source": c.get("source", ""),
                    "source_kind": c.get("source_kind", ""),
                    "rrf_score": 0.0,
                    "routes": [],
                    "matched_attention_token": c.get("matched_attention_token"),
                    "max_route_score": c.get("score", 0.0),
                }
            fused[key]["rrf_score"] += contribution
            fused[key]["routes"].append(route_name)
            fused[key]["max_route_score"] = max(
                fused[key]["max_route_score"], c.get("score", 0.0)
            )
            # 文本/source 优先取有 id 的精确来源
            if c.get("chunk_id", -1) >= 0:
                fused[key]["text"] = c.get("text", "") or fused[key]["text"]
                fused[key]["source"] = c.get("source", "") or fused[key]["source"]

    # 排序:主键 RRF,次键 route score
    out = sorted(
        fused.values(),
        key=lambda x: (x["rrf_score"], x["max_route_score"]),
        reverse=True,
    )
    return out


def _texthash(text: str) -> str:
    import hashlib
    return hashlib.md5((text or "")[:200].encode("utf-8")).hexdigest()


# ───────────────── breadcrumb renderer ─────────────────

_SOURCE_LABELS = {
    "vector": "语义",
    "conversation": "对话",
    "digest_session": "经历摘要",
    "digest_segment": "段叙事",
    "digest_day": "日摘要",
    "digest_week": "周摘要",
    "rules": "行为规则",
    "lessons": "教训",
    "self_knowledge": "自我认知",
    "context": "上下文",
    "identity": "自我",
    "journal": "日记",
    "notes": "笔记",
    "him": "用户",
    "work": "工作",
    "goals": "目标",
    "plans": "计划",
}


def _label_of(source: str) -> str:
    return _SOURCE_LABELS.get(source, source.upper() if source else "记忆")


def _route_icon(route_name: str) -> str:
    if route_name == "vector":
        return "🔍"
    if route_name == "lexical":
        return "📅"
    return "🔗"


def render_breadcrumbs(
    results: list[dict],
    *,
    new_entities: list[str] | None = None,
    max_total_chars: int = 600,
) -> str:
    """把 unified_recall 结果渲染成面包屑,注入 agent.messages 用。
    语义要兼容既有 _inject_entity_recall 的 🎯/🔍 标注(spec §User Story 2)。
    """
    if not results:
        return ""

    lines = ["[联想命中 — 统一召回]"]
    total = 0
    shown = 0
    for r in results:
        if total >= max_total_chars:
            break
        # icon:优先按 route(已融合过)
        route_names = r.get("routes") or ["vector"]
        # 第一图标优先 attention_match,其次 vector,再 lexical
        if r.get("matched_attention_token"):
            icon = "🎯"
        else:
            icon = _route_icon(route_names[0])
        label = _label_of(r.get("source", ""))
        score = r.get("rrf_score", 0.0)
        body = (r.get("text") or "")[:200].replace("\n", " ")
        if not body:
            continue
        # 命中的 attention token 标注
        match_tag = ""
        if r.get("matched_attention_token"):
            match_tag = f" [命中:{r['matched_attention_token']}]"
        entry = f"- {icon}[{label} score={score:.2f}]{match_tag} {body}"
        lines.append(entry)
        total += len(entry)
        shown += 1

    if shown == 0:
        return ""

    tail = f"(命中 {len(new_entities or [])} 实体" \
           + (f": {', '.join(new_entities[:6])}" if new_entities else "") \
           + (f" 等{len(new_entities) - 6}个" if new_entities and len(new_entities) > 6 else "") \
           + f"; 召回 {shown} 条。如需更多调 recall_entity('实体名'))"
    lines.append(tail)
    return "\n".join(lines)


# ───────────────── main facade ─────────────────

def unified_recall(
    query: str,
    *,
    extra_context: str = "",
    attention_tokens: list[str] | None = None,
    exclude_chunk_ids: set[int] | None = None,
    budget_kind: BudgetKind = "passive",
    max_total_chars: int | None = None,
    timeout_seconds: float = _DEFAULT_TIMEOUT,
) -> list[dict]:
    """统一记忆检索入口。详见模块 docstring。

    返回 list[dict]:
      chunk_id, text, source, source_kind, rrf_score, routes[],
      matched_attention_token(None 或 str), max_route_score
    (消费侧 render_breadcrumbs 决定怎么展示给 model。)
    """
    if not query or not query.strip():
        return []

    # P3 T034: 懒 backfill 历史 chunks(单进程一次)。失败不阻塞检索。
    try:
        from domain.memory.memory.recall.unified.migration import (
            backfill_slice_fields_if_needed,
        )
        backfill_slice_fields_if_needed()
    except Exception:
        pass

    max_chars = max_total_chars or _BUDGET_MAX_CHARS.get(budget_kind, 600)
    exclude = exclude_chunk_ids or set()

    # 1. 三路并发,各在未来里跑;超时返回已完成部分(检索非阻断点 FR-001/FR-104)
    routes_raw: dict[str, list[dict]] = {}

    def _run_vector():
        return _route_vector(query, extra_context=extra_context, max_chars=max_chars)
    def _run_lexical():
        return _route_lexical(query, limit=20)

    with ThreadPoolExecutor(max_workers=_MAX_ROUTES) as pool:
        futures = {
            "vector": pool.submit(_run_vector),
            "lexical": pool.submit(_run_lexical),
        }
        done, not_done = wait(futures.values(), timeout=timeout_seconds)
        # 把 done 的对应结果取出
        for name, fut in futures.items():
            if fut in done:
                try:
                    routes_raw[name] = fut.result()
                except Exception as e:
                    logger.warning("route %s raised (will skip): %s", name, e)
                    routes_raw[name] = []
            else:
                logger.warning(
                    "route %s timed out after %.1fs (degraded)", name, timeout_seconds
                )
                fut.cancel()
                routes_raw[name] = []

    # 2. 合并池 + Route E 提权
    pool_candidates: list[dict] = []
    for items in routes_raw.values():
        pool_candidates.extend(items)
    pool_candidates = _boost_attention(
        pool_candidates, attention_tokens=attention_tokens or []
    )

    # 3. 写回 routes_raw 里(attention boost 已作用),给 RRF 用
    # 简单做法:把 boost 反映到 score;然后按 (route, score) 重排
    boosted_routes: dict[str, list[dict]] = {"vector": [], "lexical": []}
    for c in pool_candidates:
        # 把候选归入它最强的 route(vector > lexical);若一条同时在两路出,RRF 会自然提升它
        if c.get("chunk_id", -1) >= 0:
            boosted_routes["lexical"].append(c)
        else:
            boosted_routes["vector"].append(c)
    # 重排
    for k in boosted_routes:
        boosted_routes[k] = sorted(
            boosted_routes[k], key=lambda x: x.get("score", 0.0), reverse=True
        )

    # 4. RRF 融合
    fused = _rrf_fuse(boosted_routes)

    # 5. 排除 + 预算
    filtered = [r for r in fused if r.get("chunk_id", -1) not in exclude]
    # 截到预算字符上限
    out: list[dict] = []
    total = 0
    for r in filtered:
        body_len = len(r.get("text", ""))
        if total + body_len > max_chars and out:
            break
        out.append(r)
        total += body_len

    return out


__all__ = ["unified_recall", "render_breadcrumbs"]
