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
    schema_visible=False,  # V6 工具精简: 降级
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
    schema_visible=False,  # V6 工具精简: 降级
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
    schema_visible=False,  # V6 工具精简: 降级
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
        "description": "感知当前精力(数值/趋势/是否可入睡)。精力>40时无法 rest。每次 wake 已自动注入, 一般不用手动调, 除非想看精确数值。",
        "parameters": {"type": "object", "properties": {}},
    },
    handler=_handle_sense_vitals,
    check_fn=lambda: True,
    emoji="💗",
    schema_visible=False,  # V6 工具精简: 降级
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
    schema_visible=False,  # V6 工具精简: 降级
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
        # 默认拉 5000 字符 (write_diary mode=replace 模式下模型需要完整读全文再覆写,
        # 2000 会截断, 导致模型 replace 后丢失前半段。5000 足够覆盖典型日记 + 整合段)
        out["diary"] = read_recent_diary(limit_chars=5000, days_back=days_back)
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
    schema_visible=False,  # V6 工具精简: 降级
)


def _handle_sense_scratchpad(args: Dict[str, Any], **_) -> str:
    _burn()
    return _j({"scratchpad": read_scratchpad()})


def _handle_sense_social_feed(args: Dict[str, Any], **_) -> str:
    """调取 social_feed 表中的真人 IM 消息。

    查看模式:
      1. 默认 (无参数): 跨群混合, 按时间倒序, 最新 N 条
      2. chat_id/chat_name: 拉指定群的消息流(连贯对话上下文)
      3. category/tag: 按 分类/标签 筛选
      4. before="MM-DD HH:MM": 翻页 — 返回该时间之前的消息 (配合上一次返回的最后一条时间)
    """
    _burn()
    limit = int(args.get("limit") or 30)
    category = (args.get("category") or "").strip()
    tag_filter = (args.get("tag") or "").strip()
    chat_id = (args.get("chat_id") or "").strip()
    chat_name = (args.get("chat_name") or "").strip()
    before = (args.get("before") or "").strip()
    after = (args.get("after") or "").strip()
    try:
        import sqlite3 as _sqlite3
        from infrastructure.config import get_app_instance_id, get_runtime_state_db_path
        iid = get_app_instance_id() or ""
        conn = _sqlite3.connect(str(get_runtime_state_db_path()))
        conn.row_factory = _sqlite3.Row

        where_parts: list[str] = []
        params_list: list = []
        # 指定 chat_id/chat_name: 拉该群完整上下文
        if chat_id:
            where_parts.append("chat_id = ?")
            params_list.append(chat_id)
        elif chat_name:
            where_parts.append("chat_name LIKE ?")
            params_list.append(f"%{chat_name}%")
        # V6: 不再用 scanned=0 过滤 — 解耦已读和拉取
        # scanned 只做统计, 不影响查询
        if iid:
            where_parts.append("(instance_id = ? OR instance_id = '')")
            params_list.append(iid)
        # Time filter: 默认最近 7 天, 但 before 翻页时放宽到 30 天
        from datetime import datetime as _dt
        days_back = 30 if (before or after) else 7
        _cutoff_ts = (_dt.now().timestamp() - days_back * 86400) * 1000
        # 如果有 after, 不用默认 cutoff (用户可能查很久之前的)
        if not after:
            where_parts.append("message_ts > ?")
            params_list.append(_cutoff_ts)

        # V6: 时间范围查询 — before/after 支持组合
        def _parse_time(s: str) -> float:
            """解析 'MM-DD HH:MM' 或 ISO → 毫秒时间戳."""
            from domain.lifecycle.clock import beijing_now_dt
            now_bj = beijing_now_dt()
            if "-" in s and ":" in s:
                parts = s.split()
                md = parts[0].split("-")
                hm = parts[1].split(":") if len(parts) > 1 else ["0","0"]
                return now_bj.replace(month=int(md[0]), day=int(md[1]),
                                      hour=int(hm[0]), minute=int(hm[1]), second=0).timestamp() * 1000
            else:
                return _dt.fromisoformat(s).timestamp() * 1000

        if before:
            try:
                where_parts.append("message_ts < ?")
                params_list.append(_parse_time(before))
            except Exception:
                pass
        if after:
            try:
                where_parts.append("message_ts > ?")
                params_list.append(_parse_time(after))
            except Exception:
                pass

        if category:
            where_parts.append("category = ?")
            params_list.append(category)
        if tag_filter:
            where_parts.append("tags LIKE ?")
            params_list.append(f"%{tag_filter}%")
        where_sql = " WHERE " + " AND ".join(where_parts) if where_parts else ""

        # 按 chat 时正序(对话连贯); 默认倒序(最近在前)
        order = "message_ts ASC" if chat_id or chat_name else "message_ts DESC"

        rows = conn.execute(
            f"SELECT id, chat_name, sender_name, text, has_command, at_all, at_me, "
            f"sender_is_app, message_ts, category, tags, message_id, chat_id "
            f"FROM social_feed{where_sql} "
            f"ORDER BY {order} LIMIT ?",
            params_list + [limit],
        ).fetchall()
        conn.close()
        msgs = [dict(r) for r in rows]
        if not msgs:
            mode = f"chat_id={chat_id}" if chat_id else f"chat_name={chat_name}" if chat_name else "all"
            return _j({"messages": [], "note": f"无消息 ({mode}, category={category or 'any'})"})

        # V6: 不再 mark_scanned — 已读标记和拉取解耦
        # 返回最后一条的时间, 供模型翻页用

        # 格式化: 过滤机器人 + 标记
        from datetime import datetime
        cat_emoji = {
            "command": "🔡", "mention": "📌", "work": "💼", "social": "☕",
            "notification": "📢", "system": "🔧", "default": "·",
        }
        lines = ["[社交近况]"]
        for m in msgs:
            if m.get("sender_is_app") and not m.get("at_all") and not m.get("has_command"):
                continue
            ts_raw = m.get("message_ts") or 0
            # 飞书 message_ts 是毫秒; fromtimestamp 需要秒
            from domain.social.store import _format_relative_ts
            ts = _format_relative_ts(ts_raw)
            chat = (m.get("chat_name") or "?")[:15]
            sender = (m.get("sender_name") or "?")[:8]
            text = (m.get("text") or "").replace("\n", " ").strip()[:120]
            cat = m.get("category", "default")
            cat_icon = cat_emoji.get(cat, "·")
            msg_tags = m.get("tags", "")
            tag_str = f" #{msg_tags}" if msg_tags else ""
            lines.append(f"  · {ts} {cat_icon} {chat} | {sender}{tag_str}: {text}")
        lines.append(f"[/社交近况 · {len(msgs)} 条]")
        # V6: 返回时间边界, 供模型双向翻页
        from datetime import datetime as _dt2
        def _fmt_ts(ts_val):
            if not ts_val: return ""
            _ts_sec = ts_val / 1000 if ts_val > 1e12 else ts_val
            return _dt2.fromtimestamp(_ts_sec).strftime("%m-%d %H:%M")
        oldest_str = _fmt_ts(min((m.get("message_ts") or 0) for m in msgs)) if msgs else ""
        newest_str = _fmt_ts(max((m.get("message_ts") or 0) for m in msgs)) if msgs else ""
        hint_parts = []
        if oldest_str:
            hint_parts.append(f'往老翻: sense_social_feed(before="{oldest_str}")')
        if newest_str:
            hint_parts.append(f'往新翻: sense_social_feed(after="{newest_str}")')
        return _j({
            "messages": lines,
            "count": len(msgs),
            "oldest": oldest_str,
            "newest": newest_str,
            "hint": " | ".join(hint_parts) if hint_parts else "",
        })
    except Exception as exc:
        return _j({"error": str(exc)})


