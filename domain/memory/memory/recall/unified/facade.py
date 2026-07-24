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
# k=5 让分数阶梯化更强: rank=0 → 0.17, rank=1 → 0.14, rank=2 → 0.11
# sin valor 0.08 压到很低 → 用户看到的"经历摘要 score=0.08"真的是低相关的
_RRF_K = 5
_DEFAULT_TIMEOUT = 12.0  # vector embedding 冷启动需 ~8s, 留足空间避免向量路永远 silent fail
_MAX_ROUTES = 3  # 向量 / 词法 / 注意力

BudgetKind = Literal["resident", "passive", "on_demand"]

_BUDGET_MAX_CHARS: dict[BudgetKind, int] = {
    "resident": 300,
    # passive (联想自动注入): 提升 600→1500 — 单条复杂规则可能上百字, 600 容易塞 3 条
    # 残缺面包屑。1500 字符 ≈ 500 tokens, 对 LLM 完全无压力, 但能让完整规则被看到。
    # review 2026-07-23 #3.
    "passive": 1500,
    "on_demand": 2000,
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
            "meta_phase": h.get("phase", "experience"),
            "meta_payload": h.get("payload"),  # V3 #3: 透传 payload 给 render_breadcrumbs
            "meta_created_at": h.get("created_at", 0),  # V6 #6
            "_route": "vector",
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
                # P3+ 改: SELECT 里不再读 activation 列 — 因为 chunks.activation 是
                # 持久字段不应作为"动态注意力"源(用户文档 §5.1); activation 真值
                # 由 attention_cache (运行时)提供, 这里读其它静态字段。
                # V6 #7: 加 created_at 透传给 render_breadcrumbs
                f"""SELECT id, source, text, phase, freshness,
                          cognition_state, derived_from, entity_links, created_at
                   FROM chunks WHERE id IN ({placeholders})""",
                ids,
            ).fetchall()
            meta_by_id = {
                r["id"]: {
                    "source": r["source"],
                    "text": r["text"],
                    "phase": r["phase"] or "experience",
                    "freshness": float(r["freshness"] or 0.0),
                    "cognition_state": r["cognition_state"] or None,
                    "derived_from": r["derived_from"] or "[]",
                    "entity_links": r["entity_links"] or "[]",
                    "created_at": float(r["created_at"] or 0),  # V6 #7
                }
                for r in rows
            }
        finally:
            db.close()
        # 从运行时 cache 读 activation(替代原 SQL 列)
        try:
            from domain.memory.memory.recall.unified.attention_cache import (
                get_activations,
            )
            cache_acts = get_activations([r["id"] for r in rows])
            for cid, act in cache_acts.items():
                if cid in meta_by_id:
                    meta_by_id[cid]["activation"] = act
        except Exception as act_e:
            logger.debug("attention_cache read failed (treat as 0): %s", act_e)
    except Exception as e:
        logger.warning("lexical route: failed to load chunk text: %s", e)
        meta_by_id = {}

    out: list[dict] = []
    for cid, score in hits:
        # BM25 过滤: FTS5 对中文常见词泛匹配
        # BM25=3.0 是平衡点: 正常 query 关键词 BM25 通常>=3, 泛匹配常见词通常 1-2.5
        if score < 3.0:
            continue
        meta = meta_by_id.get(cid, {})
        # E2E 修复: lexical base + 新鲜度/活度 bonus,
        # 让刚写进来的晋升认知/项目/待办能挤进 RRF top(没这个 bonus 时新切片
        # 因 base score 低、被高频老对话挤出)。
        bonus = (0.4 * meta.get("activation", 0.0)
                 + 0.2 * meta.get("freshness", 0.0))
        if meta.get("phase") == "cognition":
            bonus += 0.05
        out.append({
            "chunk_id": cid,
            "score": float(score) + bonus,
            "source": meta.get("source", ""),
            "source_kind": "",
            "text": meta.get("text", ""),
            "meta_phase": meta.get("phase", "experience"),
            "meta_created_at": meta.get("created_at", 0),  # V6 #7
            "_route": "lexical",
        })
    return out


