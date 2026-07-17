"""Memory summary extraction helpers."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Dict, List, Optional


IGNORE_TOOLS = frozenset(
    {
        "sense_vitals",
        "sense_time",
        "sense_self",
        "sense_wake_reason",
        "sense_event_queue",
        "sense_event_detail",
        "sense_memory",
        "sense_scratchpad",
        "sense_goals",
        "sense_daily",
        "sense_todos",
        "sense_sent_log",
        "rest",
    }
)


def summarize_tool_call(name: str, args: Dict[str, Any]) -> str:
    """Compress a tool call into one readable summary line."""
    if name in IGNORE_TOOLS:
        return ""

    if name == "write_diary":
        first_line = args.get("text", "").split("\n")[0][:60].strip()
        return f"日记: {first_line}"

    if name == "express_to_human":
        text = args.get("text", "")[:60].strip()
        return f"发消息: {text}"

    if name == "record_thought":
        text = args.get("text", "")
        tag = args.get("tag", "")
        first_line = text.split("\n")[0][:60].strip()
        return f"思绪[{tag}]: {first_line}" if tag else f"思绪: {first_line}"

    if name == "update_scratchpad":
        return "更新笔记"

    # 注:manage_goals/manage_plan/manage_daily/manage_work/sense_* 等
    # 工具已于 2026-07-17 退役(handler 保留可 dispatch, schema 不再注入)。
    # 这里删除它们的历史 summary 分支, 让它们走通用 fallback——避免后人误以为还在用。
    # 退役工具的 args 多含 _deprecated_hint,自然在通用字符截断里投机出现。

    return f"工具: {name}"


def extract_energy(text: str) -> Optional[float]:
    match = re.search(r"精力([\d.]+)", text)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            pass
    return None


def dedup_tool_summaries(summaries: List[str]) -> List[str]:
    """Deduplicate adjacent summaries and merge their counts."""
    if not summaries:
        return []
    result = []
    previous = None
    count = 0
    for summary in summaries:
        prefix = re.sub(r"^(日记|思绪|发消息|工具|更新笔记|目标|每日计划|规划|完成任务|追加任务|计划|工作)(\[.*?\])?:\s*", "", summary)
        if prefix == previous:
            count += 1
        else:
            if previous and count > 1:
                result[-1] = result[-1].replace(previous, f"{previous} ×{count}", 1)
            result.append(summary)
            previous = prefix
            count = 1
    if previous and count > 1:
        result[-1] = result[-1].replace(previous, f"{previous} ×{count}", 1)
    return result


def format_session_digest(digest: Dict[str, Any]) -> str:
    lines = [
        f"[{digest['time']}] {digest['wake_reason']}, "
        f"{digest['duration_sec']}s, {digest['message_count']}msgs"
    ]
    for summary in digest["tool_summary"]:
        lines.append(f"  · {summary}")
    if digest["energy_range"]:
        energy_start, energy_end = digest["energy_range"]
        lines.append(f"  {energy_start:.0f} → {energy_end:.0f}")
    lines.append(f"  结束: {digest['end_reason']}")
    return "\n".join(lines)


def extract_topics(text: str) -> List[str]:
    phrases = re.findall(r"[\u4e00-\u9fff]{2,6}", text)
    boring = {
        "精力",
        "消息",
        "用户",
        "笔记",
        "日记",
        "思绪",
        "结束",
        "唤醒",
        "自然醒",
        "主动休息",
        "完成",
        "继续",
        "消耗",
        "降下",
        "不够",
        "自己",
    }
    meaningful = [phrase for phrase in phrases if phrase not in boring and len(phrase) >= 2]
    counts = Counter(meaningful)
    return [phrase for phrase, count in counts.most_common(5) if count >= 2]


__all__ = [
    "IGNORE_TOOLS",
    "dedup_tool_summaries",
    "extract_energy",
    "extract_topics",
    "format_session_digest",
    "summarize_tool_call",
]