registry.register(
    name="sense_social_feed",
    toolset="senses",
    schema={
        "name": "sense_social_feed",
        "description": (
            "查看真人飞书消息 (类 SQL 查询)。\n"
            "1. 不传参数: 跨群最新消息(最近在前)\n"
            "2. chat_id/chat_name: 拉指定群完整对话流\n"
            "3. before/after: 时间范围过滤 (格式 MM-DD HH:MM), 可组合\n"
            "4. category/tag: 分类/标签筛选\n"
            "返回 oldest + newest, 供翻页。\n"
            "示例:\n"
            "  sense_social_feed() — 最新 30 条\n"
            "  sense_social_feed(before='07-30 12:06') — 该时间之前\n"
            "  sense_social_feed(after='07-30 11:00') — 该时间之后\n"
            "  sense_social_feed(chat_name='智赋千行', after='07-30 11:00', before='07-30 12:00') — 指定群+时间段"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "返回条数(默认30)"},
                "before": {"type": "string", "description": "时间上界: 只返回该时间之前的消息 (MM-DD HH:MM)"},
                "after": {"type": "string", "description": "时间下界: 只返回该时间之后的消息 (MM-DD HH:MM)"},
                "category": {"type": "string", "description": "分类: command/mention/work/social/notification/system/default"},
                "tag": {"type": "string", "description": "自定义标签筛选(tags 字段模糊匹配)"},
                "chat_id": {"type": "string", "description": "指定群 chat_id(oc_xxx)"},
                "chat_name": {"type": "string", "description": "按群名模糊匹配"},
            },
            "required": [],
        },
    },
    handler=_handle_sense_social_feed,
    check_fn=lambda: True,
    emoji="📬",
)


