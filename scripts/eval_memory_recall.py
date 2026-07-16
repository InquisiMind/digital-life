#!/usr/bin/env python3
"""记忆召回评测集 + 基线评测脚本。

用法：
    python3 scripts/eval_memory_recall.py [instance_id]

不传 instance_id 默认 zero (c2a5c8e8)。

评测流程：
  1. 从 entity_index.json 自动构造评测 case——每个 case = (query_text, expected_entities)
  2. 跑当前召回路径（entity_index 精确匹配 + 向量 recall）看实际命中
  3. 计算 Recall / Precision / MRR + 各路贡献占比
  4. 输出 JSON 报告 + 终端摘要

构造原则（自动、不依赖人工标注）：
  - 从实体碎片里抽 snippet 作为 query_text（"如果模型说这段话，应该召回什么实体"）
  - expected_entities = 该 snippet 所属的实体 + linked_entities 里的关联实体
  - 精确匹配路用 entity_index.extract_entities_from_context 验证
  - 语义路用向量 recall 验证

评分维度：
  - entity_hit: query_text 是否触发 extract_entities_from_context 命中 expected_entities
  - recall_hit: 向量 recall 返回的 chunk 是否含 expected 实体关键词
  - rank: 命中的排在第几位（MRR 用）
"""
from __future__ import annotations

import json
import logging
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logger = logging.getLogger("eval_memory_recall")

# ── 评测集构造 ──────────────────────────────────────────────────


def build_eval_cases(entity_index_path: Path, max_cases: int = 100) -> list[dict]:
    """从实体索引自动构造评测 case。

    每个 case：
      query_text: 从碎片 snippet 抽取的一段文本（模拟模型可能在 reasoning 里说的）
      expected_entities: 该碎片所属实体名 + linked_entities
      expected_keywords: 碎片 snippet 的关键词（用于验证召回内容相关性）
      source_entity: 原始实体名
      memory_type: consciousness/lesson/rule/insight
    """
    data = json.loads(entity_index_path.read_text(encoding="utf-8"))
    entities = data.get("entities", {})

    cases = []
    for entity_name, entity_data in entities.items():
        memories = entity_data.get("memories", [])
        aliases = entity_data.get("aliases", [])
        for mem in memories:
            snippet = (mem.get("snippet") or "").strip()
            if len(snippet) < 20:
                continue
            mtype = mem.get("memory_type", "")
            # 跳过噪音类型
            if mtype in ("trading_wait", "system_wait", "monitor", "final_status"):
                continue

            # linked_entities 里的关联实体是"期望应当一起被召回"的
            linked = [e for e in (mem.get("linked_entities") or []) if e]

            # 从 snippet 抽关键词（简单分词取前 5 个有意义的词）
            keywords = []
            for w in snippet.replace(",", " ").replace(".", " ").replace("，", " ").replace("。", " ").split():
                w = w.strip()
                if len(w) >= 2 and w not in ("的", "了", "是", "在", "和", "与", "到", "为", "以", "后", "前"):
                    keywords.append(w)
            keywords = keywords[:5]

            cases.append({
                "source_entity": entity_name,
                "query_text": snippet[:200],
                "expected_entities": [entity_name] + linked,
                "expected_keywords": keywords,
                "memory_type": mtype,
                " snippet_full": snippet,
            })

    # 随机采样（保证多样性）
    random.seed(42)
    if len(cases) > max_cases:
        random.shuffle(cases)
        cases = cases[:max_cases]

    return cases


# ── 召回评测 ───────────────────────────────────────────────────


def eval_entity_route(query_text: str, expected_entities: list[str]) -> dict:
    """跑 entity_index 的精确匹配路线。

    返回 {hit_entities, missed_entities, rank_of_expected}
    """
    from domain.memory.memory.consciousness.entity_index import extract_entities_from_context

    hit = extract_entities_from_context(query_text)
    hit_set = set(hit)
    expected_set = set(expected_entities)

    hit_expected = hit_set & expected_set
    missed = expected_set - hit_set

    # MRR：期望实体在命中列表里的排名
    rank = None
    for i, h in enumerate(hit):
        if h in expected_set:
            rank = i + 1
            break

    return {
        "hit_count": len(hit),
        "hit_expected_count": len(hit_expected),
        "missed_count": len(missed),
        "missed": sorted(missed)[:5],
        "rank": rank,
    }


