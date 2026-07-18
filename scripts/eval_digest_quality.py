"""Digest Quality Evaluator — 量化 session digest 质量, 用于离线对比老规 vs 新规。

指标 (3 大类):
  1. 体积 (token 成本)
     - chars / tokens(估算)
     - 行数

  2. 信息密度 (efficiency)
     - "工具: X" 类纯无信息 fallback 行占比 → 越低越好
     - "命令: <long cmd>" + "执行代码" 类中等噪音占比
     - 高价值产出(写文件/沉淀 lesson/形成认知/完成任务/创建待办/注册/发消息) 占比
     - 空行数(digest 只有 header)

  3. 产出可见性 (实际价值)
     - 抓到的文件路径数
     - 抓到的 lesson/insight 数
     - 抓到的认知 evolution 事件数(promote/supersede)
     - 抓到的任务完成数

用法:
  python3 scripts/eval_digest_quality.py                      # 20 条近期 session
  python3 scripts/eval_digest_quality.py --instance <name>    # 指定实例
  python3 scripts/eval_digest_quality.py --limit 50           # 条数
  python3 scripts/eval_digest_quality.py --raw                # 同时输出每条详情
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

INSTANCES = {
    "zero": "c2a5c8e8-e4f5-4c69-be3e-aac49903081d",
    "alpha": "5052c33a-e700-44dd-aea3-00e04a661ab1",
}

# 高价值产出模式(信息密度高)
HIGH_VALUE_PATTERNS = [
    (r"^写文件: ", "files_writes"),
    (r"^沉淀 (lesson|insight|inference)", "lessons"),
    (r"^(形成认知|取代认知|修订认知|聚类衍生|信号记忆)", "cognition"),
    (r"^完成任务: ", "tasks_done"),
    (r"^创建待办: ", "tasks_created"),
    (r"^(注册 (tool|skill)|登记附件)", "capability"),
    (r"^日记: ", "diary"),
    (r"^思绪\[?(daily-progress|tool-audit)", "self_status"),
    (r"^发消息: ", "messages"),
]

# 无信息密度 fallback
NOISE_PATTERNS = [
    (r"^工具: \w+($|\s×)", "tool_count"),       # "工具: terminal ×3"
    (r"^工具: \S+\s*$", "tool_unmarked"),        # "工具: recall_memory"
    (r"^命令: ", "cmd_unmarked"),                # "命令: cat xxx 前 50 chars"
    (r"^执行代码\s*$", "code_unmarked"),         # "执行代码" 空内容
    (r"^更新笔记\s*$", "scratchpad_unmarked"),   # "更新笔记"
]


def classify_line(line: str) -> tuple[str, str]:
    """一行摘要分类: 高价值 / 噪音 / 中性."""
    line = line.strip()
    if not line.startswith("·"):
        return ("header", line)
    s = line.lstrip("·").strip()
    for pat, kind in HIGH_VALUE_PATTERNS:
        if re.match(pat, s):
            return ("high_value", kind)
    for pat, kind in NOISE_PATTERNS:
        if re.match(pat, s):
            return ("noise", kind)
    return ("other", s)


def extract_outputs(detail_lines: list[str]) -> dict[str, int]:
    """从摘要行里统计关键产出。

    detail_lines 是 analyze_digest 收集的 "已剥离 · 前缀" 行(high_value/other 都收集)。
    但有时候调用方传 raw lines (含 ·), 所以这里也再保险 strip 一次。
    """
    out = {
        "files_writes": 0,
        "lessons": 0,
        "cognition": 0,
        "tasks_done": 0,
        "messages": 0,
        "diary": 0,
        "capability_registered": 0,
    }
    for r in detail_lines:
        s = r.lstrip("·").strip()
        if s.startswith("写文件: "):
            out["files_writes"] += max(s.count(","), 1)
        elif s.startswith(("沉淀 lesson", "沉淀 insight")):
            out["lessons"] += 1
        elif s.startswith(("形成认知", "取代认知", "修订认知")):
            out["cognition"] += 1
        elif s.startswith("完成任务: "):
            out["tasks_done"] += 1
        elif s.startswith("日记: "):
            out["diary"] += 1
        elif s.startswith(("注册 tool", "注册 skill", "登记附件")):
            out["capability_registered"] += 1
        elif s.startswith("发消息: "):
            out["messages"] += 1
    return out


def render_digest_new(session_db, session_id: str, old_digest: str) -> str:
    """用新规 summarize_tool_call 重生成 digest."""
    from domain.memory.memory.summaries import summarize_tool_call, dedup_tool_summaries
    msgs = session_db.get_messages(session_id)
    if not msgs:
        return old_digest
    summaries = []
    for m in msgs:
        tc = m.get("tool_calls")
        if not tc:
            continue
        calls = tc if isinstance(tc, list) else json.loads(tc or "[]")
        for c in calls:
            fn = c.get("function", {})
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments", "{}"))
            except Exception:
                args = {}
            s = summarize_tool_call(name, args)
            if s:
                summaries.append(s)
    deduped = dedup_tool_summaries(summaries)
    reason = "unknown"
    parts = session_id.split("_")
    if len(parts) >= 4 and parts[0] == "tx":
        reason = parts[1]
    start_ts = msgs[0].get("timestamp") or 0
    end_ts = msgs[-1].get("timestamp") or 0
    duration = int(end_ts - start_ts) if start_ts and end_ts else 0
    ts_str = datetime.datetime.fromtimestamp(start_ts).strftime("%m/%d %H:%M")
    header = f"[{ts_str}] {reason}, {duration}s, {len(msgs)}msgs"
    return header + "\n  " + "\n  ".join(f"· {s}" for s in deduped)


def analyze_digest(text: str) -> dict:
    """评估单个 digest 文本的质量."""
    lines = text.split("\n")
    categories = {"high_value": 0, "noise": 0, "other": 0, "header": 0}
    high_kinds = {}
    detail_lines = []
    for line in lines:
        cat, kind = classify_line(line)
        categories[cat] += 1
        if cat == "high_value":
            high_kinds[kind] = high_kinds.get(kind, 0) + 1
            detail_lines.append(line.lstrip("·").strip())
        elif cat == "other":
            detail_lines.append(line.lstrip("·").strip())
    content_lines = sum(
        1 for l in lines
        if l.strip().startswith("·")
    )
    outputs = extract_outputs(detail_lines)
    return {
        "chars": len(text),
        "tokens": round(len(text) / 3.5),
        "lines": len([l for l in lines if l.strip()]),
        "detail_lines": content_lines,
        "categories": categories,
        "high_kinds": high_kinds,
        "noise_pct": round((categories["noise"] / max(content_lines, 1)) * 100, 1) if content_lines else 0,
        "high_value_pct": round((categories["high_value"] / max(content_lines, 1)) * 100, 1) if content_lines else 0,
        "outputs": outputs,
    }


def evaluate_instance(iid: str, limit: int = 20, raw: bool = False) -> dict:
    """评估一个实例的 digest 质量, 老规/db-dump vs 新规 re-generate."""
    from infrastructure.ai.session_db import SessionDB
    session_db = SessionDB()

    db_path = PROJECT_ROOT / "apps" / iid / "data" / "memories" / "memory_layers.db"
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT id, period, digest FROM memory_layers WHERE layer='session' "
            "ORDER BY start_time DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()

    print(f"\n{'=' * 80}")
    print(f"实例: {iid} | 分析 {len(rows)} 条 session digest")
    print(f"{'=' * 80}")

    evals = []
    for r in rows:
        period = r[1]
        old_text = r[2] or ""
        new_text = render_digest_new(session_db, period, old_text)

        old_eval = analyze_digest(old_text)
        new_eval = analyze_digest(new_text)

        evals.append({
            "id": r[0],
            "period": period,
            "old": old_eval,
            "new": new_eval,
        })

        if raw:
            print(f"\n[{r[0]}] {period[:55]}")
            print(f"  OLD: {old_eval['chars']:5d}ch / {old_eval['tokens']:4d}tok | "
                  f"noise={old_eval['noise_pct']:5.1f}% high={old_eval['high_value_pct']:5.1f}% | "
                  f"files={old_eval['outputs']['files_writes']} lessons={old_eval['outputs']['lessons']}")
            print(f"  NEW: {new_eval['chars']:5d}ch / {new_eval['tokens']:4d}tok | "
                  f"noise={new_eval['noise_pct']:5.1f}% high={new_eval['high_value_pct']:5.1f}% | "
                  f"files={new_eval['outputs']['files_writes']} lessons={new_eval['outputs']['lessons']}")

    # 聚合统计
    n = len(evals)
    if n == 0:
        print("  (无数据)")
        return {}

    total_old_chars = sum(e["old"]["chars"] for e in evals)
    total_new_chars = sum(e["new"]["chars"] for e in evals)
    total_old_noise = sum(e["old"]["categories"]["noise"] for e in evals)
    total_new_noise = sum(e["new"]["categories"]["noise"] for e in evals)
    total_old_high = sum(e["old"]["categories"]["high_value"] for e in evals)
    total_new_high = sum(e["new"]["categories"]["high_value"] for e in evals)
    total_old_lines = sum(e["old"]["detail_lines"] for e in evals)
    total_new_lines = sum(e["new"]["detail_lines"] for e in evals)

    # 产出累计
    prod_keys = ["files_writes", "lessons", "cognition", "tasks_done",
                 "messages", "diary", "capability_registered"]
    old_prod = {k: sum(e["old"]["outputs"][k] for e in evals) for k in prod_keys}
    new_prod = {k: sum(e["new"]["outputs"][k] for e in evals) for k in prod_keys}

    print(f"\n{'─' * 80}")
    print(f"汇总 ({n} 条 session)")
    print(f"{'─' * 80}\n")
    print(f"{'指标':35s} {'老规':>10s} {'新规':>10s} {'差':>10s}")
    print(f"{'─' * 65}")
    print(f"{'体积 (chars)':35s} {total_old_chars:>10d} {total_new_chars:>10d} {total_new_chars-total_old_chars:>+10d}")
    print(f"{'体积 (tokens, /3.5)':35s} {round(total_old_chars/3.5):>10d} {round(total_new_chars/3.5):>10d} {round((total_new_chars-total_old_chars)/3.5):>+10d}")
    print(f"{'摘要行数':35s} {total_old_lines:>10d} {total_new_lines:>10d} {total_new_lines-total_old_lines:>+10d}")
    print(f"{'噪音行(无信息)':35s} {total_old_noise:>10d} {total_new_noise:>10d} {total_new_noise-total_old_noise:>+10d}")
    print(f"{'高价值产出行':35s} {total_old_high:>10d} {total_new_high:>10d} {total_new_high-total_old_high:>+10d}")
    print(f"{'噪音占比':35s} {round(total_old_noise/max(total_old_lines,1)*100):>9.1f}% {round(total_new_noise/max(total_new_lines,1)*100):>9.1f}%")
    print(f"{'高价值占比':35s} {round(total_old_high/max(total_old_lines,1)*100):>9.1f}% {round(total_new_high/max(total_new_lines,1)*100):>9.1f}%")
    print(f"\n产出可见性:")
    for k in prod_keys:
        d = new_prod[k] - old_prod[k]
        print(f"  {k:24s} {old_prod[k]:>4d} → {new_prod[k]:>4d} ({d:+d})")

    # 每 wake 注入 3 条 → 单 wake 节省估算
    if n >= 3:
        avg_old = total_old_chars / n
        avg_new = total_new_chars / n
        delta_per_wake = (avg_old - avg_new) * 3
        print(f"\n按 wake 注入最近 3 条估算:")
        print(f"  老/新 avg/session: {avg_old:.0f} / {avg_new:.0f} chars")
        print(f"  wake payload 节省/注入: ~{delta_per_wake:.0f} chars ≈ {round(delta_per_wake/3.5)} tok/wake")

    return {
        "instance": iid,
        "n_sessions": n,
        "old_chars": total_old_chars,
        "new_chars": total_new_chars,
        "savings_per_wake": (total_old_chars - total_new_chars) / n * 3 if n else 0,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instance", default=None)
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--raw", action="store_true", help="显示每条详情")
    args = ap.parse_args()

    if args.instance:
        targets = {args.instance: INSTANCES.get(args.instance, args.instance)}
    else:
        targets = INSTANCES

    results = []
    for name, iid in targets.items():
        import infrastructure.config as cfg
        cfg.set_current_instance_id(iid)
        r = evaluate_instance(iid, limit=args.limit, raw=args.raw)
        if r:
            r["name"] = name
            results.append(r)

    if len(results) > 1:
        print(f"\n\n{'=' * 80}")
        print(f"两实例汇总")
        print(f"{'=' * 80}")
        for r in results:
            print(f"  {r['name']:6s}: {r['n_sessions']:3d} sessions, "
                  f"老规 {r['old_chars']:6d}chars → 新规 {r['new_chars']:6d}chars, "
                  f"wake 节省 ≈ {round(r['savings_per_wake']/3.5):4d} tok")


if __name__ == "__main__":
    main()