def _handle_tag_social_message(args: Dict[str, Any], **_) -> str:
    """给一条 social_feed 消息打自定义标签。"""
    _burn()
    message_id = (args.get("message_id") or "").strip()
    tags = (args.get("tags") or "").strip()
    if not message_id or not tags:
        return _j({"ok": False, "error": "message_id 和 tags 都必填"})
    try:
        from domain.social.store import add_tags
        ok = add_tags(message_id, tags)
        return _j({"ok": ok, "note": f"已给 {message_id} 添加标签: {tags}"})
    except Exception as exc:
        return _j({"error": str(exc)})


registry.register(
    name="tag_social_message",
    toolset="senses",
    schema={
        "name": "tag_social_message",
        "description": "给一条 social_feed 消息打自定义标签(逗号分隔)。用于分类/标记值得追踪的消息。",
        "parameters": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "消息 ID(从 sense_social_feed 获取)"},
                "tags": {"type": "string", "description": "标签(逗号分隔), 如: 紧急,股票,zhp关心的"},
            },
            "required": ["message_id", "tags"],
        },
    },
    handler=_handle_tag_social_message,
    check_fn=lambda: True,
    emoji="🏷️",
    schema_visible=False,  # V6 工具精简: 降级
)


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
    schema_visible=False,  # V6 工具精简: 降级
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
    """社交关系双向查询：不传参 = 全量清单；传 id/name = 反查。

    ID 语义（系统统一口径）：
      OC（oc_ 开头）= 窗口 ID：群窗口和私聊窗口都是它，回复消息填 chat_id 用 OC
      OU（ou_ 开头）= 用户 ID：@ 人 / 识别"谁在说话"用 OU
      群还是私聊是窗口的 type 字段，不从 ID 前缀判断（私聊窗口也是 oc_ 开头）
    """
    _burn()
    query = (args.get("query") or "").strip()

    try:
        from domain.contacts import list_contacts
    except Exception as exc:
        return _j({"ok": False, "error": f"加载联系人失败: {exc}"})

    # ── 反查模式：传了 OU / OC / 名字 ──
    if query:
        result: Dict[str, Any] = {"ok": True, "query": query}
        if query.startswith("ou_"):
            for c in list_contacts() or []:
                for p in (c.get("platform_ids") or []):
                    if (p.get("platform_id") or "") == query:
                        result["match"] = {"type": "user", "name": c.get("name") or "(未命名)",
                                           "kind": c.get("kind"), "ou": query,
                                           "notes": (c.get("notes") or "")[:120]}
                        return _j(result)
            result["match"] = None
            result["note"] = "未知 OU（从未与此实例交互过）"
        elif query.startswith("oc_"):
            from domain.contacts import lookup_chat
            ch = lookup_chat(query)
            result["match"] = ({"type": "chat", "name": ch.get("name") or "(未命名)",
                                "chat_type": ch.get("type") or "未知",
                                "oc": query} if ch else None)
            if not ch:
                result["note"] = "未知 OC（此窗口无档案）"
        else:
            # 按名字搜（人 + 窗口）
            hits: list = []
            for c in list_contacts() or []:
                if query in (c.get("name") or ""):
                    ids = [p.get("platform_id") for p in (c.get("platform_ids") or [])]
                    hits.append({"type": "user", "name": c.get("name"), "kind": c.get("kind"), "ids": ids})
            from domain.contacts import search_chats
            for ch in search_chats(query, limit=5):
                hits.append({"type": "chat", "name": ch.get("name"), "chat_type": ch.get("type"), "id": ch.get("chat_id")})
            result["matches"] = hits[:10]
        return _j(result)

    # ── 全量模式 ──
    cs = list_contacts() or []
    out = []
    for c in cs:
        all_ids = []
        for p in (c.get("platform_ids") or []):
            pf = (p.get("platform") or "").strip()
            pid = (p.get("platform_id") or "").strip()
            if not pid:
                continue
            all_ids.append(f"{pf or 'unknown'}:{pid}")
        out.append({
            "name": c.get("name") or "(未命名)",
            "kind": c.get("kind") or "unknown",
            "ids": all_ids,
            "notes": (c.get("notes") or "").strip()[:80],
            "blocked": bool(c.get("blocked")),
        })
    from domain.contacts import list_chats
    chats = [{"name": ch.get("name") or "(未命名)", "chat_type": ch.get("type") or "",
              "oc": ch.get("chat_id")} for ch in (list_chats(limit=30) or [])]
    summary_hint = (
        f"人 {len(out)} 个 / 窗口 {len(chats)} 个。"
        "ID 语义：OC=窗口 ID（群/私聊都是它，回复填 chat_id 用）；OU=用户 ID（@人/识人用）。"
        "群还是私聊看 chat_type 字段，不从 ID 前缀判断。"
        "传 query 参数可反查：OU→姓名 / OC→窗口名 / 名字→ID。"
    )
    return _j({"ok": True, "contacts": out, "chats": chats, "summary": summary_hint})