def eval_vector_route(query_text: str, expected_entities: list[str],
                      expected_keywords: list[str]) -> dict:
    """跑向量 recall 路线。

    返回 {recall_count, relevant_count, rank_of_relevant, top_snippets}
    """
    try:
        from domain.memory.memory.recall.vector import recall
        results = recall(query_text, max_total_chars=400)
    except Exception as exc:
        logger.warning("vector recall failed: %s", exc)
        return {"recall_count": 0, "relevant_count": 0, "error": str(exc)}

    # recall 可能返回 str（拼接后的文本）或 list[dict]
    if isinstance(results, str):
        text_lower = results.lower()
        relevant = 0
        for entity in expected_entities:
            if entity.lower() in text_lower:
                relevant += 1
        for kw in expected_keywords:
            if kw.lower() in text_lower:
                relevant += 1
        return {
            "recall_count": 1,
            "relevant_count": min(relevant, 1),
            "rank": 1 if relevant > 0 else None,
            "top_snippets": [{"source": "str", "snippet": results[:80], "score": 0}],
        }

    if not results:
        return {"recall_count": 0, "relevant_count": 0}

    # list[dict] 路径
    relevant = 0
    rank = None
    snippets = []
    for i, r in enumerate(results):
        if isinstance(r, str):
            text = r.lower()
        else:
            text = (r.get("text") or r.get("snippet") or "").lower()
        source = r.get("source", "") if isinstance(r, dict) else "str"
        snippets.append({"source": source, "snippet": text[:80], "score": r.get("score", 0) if isinstance(r, dict) else 0})

        # 是否含期望实体名
        is_relevant = False
        for entity in expected_entities:
            if entity.lower() in text:
                is_relevant = True
                break
        # 是否含期望关键词
        if not is_relevant:
            for kw in expected_keywords:
                if kw.lower() in text:
                    is_relevant = True
                    break

        if is_relevant:
            relevant += 1
            if rank is None:
                rank = i + 1

    return {
        "recall_count": len(results),
        "relevant_count": relevant,
        "rank": rank,
        "top_snippets": snippets[:3],
    }


# ── 主流程 ────────────────────────────────────────────────────


