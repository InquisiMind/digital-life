"""Wake-up prompt rendering helpers."""

from __future__ import annotations

from typing import Any, Callable, Dict, List


_get_event_type_hook: Callable[[str], Any | None] = lambda kind: None
_consume_event_hook: Callable[[int], Any] = lambda event_id: None


def configure_runtime_hooks(
    *,
    get_event_type: Callable[[str], Any | None] | None = None,
    consume_event: Callable[[int], Any] | None = None,
) -> None:
    global _get_event_type_hook, _consume_event_hook
    if get_event_type is not None:
        _get_event_type_hook = get_event_type
    if consume_event is not None:
        _consume_event_hook = consume_event


def _render_event_summary(event: Dict[str, Any]) -> str:
    """单条事件的概要行（名称 + 描述 + 计数）。"""
    kind = event.get("kind", "")
    payload = event.get("payload", {})
    event_id = event.get("event_id")

    try:
        type_def = _get_event_type_hook(kind)
    except Exception:
        type_def = None

    display = _display_name(kind, type_def)
    description = type_def.description if type_def else ""

    merged_count = payload.get("_merged_count", 1) if isinstance(payload, dict) else 1
    count_suffix = f"（×{merged_count}）" if merged_count > 1 else ""

    parts = [f"- [#{event_id}] **{display}**{count_suffix}"]
    if description:
        parts.append(f"— {description}")
    if isinstance(payload, dict) and payload.get("nurture_kinds"):
        nurture_labels = [_nurture_label(item) for item in payload.get("nurture_kinds", [])]
        nurture_labels = [item for item in nurture_labels if item]
        if nurture_labels:
            parts.append("— " + "、".join(nurture_labels))
    return " ".join(parts)


def _display_name(kind: str, type_def: Any | None) -> str:
    fallback = {
        "message": "新消息",
        "timer": "定时器到期",
    }
    return fallback.get(kind) or (type_def.display_name if type_def else kind)


def _nurture_label(kind: str) -> str:
    return {
        "feed": "投食",
        "pet": "摸头",
        "groom": "清洁",
        "play": "玩耍",
        "praise": "夸奖",
        "comfort": "安慰",
    }.get(str(kind), "")


def format_pending_events(events: List[Dict[str, Any]]) -> str:
    """渲染待处理事件队列（统一摘要列表，不内联详情）。

    不再在唤醒 prompt 中注入事件完整明细或 YAML wake_prompt。
    事件详情统一通过 sense_events 工具返回。
    BLOCKED+单事件场景由 build_wake_prompt() 另外注入 payload 关键信息。
    """
    if not events:
        return ""

    lines = ["\n\n## 待处理事件"]

    # 按 priority 降序排
    try:
        def _prio(ev):
            t = _get_event_type_hook(ev.get("kind", ""))
            return -(t.priority if t else 5)
        sorted_events = sorted(events, key=_prio)
    except Exception:
        sorted_events = events

    shown = sorted_events[:5]
    for ev in shown:
        lines.append(_render_event_summary(ev))
    if len(sorted_events) > 5:
        lines.append(f"- ... 还有 {len(sorted_events) - 5} 条事件")

    lines.append("\n> 调用 `sense_events(event_id)` 查看明细，明细查看后该事件标记已消费。可自行决定优先级与是否处理。")
    return "\n".join(lines)


__all__ = ["configure_runtime_hooks", "format_pending_events"]