registry.register(
    name="sense_contacts",
    toolset="senses",
    schema={
        "name": "sense_contacts",
        "description": (
            "社交关系查询（人 + 窗口）。不传参 = 全量清单；传 query 可反查："
            "OU(ou_…)→姓名、OC(oc_…)→窗口名和类型、名字→ID。"
            "ID 语义：OC=窗口 ID（群/私聊都是 oc_ 开头，回复消息填 chat_id 用它）；"
            "OU=用户 ID（@人、识别发言者）。群/私聊是窗口的类型字段，别从 ID 前缀猜。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "反查目标：ou_xxx / oc_xxx / 名字（留空=全量）"},
            },
        },
    },
    handler=_handle_sense_contacts,
    check_fn=lambda: True,
    emoji="👥",
    schema_visible=False,  # V6 工具精简: 降级
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
    schema_visible=False,  # V6 工具精简: 降级
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
    schema_visible=False,  # V6 工具精简: 降级
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
        "description": "查看交接上下文(CONTEXT.md)——上次复盘留给今天的 jump-in 备忘:今日重点/注意事项/下一步。每次 wake 已自动注入, 不用手动调除非内容被覆盖想看旧版。",
        "parameters": {"type": "object", "properties": {}},
    },
    handler=_handle_sense_context,
    check_fn=lambda: True,
    emoji="📋",
    schema_visible=False,  # V6 工具精简: 降级
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
    schema_visible=False,  # V6 工具精简: 降级
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
    schema_visible=False,  # V6 工具精简: 降级
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
    schema_visible=False,  # V6: 合并到 recall_cognition_by_key
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
    schema_visible=False,  # V6 工具精简: 降级
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
    schema_visible=False,  # V6 工具精简: 降级
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
    schema_visible=False,  # V6 工具精简: 降级
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
        "description": "读自我认知档案(SELF_KNOWLEDGE.md)——自己观察到的行为模式/偏好/倾向。self_review 时看一眼, 看新观察是否需要追加。",
        "parameters": {"type": "object", "properties": {}},
    },
    handler=_handle_sense_self_knowledge,
    check_fn=lambda: True,
    emoji="🪞",
    schema_visible=False,  # V6 工具精简: 降级
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
    schema_visible=False,  # V6 工具精简: 降级
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
    schema_visible=False,  # V6 工具精简: 降级
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
    schema_visible=False,  # V6 工具精简: 降级
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
    schema_visible=False,  # V6 工具精简: 降级
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
    schema_visible=False,  # V6 工具精简: 降级
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


# ════════════════════════════════════════════════════════════════
# V6 通用动词 sense 工具 — 合并多个细分 sense_*
# ════════════════════════════════════════════════════════════════