def main() -> int:
    instance_id = sys.argv[1] if len(sys.argv) > 1 else "c2a5c8e8-e4f5-4c69-be3e-aac49903081d"
    instance_dir = ROOT / "apps" / instance_id
    entity_index_path = instance_dir / "data" / "memories" / "entity_index.json"

    if not entity_index_path.exists():
        print(f"entity_index.json not found: {entity_index_path}")
        return 1

    from infrastructure.config import set_current_instance_id, reset_current_instance_id
    token = set_current_instance_id(instance_id)
    try:
        print(f"Building eval cases from {instance_id[:8]}...")
        cases = build_eval_cases(entity_index_path, max_cases=100)
        print(f"Generated {len(cases)} cases")
        print()

        # 统计 case 分布
        from collections import Counter
        type_dist = Counter(c["memory_type"] for c in cases)
        print("Case distribution by memory_type:")
        for t, n in type_dist.most_common():
            print(f"  {t:20s} {n}")
        print()

        # 逐 case 跑评测
        print("Running eval (entity route + vector route)...")
        results = []
        t0 = time.time()
        for i, case in enumerate(cases):
            entity_eval = eval_entity_route(case["query_text"], case["expected_entities"])
            vector_eval = eval_vector_route(
                case["query_text"], case["expected_entities"], case["expected_keywords"]
            )
            results.append({
                "case_id": i,
                "source_entity": case["source_entity"],
                "memory_type": case["memory_type"],
                "entity_route": entity_eval,
                "vector_route": vector_eval,
            })
            if (i + 1) % 20 == 0:
                print(f"  {i+1}/{len(cases)} done ({time.time()-t0:.1f}s)")

        elapsed = time.time() - t0

        # 汇总
        entity_cases = len(results)
        entity_hit_cases = sum(1 for r in results if r["entity_route"]["hit_expected_count"] > 0)
        entity_rr = [1.0 / r["entity_route"]["rank"] for r in results if r["entity_route"]["rank"]]
        entity_mrr = sum(entity_rr) / entity_cases if entity_cases else 0

        vec_cases = sum(1 for r in results if r["vector_route"].get("recall_count", 0) > 0)
        vec_hit_cases = sum(1 for r in results if r["vector_route"].get("relevant_count", 0) > 0)
        vec_rr = [1.0 / r["vector_route"]["rank"] for r in results if r.get("vector_route", {}).get("rank")]
        vec_mrr = sum(vec_rr) / entity_cases if entity_cases else 0

        # 合并覆盖：两路至少一路命中
        combined_hit = sum(
            1 for r in results
            if r["entity_route"]["hit_expected_count"] > 0
            or r["vector_route"].get("relevant_count", 0) > 0
        )

        print()
        print("=" * 60)
        print(f"MEMORY RECALL EVALUATION REPORT ({entity_cases} cases, {elapsed:.1f}s)")
        print("=" * 60)
        print()
        print(f"{'Metric':<30s} {'Entity Route':>15s} {'Vector Route':>15s} {'Combined':>15s}")
        print("-" * 75)
        print(f"{'Cases with results':<30s} {entity_cases:>15d} {vec_cases:>15d} {'—':>15s}")
        print(f"{'Cases with hits':<30s} {entity_hit_cases:>15d} {vec_hit_cases:>15d} {combined_hit:>15d}")
        def _pct(n, d):
            return f"{n/d*100:.1f}%" if d else "0.0%"

        print(f"{'Recall (hit/cases)':<30s} {_pct(entity_hit_cases, entity_cases):>15s} {_pct(vec_hit_cases, entity_cases):>15s} {_pct(combined_hit, entity_cases):>15s}")
        print(f"{'MRR':<30s} {entity_mrr:>15.3f} {vec_mrr:>15.3f} {'—':>15s}")
        print()

        # 按 memory_type 分组统计
        print("Entity Route Recall by memory_type:")
        for mt, _ in type_dist.most_common():
            mt_cases = [r for r in results if r["memory_type"] == mt]
            mt_hits = sum(1 for r in mt_cases if r["entity_route"]["hit_expected_count"] > 0)
            print(f"  {mt:20s} {mt_hits}/{len(mt_cases)} = {mt_hits/len(mt_cases):.1%}" if mt_cases else f"  {mt:20s} 0/0")

        # miss 样本（前 10）
        misses = [r for r in results if r["entity_route"]["hit_expected_count"] == 0]
        if misses:
            print()
            print(f"Entity Route Misses ({len(misses)} cases, showing first 10):")
            for r in misses[:10]:
                er = r["entity_route"]
                print(f"  [{r['case_id']:3d}] entity={r['source_entity'][:25]} type={r['memory_type']} missed={er['missed'][:3]}")

        # 保存完整报告
        report = {
            "instance_id": instance_id,
            "n_cases": entity_cases,
            "elapsed_s": round(elapsed, 1),
            "entity_route": {
                "recall": entity_hit_cases / entity_cases if entity_cases else 0,
                "mrr": entity_mrr,
                "n_with_hits": entity_hit_cases,
            },
            "vector_route": {
                "recall": vec_hit_cases / entity_cases if entity_cases else 0,
                "mrr": vec_mrr,
                "n_with_results": vec_cases,
                "n_with_hits": vec_hit_cases,
            },
            "combined": {
                "recall": combined_hit / entity_cases if entity_cases else 0,
            },
            "details": results,
        }
        report_path = instance_dir / "data" / "memories" / "recall_eval_report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print()
        print(f"Full report saved: {report_path}")

    finally:
        reset_current_instance_id(token)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
