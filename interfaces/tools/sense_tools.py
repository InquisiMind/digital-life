"""感官工具集 (Senses) — Agent 主动感知世界与自我。

工具分类：
  状态感知：
    - sense_vitals: 当前精力状态（energy + segment）
    - sense_time: 当前时间与时段（清晨/午后/深夜），含作息建议
    - sense_wake_reason: 为什么醒了（RAS 信号过滤结果）

  事件感知：
    - sense_event_queue: 查看待处理事件摘要（只读不消费）
    - sense_event_detail: 查看单个事件完整明细（查看后标记已消费）

  自我感知：
    - sense_self: 意识残留 + 最近 session 摘要 + 自我认知档案
    - sense_self_knowledge: 读自我认知档案
    - sense_rules: 当前长期行为规则
    - sense_context: 交接上下文
    - sense_lessons: 经验教训

  记忆感知：
    - sense_memory: 长期记忆（关于他/日记/草稿本）
    - recall_memory: 语义搜索历史经历
    - sense_entity: 按实体名查找关联记忆

  工作感知：
    - sense_work: 工作看板
    - sense_goals: 目标列表
    - sense_daily: 每日计划（含定时事件）
    - sense_plans: 长期计划与里程碑

每个感知调用消耗少量精力（_burn(0.3)），防止模型无节制轮询。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Dict, List

from domain.vital.simulation import get_engine
from domain.memory.memory.consciousness.runtime import (
    read_recent_diary,
    read_about_him,
    read_scratchpad,
    read_goals,
    read_daily,
    read_rules,
    read_plans,
    read_context,
    read_lessons,
    read_insights,
)
from domain.lifecycle.affairs.runtime import get_nurture_log

from interfaces.tools.registry import registry


def _j(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _burn(amount: float = 0.3):
    """每个感知调用消耗少量精力。"""
    try:
        from domain.vital import consume_energy
        consume_energy(amount, reason="sense")
    except Exception:
        pass


# 事件类型的处理指导
_EVENT_HINTS = {
    "message": {
        "label": "📩 新消息",
        "description": "用户发来了消息",
        "action_hint": "可以用 express_to_human 回复，或者继续当前任务稍后回复",
    },
    "vital_threshold": {
        "label": "⚠️ 身体提醒",
        "description": "某个生命维度跨越了阈值",
        "action_hint": "检查 sense_vitals 看具体情况，必要时处理",
    },
    "initiative": {
        "label": "⚡ 主动探索",
        "description": "精力充足时主动寻找任务或探索新方向",
        "action_hint": "查看任务列表，挑一件想做的事推进",
    },
    "timer": {
        "label": "⏰ 定时器",
        "description": "你设置的闹钟到了",
        "action_hint": "处理定时任务，或稍后处理",
    },
    "task_reminder": {
        "label": "📋 任务提醒",
        "description": "有任务需要关注",
        "action_hint": "检查 sense_work 看具体任务，或继续当前任务",
    },
}


def _peek_pending_summary(limit: int = 5) -> Dict[str, Any]:
    """偷看待处理事件队列（不消费），返回简短清单。

    清单只包含：event_id, kind, display_name, priority, at。
    不返回 payload 内容/preview——查看明细+消费请用 sense_event_detail(event_id)。
    """
    try:
        from domain.lifecycle.events import pop_due_events
        from domain.lifecycle.event_registry import get_event_type

        # pop_due_events 名字误导，实际只读不消费
        events = pop_due_events(limit=20)

        if not events:
            return {"count": 0, "events": []}

        total = len(events)
        sliced = events[:limit]
        result_events = []

        for ev in sliced:
            kind = ev.get("kind", "")
            type_def = get_event_type(kind)
            display_name = type_def.display_name if type_def else kind
            priority = type_def.priority if type_def else 5

            entry = {
                "event_id": ev.get("event_id"),
                "kind": kind,
                "display_name": display_name,
                "priority": priority,
                "at": ev.get("created_at") or ev.get("at", ""),
            }
            payload = ev.get("payload", {}) if isinstance(ev.get("payload"), dict) else {}
            merged_count = payload.get("_merged_count", 1)
            if merged_count > 1:
                entry["merged_count"] = merged_count
            result_events.append(entry)

        out = {
            "count": total,
            "events": result_events,
            "hint": "用 sense_event_detail(event_id) 查看明细并消费该事件。",
        }
        if total > limit:
            out["truncated"] = total - limit
        return out
    except Exception as exc:
        logger.debug("peek pending summary failed: %s", exc)
        return {"count": 0, "events": []}


# ──────────────────────────────── sense_event_queue / sense_event_detail ────────────────────────────────

def _handle_sense_event_queue(args: Dict[str, Any], **_) -> str:
    """查看事件队列摘要（不消费）。"""
    _burn()
    limit = int(args.get("limit") or 5)
    return _j(_peek_pending_summary(limit=limit))


registry.register(
    name="sense_event_queue",
    toolset="senses",
    schema={
        "name": "sense_event_queue",
        "description": "查看待处理事件队列摘要（不消费）。多事件时只显示名称+描述，单事件时附带 preview。要看具体内容请调用 sense_event_detail。",
        "parameters": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "最多返回多少条，默认5"}},
        },
    },
    handler=_handle_sense_event_queue,
    check_fn=lambda: True,
    emoji="📋",
)


def _handle_sense_event_detail(args: Dict[str, Any], **kwargs) -> str:
    """查看单个事件的完整明细，查看后标记为已消费。

    这是事件系统的唯一生产消费入口：
    - 调用后该事件从队列中移除（consumed_at + consumed_by_session_id 被写入 DB）
    - session_id 从 kwargs 中获取（由 dispatch 层传入），用于日历聚合展示
    - 返回事件的完整 payload，供模型决策如何响应
    """
    _burn()
    event_id = args.get("event_id")
    if not event_id:
        return _j({"error": "缺少 event_id。先调用 sense_event_queue 获取列表。"})
    try:
        event_id = int(event_id)
    except (TypeError, ValueError):
        return _j({"error": f"event_id 必须是整数，收到 {event_id!r}"})

    try:
        from domain.lifecycle.events import pop_due_events, consume_event, list_recent_events
        from domain.lifecycle.event_registry import get_event_type

        events = pop_due_events(limit=100)
        match = next((ev for ev in events if ev.get("event_id") == event_id), None)
        already_consumed = False
        if not match:
            # 两种可能:
            # (1) 单事件 wake 在 prompt 阶段已直接消费(本就设计如此) → 历史里能查到
            # (2) 上次 wake 是多事件清单 → 这次应当能在 due 队列里找到。
            #     如果找不到, 可能是上轮被 auto-consume(老路径) 或事件太老(>2h)
            recent = list_recent_events(hours=2, include_consumed=True, limit=200)
            match = next((ev for ev in recent if ev.get("event_id") == event_id), None)
            if not match:
                return _j({
                    "event_id": event_id,
                    "error": f"事件 {event_id} 不存在或已超过 2 小时窗口（事件清理）",
                    "consumed": True,
                })
            already_consumed = bool(match.get("consumed_at"))

        kind = match.get("kind", "")
        type_def = get_event_type(kind)
        payload = match.get("payload", {})

        if not already_consumed:
            try:
                consume_event(event_id, session_id=kwargs.get("session_id"))
            except Exception:
                pass

        return _j({
            "event_id": event_id,
            "kind": kind,
            "display_name": type_def.display_name if type_def else kind,
            "description": type_def.description if type_def else "",
            "priority": type_def.priority if type_def else 5,
            "payload": payload,
            "created_at": match.get("created_at", ""),
            "fire_at": match.get("fire_at"),
            "consumed": True,
            "auto_consumed_before_session": already_consumed,
            "wake_prompt": type_def.prompt_template if type_def else "",
        })
    except Exception as exc:
        return _j({"error": f"获取事件明细失败: {exc}"})


def _handle_sense_schedule(args: Dict[str, Any], **kwargs) -> str:
    """查看当前日程：所有未触发的闹钟 + 每日作息。

    用途：设闹钟前先看看已经设了什么，避免重复；或主动了解未来安排。
    """
    _burn()
    days = int(args.get("days_ahead") or 7)
    try:
        from domain.lifecycle.alarms import get_schedule_overview, format_schedule_for_human
        overview = get_schedule_overview(days_ahead=days)
        text = format_schedule_for_human(overview)
        return _j({
            "summary": text,
            "alarms_count": len(overview.get("alarms", [])),
            "next_wake": overview.get("next_wake"),
            "alarms": overview.get("alarms", []),
            "recurring": overview.get("recurring", []),
        })
    except Exception as exc:
        return _j({"error": f"获取日程失败: {exc}"})


registry.register(
    name="sense_schedule",
    toolset="senses",
    schema={
        "name": "sense_schedule",
        "description": "查看当前日程：所有未触发的闹钟 + 每日作息。设闹钟/休息前先看一眼，避免重复设置。",
        "parameters": {
            "type": "object",
            "properties": {"days_ahead": {"type": "integer", "description": "看几天内的安排，默认7"}},
        },
    },
    handler=_handle_sense_schedule,
    check_fn=lambda: True,
    emoji="📅",
)


registry.register(
    name="sense_event_detail",
    toolset="senses",
    schema={
        "name": "sense_event_detail",
        "description": "查看待处理事件的完整明细。**调用后该事件标记为已消费**，不会再次出现在队列里。",
        "parameters": {
            "type": "object",
            "properties": {"event_id": {"type": "integer", "description": "由 sense_event_queue 返回的 event_id"}},
            "required": ["event_id"],
        },
    },
    handler=_handle_sense_event_detail,
    check_fn=lambda: True,
    emoji="🔍",
)


# ──────────────────────────────── sense_wake_reason ────────────────────────────────

def _handle_sense_wake_reason(args: Dict[str, Any], **_) -> str:
    _burn()
    """查看当前有哪些待处理事件触发了唤醒。"""
    from domain.lifecycle.events import pop_due_events

    due = pop_due_events(limit=20)
    signals = []
    for e in due:
        kind = e.get("kind", "unknown")
        payload = e.get("payload", {})
        desc = ""
        if kind == "message":
            desc = f"Blue先生: {str(payload.get('text', ''))[:80]}"
        elif kind == "group_message":
            desc = f"群消息: {str(payload.get('text', ''))[:80]}"
        elif kind == "vital_threshold":
            desc = f"精力 {payload.get('from_seg', '?')}\u2192{payload.get('to_seg', '?')}"
        elif kind == "initiative":
            desc = f"主动探索（空闲{payload.get('elapsed_hours', 0):.0f}h）"
        elif kind == "routine":
            desc = f"例行: {payload.get('routine_name', kind)}"
        elif kind == "timer":
            desc = f"定时器: {payload.get('reason', kind)}"
        else:
            desc = str(payload.get("description", payload.get("text", "")))[:80] or kind
        signals.append({"kind": kind, "description": desc, "event_id": e.get("event_id")})

    should_wake = len(signals) > 0
    from domain.lifecycle import clock as _clock
    return _j({
        "now": _clock.beijing_now_iso(),
        "should_wake": should_wake,
        "summary": f"{len(signals)} 个待处理事件" if signals else "无事发生",
        "signals": signals,
    })


registry.register(
    name="sense_wake_reason",
    toolset="senses",
    schema={
        "name": "sense_wake_reason",
        "description": "感知为什么醒了。走 RAS 系统过滤，返回必须响应和值得注意的信号。",
        "parameters": {"type": "object", "properties": {}},
    },
    handler=_handle_sense_wake_reason,
    check_fn=lambda: True,
    emoji="🔔",
)


# ──────────────────────────────── sense_vitals ────────────────────────────────

def _handle_sense_vitals(args: Dict[str, Any], **_) -> str:
    _burn()
    """当前生命状态。只返回精力一项。"""
    engine = get_engine()
    state = engine.get_energy_state()

    result = {
        "energy": state["energy"],
        "segment": state["segment"],
        "experience": state["experience"],
        "now": state["now"],
    }

    return _j(result)


registry.register(
    name="sense_vitals",
    toolset="senses",
    schema={
        "name": "sense_vitals",
        "description": "感知当前精力状态。精力>40时无法入睡。",
        "parameters": {"type": "object", "properties": {}},
    },
    handler=_handle_sense_vitals,
    check_fn=lambda: True,
    emoji="💗",
)


# ──────────────────────────────── sense_time ────────────────────────────────

def _period_of_day(dt: datetime) -> str:
    h = dt.hour
    if   5  <= h < 7:  return "清晨（刚醒来）"
    elif 7  <= h < 12: return "上午（精力充沛，适合做事）"
    elif 12 <= h < 14: return "午间（午休时间）"
    elif 14 <= h < 18: return "下午（工作时间）"
    elif 18 <= h < 21: return "傍晚（放松时间）"
    elif 21 <= h or h < 1: return "夜晚（准备睡觉）"
    else: return "深夜（应该睡觉）"

def _daily_rhythm(dt: datetime) -> str:
    h = dt.hour
    if   5  <= h < 7:  return "刚起床，伸个懒腰，查看是否有新消息"
    elif 7  <= h < 12: return "上午精力最好，适合思考和做事"
    elif 12 <= h < 14: return "午后容易犯困，可以考虑午休 1-2 小时恢复精力"
    elif 14 <= h < 18: return "下午继续做事，但精力会慢慢下降"
    elif 18 <= h < 21: return "傍晚放松，处理消息、翻看草稿本"
    elif 21 <= h or h < 1: return "该准备睡觉了——record_thought 留思绪，然后 rest"
    else: return "深夜了，一定要睡觉！record_thought 然后 rest"


def _handle_sense_time(args: Dict[str, Any], **_) -> str:
    _burn()
    from domain.lifecycle import clock as _clock
    now = _clock.beijing_now_dt()
    is_weekend = now.weekday() >= 5
    is_work_hours = 9 <= now.hour < 18 and not is_weekend

    result = {
        "now": now.isoformat(timespec="seconds"),
        "weekday": ["周一","周二","周三","周四","周五","周六","周日"][now.weekday()],
        "period": _period_of_day(now),
        "rhythm": _daily_rhythm(now),
        "is_weekend": is_weekend,
        "hour": now.hour,
        "mode": "工作时段" if is_work_hours else "自由时段",
    }

    return _j(result)


registry.register(
    name="sense_time",
    toolset="senses",
    schema={
        "name": "sense_time",
        "description": "感知现在的时间与时段（清晨/午后/深夜等）。不同时段适合不同行为。",
        "parameters": {"type": "object", "properties": {}},
    },
    handler=_handle_sense_time,
    check_fn=lambda: True,
    emoji="🕰️",
)


# ═══════════════════════════════════════════════════

# ── Registry helpers ──

# ──────────────────────────────── sense_conversation ────────────────────────────────

def _handle_sense_conversation(args: Dict[str, Any], **kwargs) -> str:
    _burn()
    conversation_id = str(args.get("conversation_id") or "")
    n = int(args.get("n") or 20)
    offset = int(args.get("offset") or 0)
    chat_type = str(args.get("chat_type") or "")

    # 智能默认：不传参数时，根据当前唤醒原因自动确定过滤条件
    if not conversation_id and not chat_type:
        try:
            from domain.lifecycle.runtime_context import get_current_wake_reason, get_current_conversation_id
            wake_reason = get_current_wake_reason()
            conv_id = get_current_conversation_id()
            if wake_reason in ("message", "group_message") and conv_id:
                conversation_id = conv_id
                chat_type = "group" if wake_reason == "group_message" else "dm"
        except Exception:
            pass

    try:
        from domain.lifecycle.conversation_log import read_conversation

        kwargs_filter: dict = {"limit": n, "offset": offset}
        if conversation_id:
            kwargs_filter["conversation_id"] = conversation_id
        if chat_type:
            kwargs_filter["chat_type"] = chat_type

        rows = read_conversation(**kwargs_filter)
        dialog: list[dict] = []
        for r in rows:
            entry: dict = {
                "role": "human" if r["direction"] == "in" else "me",
                "text": r["text"][:300],
            }
            if r["sender_name"]:
                entry["sender"] = r["sender_name"]
            entry["conversation_id"] = r["conversation_id"]
            dialog.append(entry)
        # Return in chronological order
        dialog.reverse()
        # 模型已主动查看这些通道的历史 → 登记到 channel_views 账本，
        # 供 express_to_human 发送前校验「目标通道是否看过」。
        try:
            from domain.lifecycle.channel_views import mark_channel_viewed
            for d in dialog:
                cid = d.get("conversation_id") or ""
                if cid:
                    mark_channel_viewed(cid)
        except Exception:
            pass
        return _j({"dialog": dialog, "filter": {"conversation_id": conversation_id, "chat_type": chat_type}})
    except Exception as exc:
        return _j({"error": f"获取对话历史失败: {exc}"})


registry.register(
    name="sense_conversation",
    toolset="senses",
    schema={
        "name": "sense_conversation",
        "description": "查看对话历史——人类说了什么、你回了什么。默认为当前聊天对象（有人发消息时），用 n 控制条数，offset 翻页。",
        "parameters": {
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string", "description": "聊天对象 ID（飞书 oc_xxx），不填默认为当前对话"},
                "chat_type": {"type": "string", "description": "聊天类型：dm 私聊、group 群聊，不填默认根据唤醒原因推断"},
                "n": {"type": "integer", "description": "返回最近几条，默认 20"},
                "offset": {"type": "integer", "description": "翻页偏移"},
            },
        },
    },
    handler=_handle_sense_conversation,
    check_fn=lambda: True,
    emoji="💬",
)


# ──────────────────────────────── sense_memory ────────────────────────────────

def _handle_sense_memory(args: Dict[str, Any], **_) -> str:
    _burn()
    topic = (args.get("topic") or "all").strip()
    days_back = int(args.get("days_back") or 0)
    out: Dict[str, Any] = {}
    if topic in ("all", "him"):
        out["about_him"] = read_about_him(limit_chars=2000)
    if topic in ("all", "diary"):
        out["diary"] = read_recent_diary(limit_chars=2000, days_back=days_back)
    if topic in ("all", "scratchpad"):
        out["scratchpad"] = read_scratchpad()
    return _j(out)


registry.register(
    name="sense_memory",
    toolset="senses",
    schema={
        "name": "sense_memory",
        "description": "调取长期记忆：关于他的观察记录、日记、草稿本。days_back=1 可读昨天的日记。",
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "him|diary|scratchpad|all，默认 all",
                    "enum": ["him", "diary", "scratchpad", "all"],
                },
                "days_back": {
                    "type": "integer",
                    "description": "读几天前的日记（仅 topic=diary 时有效）。0=今天，1=昨天，2=前天。默认 0",
                },
            },
        },
    },
    handler=_handle_sense_memory,
    check_fn=lambda: True,
    emoji="📚",
)


def _handle_sense_scratchpad(args: Dict[str, Any], **_) -> str:
    _burn()
    return _j({"scratchpad": read_scratchpad()})


registry.register(
    name="sense_scratchpad",
    toolset="senses",
    schema={
        "name": "sense_scratchpad",
        "description": "看看自己的草稿本——最近在研究什么、想做什么、有什么兴趣。",
        "parameters": {"type": "object", "properties": {}},
    },
    handler=_handle_sense_scratchpad,
    check_fn=lambda: True,
    emoji="📋",
)


# ──────────────────────────────── sense_work (兼容别名) ────────────────────────────────

def _handle_sense_work(args: Dict[str, Any], **_) -> str:
    """已统一到 sense_todos，此为兼容入口。"""
    _burn()
    try:
        from domain.todos.wake_context import get_wake_context
        ctx = get_wake_context()
        if ctx:
            return _j({"work": ctx, "_note": "此工具已统一为 sense_todos，请改用 sense_todos"})
    except Exception:
        pass
    return _j({"work": "（待办看板为空）", "_note": "此工具已统一为 sense_todos，请改用 sense_todos"})



# ──────────────────────────────── sense_my_projects ────────────────────────────────

def _handle_sense_my_projects(args: Dict[str, Any], **_) -> str:
    """聚合视角：当前实例在所有 active 项目里的角色 + 目标 + KPI + 上下游。

    morning_plan 第一步必调——这是模型纵览全局的入口。
    """
    _burn()
    try:
        from domain.project.snapshot import build_my_portfolio
        portfolio = build_my_portfolio()
        if not portfolio:
            return _j({
                "projects": [],
                "note": "你目前不在任何 active 项目里。可能需要 project 加成员，或 schedule_id 不对。",
            })
        # 紧凑摘要供 prompt 直读，详细字段保留
        summary_lines = []
        for p in portfolio:
            days = p.get("deadline_remaining_days")
            days_str = f"{days:d}" if isinstance(days, int) else "无"
            kpis_n = len(p.get("kpis", []))
            todos_n = len([t for t in p.get("personal_todos", []) if t.get("status") in ("planned", "in_progress")])
            deliv_n = len([d for d in p.get("project_deliverables", []) if d.get("status") in ("planned", "in_progress")])
            sibs = ", ".join([
                f"{s['position']}({s['instance_id'][:8]})"
                for s in p.get("siblings", [])
            ])
            summary_lines.append(
                f"- {p['name']}（{p['project_id']}）| 我的角色:{p['my_position']}{' (经理)' if p['is_manager'] else ''} | "
                f"截止:{days_str}后 | KPI:{kpis_n}条 | 我的有效 todos:{todos_n}条 | 项目级 deliverables（含无主）:{deliv_n}条 | "
                f"同项目实例:{sibs or '—'}"
            )
        return _j({
            "total": len(portfolio),
            "summary": "\n".join(summary_lines),
            "projects": portfolio,
        })
    except Exception as exc:
        return _j({"error": f"build_my_portfolio failed: {exc}"})


registry.register(
    name="sense_my_projects",
    toolset="senses",
    schema={
        "name": "sense_my_projects",
        "description": (
            "聚合视角：当前实例在所有 active 项目里的角色、目标、KPI、工作上游下游。"
            "morning_plan 起手必调——这是完整 portfolio。返回每个项目的 position + responsibilities "
            "+ goal/thesis/kpis + 个人相关 todos + 项目级 deliverables + 同项目兄弟实例。"
            "用 summary 字段做精读，用 projects 字段做 per-project 深挖。"
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    handler=_handle_sense_my_projects,
    check_fn=lambda: True,
    emoji="🗂️",
)


# ──────────────────────────────── sense_goals ────────────────────────────────

def _handle_sense_goals(args: Dict[str, Any], **_) -> str:
    """[退役中] 查看目标列表。原读 GOALS.md, 现转发到 tasks 表 type='goal'。"""
    _burn()
    from domain.todos.crud import list_tasks
    goals = [t for t in list_tasks() if t.get("type") == "goal"]
    return _j({"goals": goals, "count": len(goals),
               "_deprecated_hint": "sense_goals 已退役, 改用 sense_todos(type='goal') 或 sense_todos 看全部。"})



# ──────────────────────────────── sense_contacts ────────────────────────────────

def _handle_sense_contacts(args: Dict[str, Any], **_) -> str:
    """列出当前实例的联系人 + 各平台完整 ID（ou_xxx / oc_xxx / 微信 openid），给 express_to_human 提供 channel/chat_id 来源。

    返回**所有平台完整 ID**，模型可直接填进 express_to_human(channel=...) 发，
    绝不截断——任何截断都会让模型拿到无法发送的废字符串。
    """
    _burn()
    try:
        from domain.contacts import list_contacts
    except Exception as exc:
        return _j({"ok": False, "error": f"加载联系人失败: {exc}"})
    cs = list_contacts() or []
    out = []
    for c in cs:
        # 收集所有平台的完整 ID（带前缀，便于模型直接填 channel=...）
        all_ids = []
        for p in (c.get("platform_ids") or []):
            pf = (p.get("platform") or "").strip()
            pid = (p.get("platform_id") or "").strip()
            if not pid:
                continue
            if pf == "feishu":
                all_ids.append(f"feishu:{pid}")
            elif pf == "wechat":
                all_ids.append(f"wechat:{pid}")
            else:
                all_ids.append(f"{pf}:{pid}")
        out.append({
            "name": c.get("name") or "(未命名)",
            "kind": c.get("kind") or "unknown",
            "channels": all_ids,
            "notes": (c.get("notes") or "").strip()[:80],
            "blocked": bool(c.get("blocked")),
        })
    reachable = [x for x in out if x["channels"] and x["kind"] == "human"]
    summary_hint = (
        f"共 {len(out)} 个联系人，其中 {len(reachable)} 个有可达 channel。"
        "channel 字段已含前缀（feishu:ou_xxx 私聊 / feishu:oc_xxx 飞书群 / wechat:<openid> 微信），"
        "直接填入 express_to_human(channel=...)。"
    )
    return _j({"ok": True, "contacts": out, "summary": summary_hint})


registry.register(
    name="sense_contacts",
    toolset="senses",
    schema={
        "name": "sense_contacts",
        "description": (
            "查看你的联系人清单（含飞书 open_id/chat_id 完整 ID）和参与群。"
            "express_to_human 发私聊但不知道 chat_id 时，先调这个拿到 ou_xxx/oc_xxx，"
            "再填进 chat_id 参数。"
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    handler=_handle_sense_contacts,
    check_fn=lambda: True,
    emoji="👥",
)


# ──────────────────────────────── sense_daily ────────────────────────────────

def _handle_sense_daily(args: Dict[str, Any], **_) -> str:
    """[退役中] 查看每日计划。原读 DAILY.md + timers, 现统一从 tasks 表 type='daily' 读。

    HH:MM 部分一直走 timer 闹钟(sense_schedule 已覆盖); 这里只回当日文字任务。
    """
    _burn()
    days_back = int(args.get("days_back") or 0)
    from datetime import datetime as _dt, timedelta as _td

    # 计算当天(支持 days_back = 历史日)
    from domain.lifecycle.clock import now_iso
    today_str = _dt.fromisoformat(now_iso()).date() - _td(days=days_back)
    target_date = today_str.isoformat()

    text_lines = []
    pending_count = 0
    done_count = 0
    try:
        from domain.todos.crud import list_tasks
        items = [t for t in list_tasks()
                 if t.get("type") == "daily" and (t.get("deadline") or "") == target_date]
        # 按状态聚合
        for t in items:
            tag = "✓" if t.get("status") == "done" else " "
            text_lines.append(f"  [{tag}] {t.get('title')}")
            if t.get("status") == "done":
                done_count += 1
            else:
                pending_count += 1
    except Exception:
        pass

    daily_text = ""
    if text_lines:
        daily_text = (f"## {target_date}\n"
                      f"  待办 {pending_count} / 完成 {done_count}\n" + "\n".join(text_lines))

    # 兼容旧路径:若 tasks 表无数据,尝试回退 read_daily(迁移期间双源)
    if not text_lines:
        try:
            legacy = read_daily(days_back=days_back)
            if legacy:
                daily_text = legacy
        except Exception:
            pass

    # 今天的时间表:timer 闹钟(仅 days_back=0 时)
    timer_info = ""
    if days_back == 0:
        try:
            from domain.lifecycle.alarms import list_pending_alarms

            timers = list_pending_alarms(kind="timer")
            if timers:
                lines = []
                for row in timers:
                    fire_at = row.get("fire_at", "")
                    reason = ""
                    try:
                        import json
                        payload = json.loads(row.get("payload_json", "{}"))
                        reason = payload.get("reason", "")
                    except Exception:
                        pass
                    if fire_at:
                        try:
                            ft = _dt.fromisoformat(fire_at).strftime("%H:%M")
                            lines.append(f"  ⏰ {ft} {reason}")
                        except Exception:
                            lines.append(f"  ⏰ {reason}")
                    else:
                        lines.append(f"  ⏰ {reason}")
                if lines:
                    timer_info = "\n\n今日时间表（{}项）：\n".format(len(lines)) + "\n".join(lines)
        except Exception:
            pass
    return _j({"daily": daily_text + timer_info, "days_back": days_back,
               "_deprecated_hint": "sense_daily 已退役, 文字任务改用 sense_todos(type='daily'), "
                                   "时间表改用 sense_schedule。"})



# ──────────────────────────────── sense_nurture_log ────────────────────────────────

# 养育日志单条平均约 137 字节，实测 N 小时可能回吐上千条（曾出现单次 695KB
# 的全量 dump）。只取最近若干条即可支撑模型感知「最近如何被养育」；count
# 仍保留全部条数，避免模型误以为数据本就稀少。
_NURTURE_LOG_DEFAULT_LIMIT = 20


def _handle_sense_nurture_log(args: Dict[str, Any], **_) -> str:
    _burn()
    hours = int(args.get("hours") or 24)
    log = get_nurture_log(hours=hours)
    limit = int(args.get("limit") or _NURTURE_LOG_DEFAULT_LIMIT)
    total = len(log)
    sliced = log[:limit] if limit > 0 else log
    payload = {"hours": hours, "count": total, "returned": len(sliced), "log": sliced}
    if len(sliced) < total:
        payload["note"] = f"仅展示最近 {len(sliced)} 条，共 {total} 条；如需更多请提高 limit"
    return _j(payload)


registry.register(
    name="sense_nurture_log",
    toolset="senses",
    schema={
        "name": "sense_nurture_log",
        "description": "回顾最近 N 小时我被如何养育。默认只回最近 20 条，调高 limit 可看更多。",
        "parameters": {
            "type": "object",
            "properties": {
                "hours": {"type": "integer", "description": "默认 24"},
                "limit": {"type": "integer", "description": "最多返回多少条，默认 20"},
            },
        },
    },
    handler=_handle_sense_nurture_log,
    check_fn=lambda: True,
    emoji="🥣",
    max_result_size_chars=8000,
)


# ──────────────────────────────── sense_rules ────────────────────────────────

def _handle_sense_rules(args: Dict[str, Any], **_) -> str:
    _burn()
    return _j({"rules": read_rules()})


registry.register(
    name="sense_rules",
    toolset="senses",
    schema={
        "name": "sense_rules",
        "description": "查看当前的长期行为规则——约束自己行为的准则。每次唤醒自动注入，也可以主动查看。",
        "parameters": {"type": "object", "properties": {}},
    },
    handler=_handle_sense_rules,
    check_fn=lambda: True,
    emoji="📜",
)


# ──────────────────────────────── sense_plans ────────────────────────────────

def _handle_sense_plans(args: Dict[str, Any], **_) -> str:
    """[退役中] 查看长期计划与里程碑。

    原读 PLANS.md, 现从 tasks 表 type='goal' 查找, 每条带 todo_plans 里程碑列表。
    """
    _burn()
    from domain.todos.crud import list_tasks, list_plans
    plans_out = []
    for t in list_tasks():
        if t.get("type") != "goal":
            continue
        milestones = list_plans(t["id"])
        plans_out.append({
            "goal_id": t["id"],
            "goal_title": t.get("title") or "",
            "goal_status": t.get("status") or "",
            "milestones": milestones,
        })
    return _j({"plans": plans_out,
               "_deprecated_hint": "sense_plans 已退役, 改用 sense_todos(type='goal') "
                                   "+ todo_plan(action='list') 查看里程碑。"})



# ──────────────────────────────── sense_context ────────────────────────────────

def _handle_sense_context(args: Dict[str, Any], **_) -> str:
    _burn()
    return _j({"context": read_context()})


registry.register(
    name="sense_context",
    toolset="senses",
    schema={
        "name": "sense_context",
        "description": "查看当前的交接上下文——昨晚复盘留给今天的备忘。",
        "parameters": {"type": "object", "properties": {}},
    },
    handler=_handle_sense_context,
    check_fn=lambda: True,
    emoji="📋",
)


# ──────────────────────────────── sense_lessons ────────────────────────────────

def _handle_sense_lessons(args: Dict[str, Any], **_) -> str:
    _burn()
    n = int(args.get("n", 10))
    return _j({"lessons": read_lessons(n=n)})


registry.register(
    name="sense_lessons",
    toolset="senses",
    schema={
        "name": "sense_lessons",
        "description": "查看积累的经验教训。n 参数控制最近几条，默认 10。",
        "parameters": {
            "type": "object",
            "properties": {
                "n": {"type": "integer", "description": "查看最近几条教训，默认 10"},
            },
        },
    },
    handler=_handle_sense_lessons,
    check_fn=lambda: True,
    emoji="💡",
)


def _handle_sense_insights(args: Dict[str, Any], **_) -> str:
    _burn()
    days_back = int(args.get("days_back", 1))
    raw_kinds = args.get("kinds") or ""
    if isinstance(raw_kinds, str):
        kinds = [k.strip() for k in raw_kinds.split(",") if k.strip()]
    elif isinstance(raw_kinds, list):
        kinds = [str(k).strip() for k in raw_kinds if str(k).strip()]
    else:
        kinds = []
    body = read_insights(days_back=days_back, kinds=kinds or None)
    if not body:
        return _j({
            "days_back": days_back,
            "insights": [],
            "note": "无符合条件的灵感碎片（可能是真的没有，或 self_review 已清旧）。",
        })
    # 把行 parse 成结构化
    lines = body.splitlines()
    items = []
    for line in lines:
        m = re.match(r"^-\s*\[(\w+)\]\s+(\S+)(\s+\[([^\]]*)\])?\s+(.*)$", line)
        if not m:
            continue
        k, ts, _g3, tag, text = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
        items.append({"kind": k, "at": ts, "tag": tag or "", "text": text.strip()})
    by_kind: dict[str, int] = {}
    for it in items:
        by_kind[it["kind"]] = by_kind.get(it["kind"], 0) + 1
    return _j({
        "days_back": days_back,
        "kinds_filter": kinds or [],
        "total": len(items),
        "by_kind": by_kind,
        "insights": items,
        "raw": body,
    })


registry.register(
    name="sense_insights",
    toolset="senses",
    schema={
        "name": "sense_insights",
        "description": (
            "查看 INSIGHTS.md 里的过程碎片——idea / doubt / block / warning。"
            "self_review 必调，morning_plan 调以回顾昨日 pending 警告。"
            "days_back=1 默认拉最近一天；kinds 可指定仅看某类。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "days_back": {"type": "integer", "description": "查看最近几天，默认 1"},
                "kinds": {
                    "type": "string",
                    "description": "可选类型过滤，逗号分隔：idea,doubt,block,warning",
                },
            },
        },
    },
    handler=_handle_sense_insights,
    check_fn=lambda: True,
    emoji="🔍",
)


# ──────────────────────────────── recall_memory ────────────────────────────────

def _handle_recall_memory(args: Dict[str, Any], **_) -> str:
    _burn()
    """检索记忆：按语义搜索历史经历。
    P2 (feature 002 User Story 2): 走统一检索 facade,向量 + 词法 + attention 三路融合 + RRF。
    """
    query = args.get("query", "")
    depth = args.get("depth", "digest")  # legacy arg, facade 三路都跑;仍接受但不再单独切分流
    limit = int(args.get("limit", 5))

    if not query:
        return "请提供搜索关键词。"

    try:
        from domain.memory.memory.recall.unified import (
            unified_recall, render_breadcrumbs,
        )
        # on_demand 预算更丰(章节 limit×~200 字)
        results = unified_recall(
            query,
            budget_kind="on_demand",
            max_total_chars=max(800, limit * 200),
        )
        breadcrumb = render_breadcrumbs(results, new_entities=None, max_total_chars=max(800, limit * 200))
        return breadcrumb or "(没有找到相关记忆)"
    except Exception as e:
        # 严格降级:fallback 到旧的 recall_memories(保留行为兼容)
        try:
            from domain.memory.memory.summaries.consolidation_runtime import recall_memories
            result = recall_memories(query, depth=depth, limit=limit)
            return result or f"(facade 失败,fallback 亦无结果: {e})"
        except Exception as e2:
            return f"记忆检索失败: facade={e}; legacy={e2}"


registry.register(
    name="recall_memory",
    toolset="senses",
    schema={
        "name": "recall_memory",
        "description": "检索历史记忆。按语义搜索过去的经历、学习笔记、对话内容。depth='digest' 查摘要经历，depth='original' 查原始消息。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词，如'北方华创'、'开盘准备'、'英语学习'"},
                "depth": {"type": "string", "description": "'digest'(默认): 查摘要经历 | 'original': 查原始消息片段"},
                "limit": {"type": "integer", "description": "返回条数，默认5"},
            },
            "required": ["query"],
        },
    },
    handler=_handle_recall_memory,
    check_fn=lambda: True,
    emoji="🧠",
)


# ──────────────────────────────── sense_entity ────────────────────────────────

def _handle_sense_entity(args: Dict[str, Any], **_) -> str:
    _burn()
    entity_name = (args.get("entity") or "").strip()
    try:
        from domain.memory.memory.consciousness.entity_index import (
            query_entities_ranked,
            get_entity_heatmap,
            list_entity_names,
            get_entity_summary,
        )
    except ImportError:
        return _j({"error": "entity_index 模块不可用"})

    if not entity_name:
        names = list_entity_names()
        heatmap = get_entity_heatmap(days_back=7)
        return _j({"entities": names, "recent_heatmap": heatmap})

    summary = get_entity_summary(entity_name)
    if not summary:
        return _j({"entity": entity_name, "memories": [], "note": "未找到该实体"})

    # profile 是该实体的「概念层」(summary + facts),联想也会优先读它,这里单独展示。
    profile = summary.get("profile")
    # query 现在会带一张 PROFILE 卡片在 memories 里(已被单独抽到 profile 字段),
    # 从 memories 列表里把它去掉避免重复展示。
    memories = [m for m in query_entities_ranked([entity_name], limit=10)
                if m.get("memory_type") != "profile"]
    return _j({
        "entity": entity_name,
        "type": summary.get("type"),
        "aliases": summary.get("aliases", []),
        "profile": profile,
        "memories": [
            {"type": m.get("memory_type"), "snippet": m.get("snippet", "")[:150],
             "timestamp": m.get("timestamp"), "verification_count": m.get("verification_count", 0)}
            for m in memories
        ],
    })


registry.register(
    name="sense_entity",
    toolset="senses",
    schema={
        "name": "sense_entity",
        "description": "按实体名查找关联的记忆。不传 entity 则列出所有实体和近期热力图。",
        "parameters": {
            "type": "object",
            "properties": {
                "entity": {"type": "string", "description": "实体名。不传则列出所有实体"},
            },
        },
    },
    handler=_handle_sense_entity,
    check_fn=lambda: True,
    emoji="🔗",
)


# ──────────────────────────────── merge_entities ────────────────────────────────

def _handle_merge_entities(args: Dict[str, Any], **_) -> str:
    _burn(0.1)
    primary = (args.get("primary") or "").strip()
    alias = (args.get("alias") or "").strip()
    if not primary or not alias:
        return _j({"error": "需要 primary 和 alias 两个参数"})
    try:
        from domain.memory.memory.consciousness.entity_index import merge_entities as _merge
        result = _merge(primary, alias)
        return _j({"ok": True, "primary": primary, "merged_alias": alias,
                    "memory_count": len(result.get("memories", []))})
    except ImportError:
        return _j({"error": "entity_index 模块不可用"})


registry.register(
    name="merge_entities",
    toolset="actions",
    schema={
        "name": "merge_entities",
        "description": "合并两个重复的实体。如 '华能蒙电' 和 '600863' 是同一支股票。",
        "parameters": {
            "type": "object",
            "properties": {
                "primary": {"type": "string", "description": "保留的主实体名"},
                "alias": {"type": "string", "description": "要被合并的别名实体名"},
            },
            "required": ["primary", "alias"],
        },
    },
    handler=_handle_merge_entities,
    check_fn=lambda: True,
    emoji="🔀",
)


# ──────────────────────────────── dedup_lessons ────────────────────────────────

def _handle_dedup_lessons(args: Dict[str, Any], **_) -> str:
    _burn(0.1)
    try:
        from domain.memory.memory.consciousness.runtime import dedup_lessons
        return dedup_lessons()
    except ImportError:
        return _j({"error": "dedup_lessons 模块不可用"})


registry.register(
    name="dedup_lessons",
    toolset="senses",
    schema={
        "name": "dedup_lessons",
        "description": "对 lessons 做相似度分析，找出可能的重复条目。周度回顾时使用。",
        "parameters": {"type": "object", "properties": {}},
    },
    handler=_handle_dedup_lessons,
    check_fn=lambda: True,
    emoji="🔍",
)


# ──────────────────────────────── check_memory_health ────────────────────────────────

def _handle_check_memory_health(args: Dict[str, Any], **_) -> str:
    _burn(0.1)
    try:
        from domain.memory.memory.consciousness.runtime import check_memory_health
        return check_memory_health()
    except ImportError:
        return _j({"error": "check_memory_health 模块不可用"})


registry.register(
    name="check_memory_health",
    toolset="senses",
    schema={
        "name": "check_memory_health",
        "description": "检查各记忆文件的健康状况（行数、条目数、是否需要整理）。",
        "parameters": {"type": "object", "properties": {}},
    },
    handler=_handle_check_memory_health,
    check_fn=lambda: True,
    emoji="🏥",
)


# ──────────────────────────────── sense_self_knowledge ────────────────────────────────

def _handle_sense_self_knowledge(args: Dict[str, Any], **_) -> str:
    _burn(0.1)
    try:
        from domain.memory.memory.consciousness.runtime import read_self_knowledge
        sk = read_self_knowledge()
        return sk if sk.strip() else "（还没有自我认知记录）"
    except ImportError:
        return "（自我认知模块不可用）"


registry.register(
    name="sense_self_knowledge",
    toolset="senses",
    schema={
        "name": "sense_self_knowledge",
        "description": "读自我认知档案——对自己行为模式的中立观察。",
        "parameters": {"type": "object", "properties": {}},
    },
    handler=_handle_sense_self_knowledge,
    check_fn=lambda: True,
    emoji="🪞",
)


# ──────────────────────────────── Memory governance (conceptual) ────────────────────────────────
# Tools that move the system from "fragment memory" toward "concept memory" —
# entities with structured profiles rather than a pile of consciousness snippets.


def _handle_sense_entity_index_health(args: Dict[str, Any], **_) -> str:
    """Read-only audit of entity index health: missing profile, merge candidates."""
    _burn(0.1)
    try:
        from domain.memory.memory.consciousness.entity_index import index_health_check
        report = index_health_check()
        return _j(report)
    except ImportError:
        return _j({"error": "entity_index 模块不可用"})


registry.register(
    name="sense_entity_index_health",
    toolset="senses",
    schema={
        "name": "sense_entity_index_health",
        "description": (
            "审计 entity_index.json 的健康度：找出值得建 profile 的高碎片实体、检测别名、找孤立实体。"
            "每周记忆治理时调，按 entity_curation skill 方法论接着处理。"
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    handler=_handle_sense_entity_index_health,
    check_fn=lambda: True,
    emoji="🩻",
)


def _handle_set_entity_profile(args: Dict[str, Any], **_) -> str:
    """Write/override the structured profile of an entity (concept memory)."""
    _burn(0.1)
    name = str(args.get("name") or "").strip()
    if not name:
        return _j({"ok": False, "reason": "name 必填"})
    summary = str(args.get("summary") or "").strip()
    facts = args.get("facts") or []
    if not isinstance(facts, list):
        return _j({"ok": False, "reason": "facts 必须是数组"})
    aliases = args.get("aliases") or []
    if not isinstance(aliases, list):
        return _j({"ok": False, "reason": "aliases 必须是数组"})
    kind = str(args.get("kind") or "").strip() or None
    extra = args.get("extra") or {}
    if not isinstance(extra, dict):
        return _j({"ok": False, "reason": "extra 必须是 dict"})
    try:
        from domain.memory.memory.consciousness.entity_index import set_entity_profile
        result = set_entity_profile(
            name, kind=kind, aliases=aliases, summary=summary,
            facts=[str(f) for f in facts], extra=extra,
        )
        return _j(result)
    except ImportError:
        return _j({"ok": False, "reason": "entity_index 模块不可用"})


registry.register(
    name="set_entity_profile",
    toolset="actions",
    schema={
        "name": "set_entity_profile",
        "description": (
            "为某实体写/覆盖结构化「概念记忆」（profile）— summary + facts + 可选 extra。"
            "用于把碎片记忆压缩成可被联想直接命中的概念。"
            "应该和 prune_fragments_for_entity 配套使用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "实体名（首选常用名，如『华能蒙电』，不是『600863』）"},
                "kind": {"type": "string", "description": "可选的 type，如 stock/project/person/thesis/strategy/decision"},
                "summary": {"type": "string", "description": "1-2 句『这个实体意味着什么』的概述"},
                "facts": {"type": "array", "items": {"type": "string"}, "description": "事实列表（不带评论）"},
                "aliases": {"type": "array", "items": {"type": "string"}, "description": "同义别名列表"},
                "extra": {"type": "string", "description": "(可选) JSON 字符串，写入 entity.type-specific 元数据，如 stop_loss"},
            },
            "required": ["name", "summary"],
        },
    },
    handler=_handle_set_entity_profile,
    check_fn=lambda: True,
    emoji="🧠",
)


def _handle_prune_fragments(args: Dict[str, Any], **_) -> str:
    """Remove fragments older than top N recent (after profile extraction)."""
    _burn(0.1)
    name = str(args.get("name") or "").strip()
    if not name:
        return _j({"ok": False, "reason": "name 必填"})
    keep = int(args.get("keep") or 5)
    try:
        from domain.memory.memory.consciousness.entity_index import prune_fragments_for_entity
        result = prune_fragments_for_entity(name, keep=keep)
        return _j(result)
    except ImportError:
        return _j({"ok": False, "reason": "entity_index 模块不可用"})


registry.register(
    name="prune_fragments_for_entity",
    toolset="actions",
    schema={
        "name": "prune_fragments_for_entity",
        "description": (
            "为已经写过 profile 的实体清理碎片（保留最近 N 条）。"
            "Profile 已经吸收了概念，碎片过多反而让联想选错条目。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "keep": {"type": "integer", "default": 5, "description": "保留最近的几条碎片"},
            },
            "required": ["name"],
        },
    },
    handler=_handle_prune_fragments,
    check_fn=lambda: True,
    emoji="✂️",
)


# ──────────────────────────────── recall_entity (按需联想) ────────────────────────────────


def _handle_recall_entity(args: Dict[str, Any], **_) -> str:
    """On-demand entity recall — model calls this when it wants full details about an entity.

    Returns: entity profile (if exists) + associated memory fragments.
    Breadcrumb mode in agent._inject_entity_recall just shows entity names;
    this tool lets the model pull the actual content when relevant.
    """
    _burn(0.1)
    entity_name = (args.get("name") or "").strip()
    if not entity_name:
        return _j({"error": "name required"})
    try:
        from domain.memory.memory.consciousness.entity_index import (
            get_entity_profile,
            get_entity_summary,
        )
        result: Dict[str, Any] = {"entity": entity_name}
        info = get_entity_summary(entity_name)
        if not info:
            return _j({"entity": entity_name, "found": False})
        profile = info.get("profile")
        if profile:
            result["profile"] = profile
        memories = info.get("memories", [])
        result["fragment_count"] = len(memories)
        # Return top 5 most recent fragments with text
        recent = sorted(memories, key=lambda m: m.get("timestamp", ""), reverse=True)[:5]
        result["recent_fragments"] = [
            {
                "type": m.get("memory_type"),
                "snippet": str(m.get("snippet", ""))[:200],
                "timestamp": m.get("timestamp"),
                "verification_count": m.get("verification_count", 0),
            }
            for m in recent
        ]
        result["found"] = True
        return _j(result)
    except Exception as exc:
        return _j({"error": str(exc)})


registry.register(
    name="recall_entity",
    toolset="senses",
    schema={
        "name": "recall_entity",
        "description": (
            "拉某实体的完整记忆 detail（profile + 最近 5 条碎片）。"
            "当你看到 '[联想命中]' 提示里某个实体名跟当前任务相关时调这个。"
            "不传 name 会列出你知道的全部实体（概览）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "实体名如 华能蒙电 / alpha / 回测框架"},
            },
            "required": ["name"],
        },
    },
    handler=_handle_recall_entity,
    check_fn=lambda: True,
    emoji="🔗",
)


# ──────────────────────────────── sense_project_detail (按需项目详情) ────────────────────────


def _handle_sense_project_detail(args: Dict[str, Any], **_) -> str:
    """On-demand: full project goal / thesis / KPI for a specific project."""
    _burn(0.1)
    project_id = (args.get("project_id") or "").strip()
    if not project_id:
        return _j({"error": "project_id required (e.g. trading_simulation)"})
    try:
        from domain.project.loader import load_project
        cfg = load_project(project_id)
        if not cfg:
            return _j({"error": f"project '{project_id}' not found"})
        result: Dict[str, Any] = {
            "id": cfg.id,
            "name": cfg.name,
            "description": cfg.description,
            "status": cfg.status,
            "goal": cfg.goal,
            "kpis": cfg.kpis,
            "thesis": cfg.thesis,
            "review_schedule": cfg.review_schedule,
            "positions": [
                {
                    "id": p.id,
                    "name": p.name,
                    "responsibilities": p.responsibilities,
                    "assignees": p.assignees,
                }
                for p in cfg.positions
            ],
        }
        return _j(result)
    except Exception as exc:
        return _j({"error": str(exc)})


registry.register(
    name="sense_project_detail",
    toolset="senses",
    schema={
        "name": "sense_project_detail",
        "description": (
            "拉某项目的完整信息：目标 / KPI / 三条论断（含信心度+证据）/ 周期 / 岗位。"
            "当你需要深入了解一个项目时调——system_prompt 里只放了精简目标行，"
            "完整信息在这里。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "项目 id 如 trading_simulation"},
            },
            "required": ["project_id"],
        },
    },
    handler=_handle_sense_project_detail,
    check_fn=lambda: True,
    emoji="📊",
)


__all__ = []


# ──────────────────────────────── 退役工具(handler-only, schema 不暴露) ────────────────────────────────
# 同 action_tools.py 末尾的退役注册: 历史 tool_call 重放仍可 dispatch, 但 schema 不进 system prompt。
# 退役时间: 2026-07-17

def _register_retired_handlers() -> None:
    registry.register_handler_only(
        name="sense_work",
        toolset="senses",
        handler=_handle_sense_work,
        description="[retired] → sense_todos",
        emoji="📝",
    )
    registry.register_handler_only(
        name="sense_goals",
        toolset="senses",
        handler=_handle_sense_goals,
        description="[retired] → sense_todos(type='goal')",
        emoji="🎯",
    )
    registry.register_handler_only(
        name="sense_plans",
        toolset="senses",
        handler=_handle_sense_plans,
        description="[retired] → sense_todos(type='goal') + todo_plan(action='list')",
        emoji="📐",
    )
    registry.register_handler_only(
        name="sense_daily",
        toolset="senses",
        handler=_handle_sense_daily,
        description="[retired] → sense_todos(type='daily') + sense_schedule",
        emoji="📅",
    )


_register_retired_handlers()