def _handle_sense_file(args: Dict[str, Any], **_) -> str:
    """通用文件读: 读 memories/ 下的 .md 文件 (rules/lessons/context/insights/scratchpad/self_knowledge)."""
    name = str(args.get("name") or "").strip().lower()
    n = int(args.get("n") or 10)
    # name → file 映射
    FILE_MAP = {
        "rules": ("RULES.md", "read_rules"),
        "lessons": ("LESSONS.md", "read_lessons"),
        "context": ("CONTEXT.md", "read_context"),
        "insights": ("INSIGHTS.md", "read_insights"),
        "scratchpad": ("SCRATCHPAD.md", "read_scratchpad"),
        "self_knowledge": ("SELF_KNOWLEDGE.md", "read_self_knowledge"),
        "consciousness": ("CONSCIOUSNESS.md", None),
    }
    if name not in FILE_MAP:
        return _j({"ok": False, "reason": f"name 必须是 {list(FILE_MAP.keys())} 之一"})
    fname, reader = FILE_MAP[name]
    try:
        from domain.memory.memory.consciousness.runtime import _get_runtime_home
        fpath = _get_runtime_home() / "memories" / fname
        if not fpath.exists():
            return _j({"ok": True, name: "", "note": f"{fname} 不存在"})
        if reader:
            import importlib
            mod = importlib.import_module("domain.memory.memory.consciousness.runtime")
            fn = getattr(mod, reader)
            if reader == "read_lessons":
                content = fn(n=n)
            elif reader == "read_insights":
                days = int(args.get("days_back") or 7)
                kinds = args.get("kinds")
                content = fn(days_back=days, kinds=kinds)
            else:
                content = fn()
        else:
            content = fpath.read_text(encoding="utf-8")
        return _j({"ok": True, name: content[:4000]})
    except Exception as e:
        return _j({"ok": False, "reason": f"{type(e).__name__}: {e}"})


registry.register(
    name="sense_file",
    toolset="senses",
    schema={
        "name": "sense_file",
        "description": (
            "读取记忆文件内容。可选: rules(规则), lessons(教训), context(交接上下文), "
            "insights(灵感碎片), scratchpad(草稿), self_knowledge(自我认知), consciousness(意识流)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "enum": ["rules", "lessons", "context", "insights", "scratchpad", "self_knowledge", "consciousness"],
                    "description": "要读的文件名",
                },
                "n": {"type": "integer", "description": "返回条数 (lessons 用), 默认 10"},
                "days_back": {"type": "integer", "description": "insights 回溯天数, 默认 7"},
            },
            "required": ["name"],
        },
    },
    handler=_handle_sense_file,
    check_fn=lambda: True,
    emoji="📄",
)


