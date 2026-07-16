"""Feature 002 统一记忆体系 — 召回精度评估 (LLM judge)。

召回率:已由 scripts/eval_memory_recall.py 提供(命中是否含期望字串);
精度:本脚本补上,用 LLM 把"召回的内容 vs query 真实需求"判定相关度,
衡量"召回但无关"的占比。

设计:
  对每个 eval case(query + expected_snippet):
    1. unified_recall(query) 拿到 top-N 切片
    2. 对每条切片,LLM judge 判 0/1/2 评分:
       0 = 无关 / 1 = 边缘相关 / 2 = 直接相关
    3. precision@k = (相关切片数) / k
    4. 异常召回 = recall>0 但 precision 低 → "召回但带了一堆垃圾"

判定 prompt(中文友好):
  - 给 query + 切片 text
  - 让模型只回 0 / 1 / 2 三档
  - 同一 batch 多切片一起问节省 api 调用

用法:
  python3 scripts/eval_precision.py [instance_id] [--sample 30] [--topk 5]
  默认: instance=c2a5c8e8-e4f5-4c69-be3e-aac49903081d,sample=30,topk=5,
        base on scripts/eval_memory_recall.py 的 eval cases (= 同一批 100,
        random sample 30)。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def judge_relevance_batch(
    query: str,
    snippets: list[dict[str, Any]],
    *,
    timeout: float = 90,
) -> list[int]:
    """调 LLM judge 判一组 snippet 各自与 query 的相关度(0/1/2)。
    返回与 snippets 等长的 list[int]。
    失败时全部返回 -1(不计入精度)。
    """
    try:
        from infrastructure.ai.llm import call_llm
    except Exception:
        return [-1] * len(snippets)

    if not snippets:
        return []

    # 把所有 snippet 拼成一个 表格 prompt 让 model 一次审完
    lines = ["请审阅下面 N 段记忆与给定查询的相关度,逐条返回 0/1/2 三档评分:",
             "  0 = 无关或离题",
             "  1 = 边缘相关(主题对但内容泛泛)",
             "  2 = 直接相关(明确回答或紧贴 query)",
             "",
             f"查询: {query}",
             "", "记忆片段:"]
    for i, snip in enumerate(snippets, 1):
        text = (snip.get("text") or "").strip().replace("\n", " ")[:200]
        lines.append(f"[{i}] {text}")
    lines += ["", "请严格按格式: 只输出 N 个数字,空格分隔(如: 2 1 0 2 1)。不要其他文字。"]
    prompt = "\n".join(lines)

    system = "你是一个严谨的记忆相关性审阅者,只输出数字、不输出别的。"
    try:
        resp = call_llm(prompt, system_prompt=system, timeout=timeout)
        # 解析:取所有数字
        scores: list[int] = []
        for tok in resp.replace(",", " ").split():
            digits = "".join(c for c in tok if c.isdigit())
            if digits:
                v = int(digits)
                if 0 <= v <= 2:
                    scores.append(v)
                    if len(scores) >= len(snippets):
                        break
        # 如果数字数量不匹配,把缺的补 -1
        while len(scores) < len(snippets):
            scores.append(-1)
        return scores[:len(snippets)]
    except Exception as exc:
        # 不阻断:记 -1 表示未判
        print(f"  [judge-fail] {exc}", file=sys.stderr)
        return [-1] * len(snippets)


def eval_precision_on_cases(
    cases: list[dict],
    *,
    topk: int = 5,
    instance_id: str = "",
) -> dict[str, Any]:
    """跑每个 case, 收集 LLM judge scores → 算 precision@topk 各指标。"""
    from domain.memory.memory.recall.unified import unified_recall
    eval_data: list[dict] = []
    t0 = time.time()
    skipped = 0

    for i, case in enumerate(cases):
        query = case["query_text"]
        try:
            results = unified_recall(
                query,
                attention_tokens=case["expected_entities"],
                budget_kind="passive",
                max_total_chars=topk * 200,  # 限制 topk 条
            )
        except Exception as exc:
            print(f"  [{i}] unified_recall error: {exc}", file=sys.stderr)
            skipped += 1
            continue

        snippet_pool = results[:topk]
        if not snippet_pool:
            eval_data.append({
                "case_id": i,
                "query": query[:60],
                "snippet_count": 0,
                "scores": [],
                "precision": 0.0,
            })
            continue

        scores = judge_relevance_batch(query, snippet_pool)
        precision = sum(1 for s in scores if s >= 1) / len(scores)
        eval_data.append({
            "case_id": i,
            "query": query[:60],
            "snippet_count": len(snippet_pool),
            "scores": scores,
            "precision": round(precision, 3),
        })
        if (i + 1) % 5 == 0:
            print(f"  {i+1}/{len(cases)} done ({time.time()-t0:.1f}s)")

    # 汇总
    judged = [d for d in eval_data if d["snippet_count"] > 0]
    judged_with_scores = [d for d in judged if any(s >= 0 for s in d["scores"])]
    avg_precision = (
        sum(d["precision"] for d in judged_with_scores) / len(judged_with_scores)
        if judged_with_scores else 0.0
    )
    # "直接相关"命中率(0/1/2 算命中时要 >=2 才真的有用)
    strict = sum(d["precision"] for d in judged_with_scores
                 if sum(1 for s in d["scores"] if s == 2) >= 1)
    # 召回来的有多少带"无关垃圾"(scores 含 0)
    noise_cases = [d for d in judged_with_scores if 0 in d["scores"]]
    precision_of_strict = (
        strict / len(judged_with_scores) if judged_with_scores else 0.0
    )

    return {
        "instance_id": instance_id,
        "n_cases": len(cases),
        "n_judged": len(judged_with_scores),
        "n_skipped": skipped,
        "topk": topk,
        "avg_precision_at_k": round(avg_precision, 3),
        "strict_precision_pct": round(precision_of_strict, 3),
        "avg_noise_ratio": round(
            sum(1 for d in judged_with_scores if 0 in d["scores"]) /
            max(1, len(judged_with_scores)), 3
        ),
        "elapsed_s": round(time.time() - t0, 1),
        "details": eval_data,
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("instance_id", nargs="?",
                   default="c2a5c8e8-e4f5-4c69-be3e-aac49903081d")
    p.add_argument("--sample", type=int, default=30,
                   help="从 100 个 eval case 抽多少跑本次精度评估")
    p.add_argument("--topk", type=int, default=5,
                   help="每个 query 看前 K 条切片的精度")
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()

    os.environ["DIGITAL_LIFE_INSTANCE_ID"] = args.instance_id

    instance_dir = ROOT / "apps" / args.instance_id
    entity_index_path = instance_dir / "data" / "memories" / "entity_index.json"
    if not entity_index_path.exists():
        print(f"entity_index.json not found: {entity_index_path}")
        return 1

    try:
        from infrastructure.config import set_current_instance_id, reset_current_instance_id
        token = set_current_instance_id(args.instance_id)
    except Exception:
        token = None

    try:
        # 复用 eval_memory_recall.build_eval_cases
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "eval_memory_recall", str(ROOT / "scripts" / "eval_memory_recall.py")
        )
        em = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(em)

        print(f"Building eval cases from {args.instance_id[:8]}...")
        all_cases = em.build_eval_cases(entity_index_path, max_cases=100)
        random.seed(args.seed)
        random.shuffle(all_cases)
        sample = all_cases[:args.sample]
        print(f"抽 {args.sample} case(共 {len(all_cases)}), topk={args.topk}")
        print()

        # 确保 FTS5/baseline 单进程一次就位(避免首次调用慢)
        from domain.memory.memory.recall.unified.fts import ensure_fts5_schema, rebuild_fts_index
        ensure_fts5_schema()
        from domain.memory.memory.recall.unified.migration import backfill_slice_fields_if_needed
        backfill_slice_fields_if_needed()
        rebuild_fts_index()

        report = eval_precision_on_cases(
            sample, topk=args.topk, instance_id=args.instance_id
        )

        print()
        print("=" * 75)
        print(f"PRECISION EVAL REPORT ({report['n_cases']} cases, "
              f"{report['n_judged']} judged, {report['n_skipped']} skipped, "
              f"{report['elapsed_s']:.1f}s)")
        print("=" * 75)
        print(f"top-K               : {report['topk']}")
        print(f"avg precision @k    : {report['avg_precision_at_k']:.3f} "
              f"(每 case 召回 top{report['topk']} 里 1+2 档的平均占比)")
        print(f"strict precision pct: {report['strict_precision_pct']:.3f} "
              f"(case 中至少有 1 条直接相关的比例)")
        print(f"avg noise ratio     : {report['avg_noise_ratio']:.3f} "
              f"(召回里含 0 档无关的比例 — 越低越好)")
        print()

        # 输出个最差 precision 的 case 给人看
        judged = [d for d in report["details"] if d["snippet_count"] > 0]
        judged.sort(key=lambda x: x["precision"])
        if judged:
            print("最差 5 case (高噪声/低相关):")
            for d in judged[:5]:
                print(f"  case {d['case_id']:3d} prec={d['precision']:.2f} q={d['query']!r:55s} scores={d['scores']}")
        print()
        # 高精度示例
        good = [d for d in judged if d["precision"] >= 1.0]
        if good:
            print("全 precision=1.0 (召回极高):")
            for d in good[:3]:
                print(f"  case {d['case_id']:3d} q={d['query']!r:55s} scores={d['scores']}")

        out_path = instance_dir / "data" / "memories" / "precision_eval_report.json"
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        print(f"\nFull report: {out_path}")
    finally:
        if token is not None:
            try:
                reset_current_instance_id(token)
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