def _boost_attention(
    candidates: list[dict],
    *,
    attention_tokens: list[str],
    boost_delta: float = 0.15,
) -> list[dict]:
    """Route E:attention_tokens 命中 → chunk 文本里命中之一 → 提权。
    P4 扩展: 也检查 chunk 的 entity_links — query 含 entity_link 词时 boost。

    防误伤策略 (review 2026-07-23 #5, 中文子串误伤):
      · len(token) == 2  → 严格整词匹配 (吃苹果 vs 苹果公司)
      · len(token) == 3  → 子串允许, 但要求无相邻汉字组成更长的常见词
      · len(token) >= 4  → 子串匹配 (实体名几乎不会误伤)

    加分档:
      · 整词命中 (含 token==2/3 边界)  → +0.15
      · 子串命中 (len>=4)              → +0.15
      · 子串命中 (len==3 但跨边界)      → +0.10
      · 不命中                          → 0
    """
    if not attention_tokens:
        return candidates

    import re as _re
    # 中文字符匹配 (CJK Unified Ideographs + 常用扩展)
    _cjk = _re.compile(r"[\u4e00-\u9fff]")

    def _strict_whole_match(token: str, text: str) -> bool:
        """2-3 字 token, 仅当 token 作为独立词出现在 text 中才匹配。
        判据: token 从文本起始位置出现, 或前/后 1 字符是非 CJK (空格/半角符号/数字等),
        或 token 紧邻标点/英文。简单边界规则即可挡 "吃[苹果]" 命中 "[苹果]公司" 这类。
        """
        idx = 0
        while True:
            pos = text.find(token, idx)
            if pos < 0:
                return False
            before_ok = pos == 0 or not _cjk.match(text[pos - 1])
            after_ok = (pos + len(token) >= len(text)) or not _cjk.match(text[pos + len(token)])
            if before_ok and after_ok:
                return True
            idx = pos + 1

    lowered_tokens = [(t, t.lower()) for t in attention_tokens if t]
    if not lowered_tokens:
        return candidates

    for c in candidates:
        text = (c.get("text") or "").lower()
        if not text:
            c.setdefault("matched_attention_token", None)
            continue

        best_score_add = 0.0
        matched_original = None
        for orig_tok, tok in lowered_tokens:
            tlen = len(tok)
            if tlen < 2:
                continue
            if tlen == 2 or tlen == 3:
                # 短 token 严格整词: 防"吃苹果"vs"苹果公司"
                if _strict_whole_match(tok, text):
                    if boost_delta > best_score_add:
                        best_score_add = boost_delta
                        matched_original = orig_tok
                elif tlen == 3 and tok in text:
                    # 长度 3 的子串匹配可以接受但低 boost
                    if 0.10 > best_score_add:
                        best_score_add = 0.10
                        matched_original = orig_tok
            else:
                # 长实体 (>=4): 子串误伤概率极低, 接受
                if tok in text:
                    if boost_delta > best_score_add:
                        best_score_add = boost_delta
                        matched_original = orig_tok

        c["score"] += best_score_add
        c["matched_attention_token"] = matched_original
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
                    "meta_phase": c.get("meta_phase", "experience"),
                    # V3 #3: 透传 payload (premise/rationale) 到 render_breadcrumbs
                    "payload": c.get("payload") or c.get("meta_payload"),
                    # V6 #6: 透传 created_at 给 render_breadcrumbs 显示认知年龄
                    "created_at": c.get("created_at") or c.get("meta_created_at", 0),
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
    max_total_chars: int = 1500,
) -> str:
    """把 unified_recall 结果渲染成面包屑,注入 agent.messages 用。
    语义要兼容既有 _inject_entity_recall 的 🎯/🔍 标注(spec §User Story 2)。

    V3 (2026-07-23 #3): 若该 result 带 payload.premise/rationale, 追加背景条目让
    model 看到决策推理链路(Zero 反馈"醒来后必须重新推导才能确认前提没变"的修复点).
    """
    if not results:
        return ""

    lines = ["[联想命中 — 统一召回]"]
    total = 0
    shown = 0
    import json as _j
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
        # V6 #6: 加 chunk_id + created_at 到每条面包屑
        # chunk_id 让模型可以直接 supersede_memory / mark_obsolete / delete_cognition
        # created_at 让模型判断认知新旧
        cid = r.get("chunk_id", "?")
        created_raw = r.get("created_at") or r.get("meta_created_at") or 0
        if isinstance(created_raw, (int, float)) and created_raw > 0:
            import time as _bt
            age_days = int((_bt.time() - created_raw) / 86400)
            age_tag = f"{age_days}d前" if age_days > 0 else "今天"
        else:
            age_tag = "?"
        # 命中的 attention token 标注
        match_tag = ""
        if r.get("matched_attention_token"):
            match_tag = f" [命中:{r['matched_attention_token']}]"
        # V6: 不暴露 score 给模型 — 模型读文本内容自己判断质量, 数字反而干扰
        entry = f"- {icon}[{label}] #{cid} ({age_tag}){match_tag} {body}"
        lines.append(entry)
        total += len(entry)
        # V3 #3: 若该 result 带 payload.premise 或 .rationale → 追加背景条目
        payload = r.get("payload")
        if not payload and r.get("meta_payload"):
            # 不同路径的存储 key 不一, 双看
            try:
                payload = _j.loads(r["meta_payload"]) if isinstance(r["meta_payload"], str) else r["meta_payload"]
            except Exception:
                payload = None
        if isinstance(payload, dict):
            premise = payload.get("premise")
            rationale = payload.get("rationale")
            if premise:
                p_line = f"    · 背景: {(str(premise)[:140]).strip()}"
                lines.append(p_line)
                total += len(p_line)
            if rationale:
                rat_line = f"    · 理由: {(str(rationale)[:140]).strip()}"
                lines.append(rat_line)
                total += len(rat_line)
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
    scene_hint: str | None = None,
    cognition_only: bool = False,
) -> list[dict]:
    """统一记忆检索入口。详见模块 docstring。

    cognition_only=True (联想路径): 只召回认知(rules/lessons/knowledge/promoted),
      不召回经历(digest/conversation) → 联想注入的是精华不是噪音。
    cognition_only=False (recall_memory 工具): 全局检索,包括经历 + 认知 →
      模型有明确检索意图时深挖。

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

    # cognition_only 模式: 联想路径只召回认知, 不召回经历
    # 认知 source: rule/rule/lesson/lessons/insight/fact/knowledge/project + phase=cognition
    # 同时支持单复数(历史混存, 2026-07-23 标准化后是单数但兼容老的复数 chunk)
    _COGNITION_SOURCES = {
        "rule", "rules", "lesson", "lessons", "insight", "fact",
        "knowledge", "project",
    }

    def _filter_cognition(items: list[dict]) -> list[dict]:
        if not cognition_only:
            return items
        return [c for c in items
                if c.get("source", "") in _COGNITION_SOURCES
                or c.get("meta_phase", "") == "cognition"]

    # 1. 三路并发,各在未来里跑;超时返回已完成部分(检索非阻断点 FR-001/FR-104)
    routes_raw: dict[str, list[dict]] = {}

    def _run_vector():
        return _filter_cognition(_route_vector(query, extra_context=extra_context, max_chars=max_chars))
    def _run_lexical():
        return _filter_cognition(_route_lexical(query, limit=20))

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

    # 3. 写回 routes_raw — 修复 RRF route 归属 bug
    # 旧 bug: 用 chunk_id>=0 判 lexical/vector, 但 P2 后两路都返真实 chunk_id → 全归 lexical
    # 修复: 每个候选带 _route 标记(由 _route_vector / _route_lexical 写入), 按 _route 归
    boosted_routes: dict[str, list[dict]] = {"vector": [], "lexical": []}
    for c in pool_candidates:
        route_label = c.pop("_route", "vector")  # 默认 vector
        if route_label in boosted_routes:
            boosted_routes[route_label].append(c)
        else:
            boosted_routes["vector"].append(c)
    # 重排
    for k in boosted_routes:
        boosted_routes[k] = sorted(
            boosted_routes[k], key=lambda x: x.get("score", 0.0), reverse=True
        )

    # 4. RRF 融合
    fused = _rrf_fuse(boosted_routes)

    # P3+ 场景意图过滤器(用户文档 §4): 判定当前 scene, 给每个 source
    # 一个 multiplier, 在终排 final_score 上乘。一刀切的 RRF 改成"按场景分流"。
    # 比如 deep_work 时把 conversation 0.4 压低、rules 1.4 抬高;
    # chat 时反过来。这是 precision 0.20 痛点的根本解。
    from domain.memory.memory.recall.unified.scene_weights import (
        detect_scene, weight_multiplier,
    )
    scene = detect_scene(query, hint=scene_hint)
    if scene != "balanced":
        logger.debug("unified_recall scene=%s for query=%r", scene, query[:50])

    # P2.1 (用户认知修正 2026-07-16): 联想 = "助推" 而非 "召回",
    # 邻居/derived 不再硬塞回调,而是进候选池 + spread_boost,
    # 仍参与终排,可能因相关性不够被压在 top-K 外。
    # 三阶段: 召回 → 扩散 → 终排。
    seed_ids = [r.get("chunk_id") for r in fused
                if isinstance(r.get("chunk_id"), int) and r.get("chunk_id", -1) >= 0]
    # seed_ids 取召回阶段 top 5 作 seed(避免全 fused 都扩散,性能 + 噪音管理)
    seed_top = seed_ids[:5]
    spread_candidates: list[dict] = []
    if seed_top:
        try:
            # v2 (2026-07-24): spread_with_semantics — 加 semantic 邻居路径
            # 用 top 3 seeds 做语义扩展 (cost: 3 次 lookup_scan ≈ 60ms, 不需新 embedding
            # — 直接用 cog chunks 已存的 embedding 做对比).
            from domain.memory.memory.recall.unified.spread import spread_with_semantics
            spread_candidates = spread_with_semantics(seed_top, depth=2, semantic_top_k=3)
        except Exception as e:
            logger.debug("spread_with_semantics failed (will skip spread): %s", e)

    # 给 spread 候选包装成 RRF 兼容形态(标记 route=spread), 并参与终排
    # 终排 score = (rrf_score + cog_bonus) × scene_multiplier + spread_boost
    final_pool: dict[int, dict] = {}
    for r in fused:
        cid = r.get("chunk_id", -1)
        if cid < 0:
            # 无 id 的(向量 fallback)用 text hash 当 key
            cid_key = f"text:{_texthash(r.get('text', ''))}"
        else:
            cid_key = cid
        # cognition bonus(只在终排, 召回阶段已各自给过 vector 路)
        # 用户反馈 2026-07-16: cognition 过度抢占 → 把用户对话经历压在外;
        # 不仅终排降到 0.05, 还要让出位置给真正命中 query 的 experience。
        # 决断:cognition 应当只在"同 query 也匹配良好"时获优先,否则不算优选。
        cog_bonus = 0.0
        if r.get("meta_phase") == "cognition":
            cog_bonus = 0.02
        # 场景意图 multiplier(overlay 在 rrf+cog 之上)
        scene_mult = weight_multiplier(r.get("source", ""), scene)
        r["final_score"] = (
            r.get("rrf_score", 0.0)
            + cog_bonus
        ) * scene_mult
        r["scene"] = scene
        r["scene_mult"] = scene_mult
        final_pool[cid_key] = r

    for s in spread_candidates:
        cid = s["chunk_id"]
        # 用户认知 2026-07-16: spread 不应跨主题串扰(尤其是 unrelated cognition 派生
        # 把复盘话题带进 project X query 的情况)。这里对 spread 候选加 source
        # 一致性硬过滤——派生跨 phase/source 类型的联想默认不生效, 保持每个 query
        # 的主题边界。
        seed_origin = s.get("spread_origin")
        seed_source = None
        # 拿 seed 自己的 source, 与 spread 候选 source 对比
        for f in fused:
            if f.get("chunk_id") == seed_origin:
                seed_source = f.get("source", "")
                break
        cand_source = s.get("source", "")
        spread_kind = s.get("spread_kind")  # 'derived' / 'entity' / 'semantic'(V3v2) / None(temporal)
        # 同 source 类(都 digest_session / 都 rules / ...) 才放行;
        # 否则只给 1/4 的弱 boost,保留"跨主题也能想到"的微弱关联。
        if seed_source and cand_source and seed_source == cand_source:
            actual_boost = s.get("spread_score", 0.0)
        elif seed_source and cand_source:
            if spread_kind == "semantic":
                # V3 v2 (2026-07-24): semantic 邻居是跨 source 的本质价值 —
                # 同一标的的"事实→教训→规则"分布在不同 source, 必须允许跨.
                # boost 用 1/2 而不是 1/4 (比常规 cross-source 强, 但仍弱于同类 boost).
                actual_boost = s.get("spread_score", 0.0) * 0.5
            else:
                # 跨 source 类(比如 derived cognition 把 experience 带进 cognition query)
                # 只取微弱提示
                actual_boost = s.get("spread_score", 0.0) * 0.25
        else:
            actual_boost = s.get("spread_score", 0.0)
        # spread boost 也走场景权重(否则 conversation noise 仍可能靠 spread 抢位)
        actual_boost *= weight_multiplier(cand_source, scene)
        if cid in final_pool:
            # 已在召回结果里,只加 boost(其 base 已在主 loop 乘过 scene_mult)
            final_pool[cid]["final_score"] = (
                final_pool[cid].get("final_score", 0.0)
                + actual_boost
            )
            final_pool[cid].setdefault("routes", []).append("spread")
            if final_pool[cid].get("spread_origin") is None:
                final_pool[cid]["spread_origin"] = s.get("spread_origin")
        else:
            # 新加入的扩散项:base 0 (无 RRF 命中), 仅靠 boost + scene_mult 占位
            final_pool[cid] = {
                "chunk_id": cid,
                "text": s.get("text", ""),
                "source": cand_source,
                "source_kind": "",
                "rrf_score": 0.0,
                "routes": ["spread"],
                "matched_attention_token": None,
                "max_route_score": 0.0,
                "spread_origin": s.get("spread_origin"),
                "spread_depth": s.get("spread_depth", 1),
                "final_score": actual_boost,
                "scene": scene,
                "scene_mult": weight_multiplier(cand_source, scene),
            }

    # 排序:final_score 为主
    sorted_pool = sorted(
        final_pool.values(),
        key=lambda x: x.get("final_score", 0.0),
        reverse=True,
    )

    # 5. 排除 + 预算 + 最低相似度阈值过滤
    filtered = [r for r in sorted_pool if r.get("chunk_id", -1) not in exclude]
    # V6 #4: 最低 final_score 阈值 — 不是排完 top-K 就完事,
    # 而是先过滤掉分值太低的(确保至少有一定相似性), 再做字符预算裁剪.
    # 阈值 0.10 是 RRF 后的最低门槛: 比纯 RRF 贡献(1/(5+rank)) ~0.05 高,
    # 留出 boost (entity_links/attention) 的提权空间.
    MIN_FINAL_SCORE = 0.10
    filtered = [r for r in filtered if r.get("final_score", 0.0) >= MIN_FINAL_SCORE]
    # 截到预算字符上限
    out: list[dict] = []
    total = 0
    for r in filtered:
        body_len = len(r.get("text", ""))
        if total + body_len > max_chars and out:
            break
        r["rrf_score"] = r.get("final_score", 0.0)
        out.append(r)
        total += body_len

    # V6: 去掉 access signal 写库 — on_access 只写运行时 cache(热信号),
    # 调 apply_signal 会触发 _persist_slice + db.commit 空写库.
    # 想保留热信号 → 直接写 attention_cache 即可, 不走 apply_signal.
    try:
        from domain.memory.memory.recall.unified.attention_cache import bump_activation
        for r in out[:5]:
            cid = r.get("chunk_id", -1)
            if cid and cid > 0:
                bump_activation(cid)
    except Exception:
        pass  # 非关键路径

    return out


__all__ = ["unified_recall", "render_breadcrumbs"]
