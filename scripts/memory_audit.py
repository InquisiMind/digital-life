"""Feature 002 — 记忆切片垃圾清单 dry-run 扫描器。

默认 dry-run(只输出报告不动数据); --apply 才真清。
所有"删"操作都不真删 —— 改 cognition_state='archived' (设计 §6.4 永不硬删),
让 facade / monitor 仍能备案查询、但默认召回不返。

用法:
  python3 scripts/memory_audit.py <instance_id>               # dry-run 报告
  python3 scripts/memory_audit.py <instance_id> --apply       # 真打 archived
  python3 scripts/memory_audit.py <instance_id> --rule wake_template  # 只跑某规则
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]


# ────────── 噪音识别规则 ──────────
# 每条规则: (name, predicate(text) → bool, severity, reason_template)
#
# v2 收紧(2026-07-17): v1 单 keyword 命中误伤
# 例：「辩论收束产出六项优先级表 P1 wake_brief」 → 真 lesson, 被 'wake_brief'
# 误命。v2 用 multi-feature: 必须 2+ 个 wake 模板特征同时出现才认。

_WAKE_TEMPLATE_FEATURES = [
    re.compile(r"── ↓ 当下事件 ↓ ──"),
    re.compile(r"### 唤醒原因"),
    re.compile(r"## 记忆体检"),
    re.compile(r"⏰ 当前时间：\d"),
    re.compile(r"按优先级排列（仅标题"),
    re.compile(r"⏰ 你设的闹钟响了"),
    re.compile(r"上一轮留下的备注："),
    re.compile(r"^\[user\] ## ── ↓ 当下事件 ↓ ──", re.MULTILINE),
]
_TOOL_RESULT = re.compile(
    r"^\s*\[\s*\{.*\}\s*\]|^\s*\{.*\"id\".*\}$|tool_result|"
    r"\"memory_id\".*\"memory_type\"|sense_event_detail 返回"
)
_STATUS_REPORT = re.compile(
    r"^\[(status|trading_wait|system_wait|final_status|整理)\]|"
    r"## 记忆体检|^【整理】|能量=[\d\.]+.*精力=[\d\.]+"
)
_SHORT_LINE = re.compile(r"^.{0,30}$")  # <30 字符的疑似短空话
_PLACEHOLDER_OK = re.compile(r"^(好的|明白|收到|嗯|了解|ok|OK|好的！|了解。)$")


def is_wake_template(text: str) -> bool:
    """v2 严格: wake prompt 模板特征必须 2+ 个同时命中才算。
    单出现 'wake_brief' / 'wake_reason' 这种字面词不算(故意)。
    """
    if not text:
        return False
    hits = sum(1 for p in _WAKE_TEMPLATE_FEATURES if p.search(text))
    return hits >= 2


# v3 新增: 4 类已被抽样证实的垃圾识别规则 (2026-07-17)
_DIGEST_DAY_DUMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}:\s*\|\s*.*\|.*精力\d+", re.MULTILINE
)
_WORK_CHECKLIST = re.compile(
    r"- \[[x ]\] .*\| 来源:\s*(self|用户|自己)\s*\| 创建:\d{4}-\d{2}-\d{2}"
)
_HIM_META_PREFIX = re.compile(
    # him 文件里大量时间戳行开头但无实质内容
    r"^\d{1,2}:\d{2}:\d{2}\+\d{2}:\d{2}\s*↵\s*$|^.{0,5}↵\s*$",
    re.MULTILINE,
)
_KG_TEST_RESIDUE = re.compile(
    r"^(修订后的更准确判断|higher level cognition v\d|test_(promote|e2e|recurring))",
    re.IGNORECASE,
)


def is_digest_day_dump(text: str) -> bool:
    """digest_day chunk 是关键词 dump, 非句子 (39-50 字)。"""
    if not text or len(text) > 80:
        return False
    return bool(_DIGEST_DAY_DUMP.search(text))


def is_work_checklist(text: str) -> bool:
    """work.md / notes 里 '已完成清单' 流水 — 时间戳 + 标签, 信息密度极低。"""
    s = text or ""
    matches = _WORK_CHECKLIST.findall(s)
    return len(matches) >= 3  # 至少 3 行这种格式才算


def is_test_residue(text: str) -> bool:
    """测试残留(我们之前 E2E/cognition 测遗留的 fake cognition)。"""
    s = (text or "").strip()
    return bool(_KG_TEST_RESIDUE.match(s))


def is_tool_result(text: str) -> bool:
    """工具返回值 / JSON 片段被当对话。"""
    head = (text or "").strip()[:200]
    return bool(_TOOL_RESULT.search(head))


def is_status_report(text: str) -> bool:
    """意识流状态报告型, 非真实对话。"""
    head = (text or "").strip()[:120]
    return bool(_STATUS_REPORT.search(head))


def is_too_short(text: str) -> bool:
    """超短无意义文本("好的" / "嗯" 等)。"""
    s = (text or "").strip()
    return bool(_SHORT_LINE.match(s)) and bool(_PLACEHOLDER_OK.match(s))


def is_low_value_junk(text: str) -> bool:
    """通用无意义:text 几乎全是符号 / 标点 / 大量重复字符。"""
    s = (text or "")
    if not s.strip():
        return True
    # 50%+ 是非字符 / 空白 / 仅标点
    non_text = sum(1 for c in s if not c.isalnum() and not c.isspace())
    is_cjk = sum(1 for c in s if '\u4e00' <= c <= '\u9fff')
    if is_cjk < 3 and non_text > len(s) * 0.5 and len(s) < 60:
        return True
    return False


RULES: list[tuple[str, Callable[[str], bool], str, str]] = [
    # name, predicate, severity (P0/P1/P2), reason
    ("wake_template",   is_wake_template,   "P0", "wake prompt 模板被误当 conversation"),
    ("tool_result",     is_tool_result,     "P0", "工具返回 / JSON 片段, 非真实对话"),
    ("test_residue",    is_test_residue,    "P0", "测试残留 fake cognition (E2E/单测遗留)"),
    ("digest_day_dump", is_digest_day_dump, "P1", "digest_day 关键词 dump, 无实质句"),
    ("work_checklist",  is_work_checklist,  "P1", "work/notes 流水清单(时间戳+标签), 信息密度极低"),
    ("status_report",   is_status_report,   "P1", "意识流状态报告/【整理】标签"),
    ("placeholder_ok",  is_too_short,       "P2", "超短占位回复(好的/明白/收到)"),
    ("low_value_junk",  is_low_value_junk,  "P2", "几乎全标点符号 / 无信息"),
]


def audit_chunks(
    *,
    instance_id: str,
    apply: bool = False,
    only_rule: str | None = None,
) -> dict:
    """扫该实例 chunks, 返回报告 + (可选)真 archive 命中的 chunk。
    """
    os.environ["DIGITAL_LIFE_INSTANCE_ID"] = instance_id

    from infrastructure.config import set_current_instance_id, reset_current_instance_id
    token = set_current_instance_id(instance_id)
    try:
        from domain.memory.memory.recall.vector import _get_db
        db = _get_db()
        try:
            rows = db.execute(
                "SELECT id, source, source_kind, phase, cognition_state, "
                "       substr(text, 1, 500) as text, "
                "       length(text) as text_len, "
                "       freshness, activation, created_at "
                "FROM chunks"
            ).fetchall()

            findings: dict[str, list[dict]] = {r[0]: [] for r in RULES}
            archived_ids: list[int] = []
            for row in rows:
                # 已 archived / replaced 不再判
                if row["cognition_state"] in ("archived", "replaced"):
                    continue
                text = row["text"] or ""
                for rule_name, pred, _, _ in RULES:
                    if only_rule and rule_name != only_rule:
                        continue
                    if pred(text):
                        findings[rule_name].append({
                            "chunk_id": row["id"],
                            "source": row["source"],
                            "preview": text[:100].replace("\n", " "),
                            "text_len": row["text_len"],
                            "phase": row["phase"],
                        })
                        archived_ids.append(row["id"])
                        break  # 一条只命中一种就够, 避免重复
            # dedupe archived_ids
            archived_ids = sorted(set(archived_ids))

            # 真打 archive
            applied_count = 0
            if apply and archived_ids:
                placeholders = ",".join("?" * len(archived_ids))
                cur = db.execute(
                    f"UPDATE chunks SET cognition_state='archived', freshness=0.0 "
                    f"WHERE id IN ({placeholders}) AND cognition_state IS NULL",
                    archived_ids,
                )
                applied_count = cur.rowcount or 0
                db.commit()

            # 汇总
            total = len(rows)
            active = sum(
                1 for r in rows
                if r["cognition_state"] not in ("archived", "replaced")
            )
            return {
                "instance_id": instance_id,
                "total_chunks": total,
                "active_chunks": active,
                "noise_candidates": {k: len(v) for k, v in findings.items()},
                "samples": {
                    name: items[:3] for name, items in findings.items()
                },
                "apply": apply,
                "apply_archived_n": applied_count,
                "would_archive_n": 0 if apply else len(archived_ids),
                "archived_ids_preview": [str(i) for i in archived_ids[:20]],
            }
        finally:
            db.close()
    finally:
        reset_current_instance_id(token)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("instance_id")
    p.add_argument("--apply", action="store_true",
                   help="真打 archive(默认 dry-run)")
    p.add_argument("--rule", default=None,
                   help=f"只跑某规则: {','.join(r[0] for r in RULES)}")
    p.add_argument("--report", default=None,
                   help="可选: 报告写到该 json 路径")
    args = p.parse_args()

    report = audit_chunks(
        instance_id=args.instance_id,
        apply=args.apply,
        only_rule=args.rule,
    )
    # 控制台打印
    print(f"════════════════════════════════════════════════════════════")
    print(f"   记忆切片垃圾清单 ({'APPLY' if args.apply else 'DRY-RUN'})")
    print(f"   实例: {args.instance_id[:8]}")
    print(f"════════════════════════════════════════════════════════════")
    print(f"总切片数: {report['total_chunks']}  ·  active: {report['active_chunks']}")
    print(f"{'=' * 60}")
    print(f"按规则命中数(去重, 同一 chunk 命中一条就停):")
    for r in RULES:
        name = r[0]
        sev = r[2]
        reason = r[3]
        n = report["noise_candidates"].get(name, 0)
        flag = "🟥" if sev == "P0" and n > 0 else "🟧" if sev == "P1" and n > 0 else ("🟨" if n > 0 else "  ")
        print(f"  {flag} [{sev}] {name:18s} n={n:4d}  — {reason}")

    total_noise = sum(report["noise_candidates"].values())
    print(f"{'=' * 60}")
    print(f"总可清理候选: {total_noise} ({total_noise / max(1, report['active_chunks']):.1%} of active)")
    if args.apply:
        print(f"已 archive: {report['apply_archived_n']} 条(cognition_state='archived', 不硬删)")
    else:
        print(f"(dry-run)如 --apply, 会真把 {report['would_archive_n']} 条标 archived")

    print(f"\n样本预览(每规则 前 3 条):")
    for r in RULES:
        name = r[0]
        samples = report["samples"].get(name, [])
        if not samples:
            continue
        print(f"\n[{name}] ({len(samples)} shown)")
        for s in samples:
            print(f"  #{s['chunk_id']:5} src={s['source']:14s} len={s['text_len']:5} "
                  f"phase={s['phase']:10s}:  {s['preview']!r}")

    # 写报告
    report_path = args.report or (
        ROOT / "apps" / args.instance_id / "data" / "memories" / "memory_audit_report.json"
    )
    Path(report_path).parent.mkdir(parents=True, exist_ok=True)
    Path(report_path).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nFull report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