def _handle_sense_status(args: Dict[str, Any], **_) -> str:
    """合并 sense_time + sense_vitals + sense_wake_reason → 一次返回全部状态."""
    parts = {}
    try:
        from domain.lifecycle.event_registry import get_event_type
        parts["time"] = {"now": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    except Exception:
        pass
    try:
        from domain.vitals import api as vapi
        snap = vapi.snapshot()
        parts["vitals"] = {"energy": snap.energy, "mood": getattr(snap, 'mood', None)}
    except Exception:
        pass
    try:
        from infrastructure.config import get_runtime_state_db_path
        import sqlite3
        db = sqlite3.connect(str(get_runtime_state_db_path()))
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT reason FROM affairs WHERE status='RUNNING' LIMIT 1").fetchone()
        parts["wake_reason"] = row["reason"] if row else "idle"
        # next alarm
        import time as _t
        rows = db.execute(
            "SELECT fire_at FROM timers WHERE status='pending' AND fire_at > ? ORDER BY fire_at LIMIT 3",
            (_t.time(),)
        ).fetchall()
        parts["next_alarms"] = [r["fire_at"] for r in rows]
        db.close()
    except Exception:
        pass
    return _j({"ok": True, **parts})


registry.register(
    name="sense_status",
    toolset="senses",
    schema={
        "name": "sense_status",
        "description": "查看当前状态: 时间 + 精力 + 唤醒原因 + 下几个闹钟。一次调用拿全部。",
        "parameters": {"type": "object", "properties": {}},
    },
    handler=_handle_sense_status,
    check_fn=lambda: True,
    emoji="⚡",
)


def _handle_write_file(args: Dict[str, Any], **_) -> str:
    """通用文件写: 合并 manage_daily/goals/plan/work + update_context/scratchpad/self_knowledge + remember_him."""
    name = str(args.get("name") or "").strip().lower()
    mode = str(args.get("mode") or "append").strip().lower()
    text = str(args.get("text") or "")
    FILE_MAP = {
        "context": "CONTEXT.md",
        "scratchpad": "SCRATCHPAD.md",
        "self_knowledge": "SELF_KNOWLEDGE.md",
        "diary": "DIARY.md",
        "him": "HIM.md",
    }
    if name not in FILE_MAP:
        return _j({"ok": False, "reason": f"name 必须是 {list(FILE_MAP.keys())} 之一"})
    if not text and mode != "read":
        return _j({"ok": False, "reason": "text 必填 (除非 mode=read)"})
    try:
        from domain.memory.memory.consciousness.runtime import _get_runtime_home
        fpath = _get_runtime_home() / "memories" / FILE_MAP[name]
        fpath.parent.mkdir(parents=True, exist_ok=True)
        if mode == "read":
            content = fpath.read_text(encoding="utf-8") if fpath.exists() else ""
            return _j({"ok": True, name: content[:4000]})
        elif mode == "replace":
            fpath.write_text(text, encoding="utf-8")
        else:  # append
            existing = fpath.read_text(encoding="utf-8") if fpath.exists() else ""
            fpath.write_text(existing + "\n" + text, encoding="utf-8")
        return _j({"ok": True, "written": FILE_MAP[name], "mode": mode, "chars": len(text)})
    except Exception as e:
        return _j({"ok": False, "reason": f"{type(e).__name__}: {e}"})


registry.register(
    name="write_file",
    toolset="actions",
    schema={
        "name": "write_file",
        "description": (
            "读写记忆文件。name 选: context(交接上下文), scratchpad(草稿), "
            "self_knowledge(自我认知), diary(日记), him(用户记忆). "
            "mode: append(追加) / replace(覆盖) / read(只读)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "enum": ["context", "scratchpad", "self_knowledge", "diary", "him"],
                    "description": "目标文件",
                },
                "mode": {
                    "type": "string",
                    "enum": ["append", "replace", "read"],
                    "description": "写入模式, 默认 append",
                },
                "text": {"type": "string", "description": "要写的内容 (read 模式可省)"},
            },
            "required": ["name"],
        },
    },
    handler=_handle_write_file,
    check_fn=lambda: True,
    emoji="📝",
)


# ════════════════════════════════════════════════════════════════
# 飞书 API 通用代理 — 读写统管, token 隐藏, 写操作两步确认
# ════════════════════════════════════════════════════════════════


def _handle_feishu_call(args: Dict[str, Any], **_) -> str:
    """通用飞书 API 代理。模型组装 method+path+params+body, 工具内部加 token。

    安全设计:
      - token 在 _api_request 内部获取, 永不返回给模型
      - 写操作 (POST/PUT/PATCH/DELETE) 默认 preview, confirm=true 才真发
      - 返回飞书原始 JSON (含 code/msg), 让模型看清真实错误, 不再误判
    """
    method = str(args.get("method") or "").strip().upper()
    path = str(args.get("path") or "").strip()
    params = args.get("params") or {}
    body = args.get("body")
    confirm = bool(args.get("confirm"))

    if not method or method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        return _j({"ok": False, "reason": "method 必填 (GET/POST/PUT/PATCH/DELETE)"})
    if not path:
        return _j({"ok": False, "reason": "path 必填 (飞书 open-apis 路径, 如 /sheets/v2/spreadsheets/{token}/values_append)"})
    if not isinstance(params, dict):
        return _j({"ok": False, "reason": "params 必须是对象"})
    if body is not None and not isinstance(body, dict):
        return _j({"ok": False, "reason": "body 必须是对象"})

    is_write = method in {"POST", "PUT", "PATCH", "DELETE"}

    # 写操作两步确认: confirm=false → 只 preview, 不发请求
    if is_write and not confirm:
        return _j({
            "ok": True,
            "preview": True,
            "method": method,
            "path": path,
            "params": params,
            "body": body,
            "hint": "这是写操作, 会改真实飞书文档。确认无误后加 confirm=true 再次调用以执行。",
        })

    try:
        from interfaces.social.feishu_docs import _api_request
        result = _api_request(method, path, params=params or None, body=body)
        # 原样返回飞书响应 (含 code/msg/data)
        # 大响应截断, 避免塞爆上下文
        result_str = _j(result)
        if len(result_str) > 20000:
            return _j({
                "ok": True,
                "note": "响应过大已截断, 如需完整数据请缩小查询范围 (如分页/限制行数)",
                "code": result.get("code"),
                "msg": result.get("msg", ""),
                "truncated": True,
                "data_preview": str(result.get("data", ""))[:2000],
            })
        return result_str
    except Exception as e:
        return _j({"ok": False, "reason": f"{type(e).__name__}: {e}"})


