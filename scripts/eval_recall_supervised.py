"""Feature 002 — 基于强标签(supervised)的召回准确率评测。

不靠 substring / LLM judge, 完全对照预设的 chunk_id 集:
  - precision@k   = 召回 top-k 里命中 must_hit_id 的比例
  - recall        = must_hit_id 全召出来的 case 比例
  - unwanted@k    = 召回里含 may_not_hit_id 的比例(精度反)
  - cont_continuous (multistep, 时序): 命中点是否带 must_hit 对应的 may_hit_neighbor
  - cont_derived  (multistep, 诞生链): 命中 cognition 是否带 derived_from 源经历

总指标一目了然, 彻底告别"开卷考试"评测。

用法: python3 scripts/eval_recall_supervised.py [--topk 5] [--instance eval-sandbox-001]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--topk", type=int, default=5)
    p.add_argument("--instance", default="eval-sandbox-001")
    p.add_argument("--truth", default="scripts/eval_golden_truth.json")
    args = p.parse_args()

    os.environ["DIGITAL_LIFE_INSTANCE_ID"] = args.instance
    from infrastructure.config import set_current_instance_id, reset_current_instance_id
    token = set_current_instance_id(args.instance)
    try:
        truth = json.loads(Path(args.truth).read_text(encoding="utf-8"))
        print(f"Loaded {len(truth)} supervised queries")
        print(f"top-K={args.topk}, instance={args.instance}\n")

        # 确保 unified_recall 的 dependency 一次就位
        from domain.memory.memory.recall.unified.migration import (
            backfill_slice_fields_if_needed,
        )
        backfill_slice_fields_if_needed()
        from domain.memory.memory.recall.unified.fts import ensure_fts5_schema
        ensure_fts5_schema()

        from domain.memory.memory.recall.unified import unified_recall
        from domain.memory.memory.recall.vector import _get_db

        report_rows = []
        t0 = time.time()
        for q in truth:
            # 取稍宽候选(max_total_chars 给 topk+5 的预算),再 [:topk] 截断
            # 这样 RRF 融合空间足够, 评测 topk 限定 matches 用户实际看到的 rank-k。
            res = unified_recall(q["query"], budget_kind="passive",
                                 max_total_chars=(args.topk + 5) * 200)
            topk_ids = [r.get("chunk_id", -1) for r in res[:args.topk]]
            topk_ids = [i for i in topk_ids if i >= 0]
            must = set(q["must_hit_id"])
            may_not = set(q.get("may_not_hit_id", []))
            may_nbr = set(q.get("may_hit_neighbor_id", []))
            may_der = set(q.get("may_hit_derived_id", []))

            hits_must = len(must & set(topk_ids))
            hits_unwanted = len(may_not & set(topk_ids))
            recall = hits_must / max(1, len(must))
            precision_at_k = hits_must / max(1, len(topk_ids))
            unwanted_ratio = hits_unwanted / max(1, len(topk_ids))

            # 连续性: 若 must 命中, 则看邻居是否一起被召回
            must_hit = hits_must >= 1
            neighbor_ratio = 0.0
            if may_nbr and must_hit:
                neighbor_ratio = len(may_nbr & set(topk_ids)) / len(may_nbr)
            derived_ratio = 0.0
            if may_der and must_hit:
                derived_ratio = len(may_der & set(topk_ids)) / len(may_der)

            report_rows.append({
                "theme": q["theme"], "query": q["query"][:50],
                "note": q.get("note", ""),
                "topk_ids": topk_ids,
                "must_hit_id": list(must),
                "hits_must": hits_must,
                "recall": round(recall, 3),
                "precision_at_k": round(precision_at_k, 3),
                "unwanted_ratio": round(unwanted_ratio, 3),
                "neighbor_recall": round(neighbor_ratio, 3),
                "derived_recall": round(derived_ratio, 3),
            })

        # 汇总
        n = len(report_rows)
        sup_recall = sum(r["recall"] for r in report_rows) / n
        sup_precision = sum(r["precision_at_k"] for r in report_rows) / n
        sup_unwanted = sum(r["unwanted_ratio"] for r in report_rows) / n
        # multistep: 只测 D(neighbor) / E/G(derived)
        neighbors = [r for r in report_rows if r["neighbor_recall"] > 0 or r["theme"] == "D"]
        deriveds = [r for r in report_rows if r["derived_recall"] > 0 or r["theme"] in ("E", "G")]
        avg_neighbor = sum(r["neighbor_recall"] for r in neighbors) / len(neighbors) if neighbors else 0
        avg_derived = sum(r["derived_recall"] for r in deriveds) / len(deriveds) if deriveds else 0

        elapsed = time.time() - t0
        print("=" * 78)
        print(f"SUPERVISED RECALL EVAL REPORT ({n} queries, topk={args.topk}, {elapsed:.1f}s)")
        print("=" * 78)
        print(f"{'theme':5} {'query':45} {'rec':4} {'prec@k':6} {'unwant':6} {'nbr':5} {'der':5}")
        print("-" * 78)
        for r in report_rows:
            print(f"{r['theme']:5} {r['query'][:43]:43} {r['recall']:4.2f} "
                  f"{r['precision_at_k']:6.2f} {r['unwanted_ratio']:6.2f} "
                  f"{r['neighbor_recall']:5.2f} {r['derived_recall']:5.2f}")
        print("-" * 78)
        print(f"{'AVG':5} {'':45} {sup_recall:4.2f} {sup_precision:6.2f} "
              f"{sup_unwanted:6.2f} {avg_neighbor:5.2f} {avg_derived:5.2f}")
        print()
        print(f"Overall recall        : {sup_recall:.3f}")
        print(f"Overall precision@{args.topk}   : {sup_precision:.3f}")
        print(f"Overall unwanted@{args.topk}    : {sup_unwanted:.3f} (越低越好)")
        print(f"Temporal chain recall : {avg_neighbor:.3f} (时序邻居, 主题 D)")
        print(f"Derived chain recall  : {avg_derived:.3f} (诞生链 derived_from, 主题 E/G)")

        # 解读 + 写报告
        out_path = Path("scripts") / "eval_recall_supervised_report.json"
        out_path.write_text(json.dumps({
            "n_queries": n, "topk": args.topk,
            "overall_recall": round(sup_recall, 3),
            "overall_precision_at_k": round(sup_precision, 3),
            "overall_unwanted_ratio": round(sup_unwanted, 3),
            "temporal_chain_recall": round(avg_neighbor, 3),
            "derived_chain_recall": round(avg_derived, 3),
            "per_query": report_rows,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nFull report: {out_path}")
    finally:
        reset_current_instance_id(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