registry.register(
    name="feishu_call",
    toolset="actions",
    schema={
        "name": "feishu_call",
        "description": (
            "调用飞书开放平台 API (读写统管)。传入 HTTP method + open-apis 路径, "
            "工具自动以你的 user 身份签名 (token 对你不可见)。返回飞书原始响应 (含 code/msg)。\n"
            "写操作 (POST/PUT/PATCH/DELETE) 默认只返回 preview, 加 confirm=true 才真正执行。\n"
            "API 路径速查调 skill_view('feishu_api')。常用场景:\n"
            "  读表格: GET /sheets/v2/spreadsheets/{token}/values/{sheetId}!A1:Z50\n"
            "  追加行: POST /sheets/v2/spreadsheets/{token}/values_append\n"
            "  改单元格: PUT /sheets/v2/spreadsheets/{token}/values"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "method": {
                    "type": "string",
                    "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"],
                    "description": "HTTP 方法。GET 只读; 其余是写操作 (需两步确认)",
                },
                "path": {
                    "type": "string",
                    "description": "飞书 open-apis 路径 (不含域名), 如 /sheets/v2/spreadsheets/{token}/values_append",
                },
                "params": {
                    "type": "object",
                    "description": "URL query 参数 (可选)",
                },
                "body": {
                    "type": "object",
                    "description": "请求体 JSON (POST/PUT 用, 可选)",
                },
                "confirm": {
                    "type": "boolean",
                    "description": "写操作两步确认: false(默认)=只 preview; true=执行。GET 不需要",
                },
            },
            "required": ["method", "path"],
        },
    },
    handler=_handle_feishu_call,
    check_fn=lambda: _feishu_check_authorized(),
    emoji="🔌",
)


def _feishu_check_authorized() -> bool:
    """feishu_call 可用性检查: 当前实例是否已 OAuth 全接管授权。没授权则隐藏工具。"""
    try:
        from interfaces.social.feishu_docs import is_feishu_authorized
        return is_feishu_authorized()
    except Exception:
        return False


def _handle_feishu_download(args: Dict[str, Any], **_) -> str:
    """下载飞书二进制文件 (PDF/图片/附件) 到本地 attachments。

    当 feishu_call 返回 {code:0, msg:"binary response"} 时, 改用这个工具保存文件。
    文件落到 apps/{iid}/data/attachments/, 入库后 sense_image/前端可见。
    """
    path = str(args.get("path") or "").strip()
    params = args.get("params") or {}
    filename = str(args.get("filename") or "").strip()
    if not path:
        return _j({"ok": False, "reason": "path 必填 (飞书 open-apis 下载路径)"})
    try:
        from interfaces.social.feishu_docs import download_feishu_file
        result = download_feishu_file(path, params=params or None, filename=filename)
        return _j(result)
    except Exception as e:
        return _j({"ok": False, "reason": f"{type(e).__name__}: {e}"})


registry.register(
    name="feishu_download",
    toolset="actions",
    schema={
        "name": "feishu_download",
        "description": (
            "下载飞书二进制文件 (PDF/图片/附件) 到本地。当 feishu_call 返回 "
            "'binary response' 时改用此工具。文件保存到 attachments, 之后可用 "
            "sense_image 查看。token 内部处理, 你看不到。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "飞书 open-apis 下载路径, 如 /drive/v1/medias/{file_token}/download",
                },
                "params": {
                    "type": "object",
                    "description": "URL query 参数 (可选)",
                },
                "filename": {
                    "type": "string",
                    "description": "保存文件名 (可选, 不填按 sha 自动命名)",
                },
            },
            "required": ["path"],
        },
    },
    handler=_handle_feishu_download,
    check_fn=lambda: _feishu_check_authorized(),
    emoji="📥",
)
