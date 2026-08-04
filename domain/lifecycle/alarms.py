"""闹钟系统 —— 独立的时间事件调度器。

所有"在将来某个时间推事件"的需求统一走这里。子系统设闹钟，闹钟
自己轮询，到期后 emit_event() 将事件推入事件队列。

与事件队列的关系：
  子系统 → set_alarm() → timers 表
  核心 tick → fire_due_alarms() → emit_event() → events 表
  核心 tick → pop_due_events() → 唤醒
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta  # noqa: E402 — module-level for sub-function lookup
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    from domain.lifecycle.clock import now_iso
    return now_iso()


def _beijing_iso(dt: datetime) -> str:
    """Convert a tz-aware datetime to Beijing ISO (display layer only)."""
    from domain.lifecycle.clock import BEIJING
    return dt.astimezone(BEIJING).isoformat(timespec="seconds")


def _conn():
    from domain.lifecycle.affairs.runtime import _conn as _c
    return _c()


def set_alarm(
    event_kind: str,
    fire_at: str,
    payload: Optional[Dict[str, Any]] = None,
) -> int:
    """设置一个闹钟。到期后 alarm 系统会 emit_event(event_kind, payload)。

    Args:
        event_kind: 闹钟到期后 emit 的事件类型
        fire_at: ISO 时间字符串，闹钟触发时间
        payload: 事件 payload

    Returns:
        alarm_id
    """
    from domain.lifecycle.affairs.runtime import init_db
    init_db()

    payload_json = json.dumps(payload or {}, ensure_ascii=False)
    now = _now_iso()

    with _conn() as conn:
        # V6 闹钟去重: 同一 fire_at 只保留一个, 不管 event_kind
        # (之前只按 event_kind+fire_at 去重, 导致 routine 闹钟和 rest timer 在同一时间点重复)
        #
        # 注意: 只查 pending (fired_at IS NULL) 的。已触发/已取消的 timer 已过期,
        # 不该被复用——14:45 的 timer 触发后, 时间点已过, rest(until=14:45) 无意义。
        # 如果模型反复 rest 到同一过期时间, 根因在 rest 逻辑或模型判断, 不是去重。
        existing = conn.execute(
            "SELECT id FROM timers WHERE fire_at = ? AND fired_at IS NULL",
            (fire_at,),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE timers SET payload_json = ? WHERE id = ?",
                (payload_json, existing[0]),
            )
            logger.debug("Alarm deduped: id=%d kind=%s fire_at=%s", existing[0], event_kind, fire_at)
            return existing[0]

        cur = conn.execute(
            "INSERT INTO timers (event_kind, fire_at, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (event_kind, fire_at, payload_json, now),
        )
        alarm_id = cur.lastrowid
        logger.debug("Alarm set: id=%d kind=%s fire_at=%s", alarm_id, event_kind, fire_at)
    return alarm_id


def cancel_alarm(alarm_id: int) -> bool:
    """取消单个闹钟（标记为已触发但不 emit 事件）。"""
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE timers SET fired_at = ? WHERE id = ? AND fired_at IS NULL",
            (_now_iso(), alarm_id),
        )
        ok = cur.rowcount > 0
        if ok:
            logger.debug("Alarm cancelled: id=%d", alarm_id)
        return ok


def cancel_alarms_by_kind(event_kind: str) -> int:
    """按事件类型批量取消未触发的闹钟。返回取消数量。

    用于 express_to_human 清除旧的 awaiting_reply 闹钟。
    """
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE timers SET fired_at = ? WHERE event_kind = ? AND fired_at IS NULL",
            (_now_iso(), event_kind),
        )
        n = cur.rowcount
        if n > 0:
            logger.debug("Cancelled %d alarms: kind=%s", n, event_kind)
        return n


def cancel_alarms_by_filter(
    kind: str,
    payload_filter: Optional[Dict[str, Any]] = None,
) -> int:
    """按条件取消闹钟。payload_filter 中的 key=value 对必须在 payload 中匹配。

    用于 express_to_human 清除同通道旧 awaiting_reply + task_runtime 清特定 task_id。

    V6.1 (2026-08-04): 不再只清 fired_at IS NULL 的, 已触发的也清。
    之前只清 pending → 已触发的旧 awaiting_reply 留在表里 →
    express 发新消息时 cancel 清不到旧的 → set_alarm 又建新的 →
    同 channel 累积十几个 awaiting_reply timer。
    """
    if not payload_filter:
        return cancel_alarms_by_kind(kind)

    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, payload_json FROM timers WHERE event_kind = ?",
            (kind,),
        ).fetchall()

        matched = 0
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except Exception:
                continue
            if all(
                str(payload.get(k)) == str(v)
                for k, v in payload_filter.items()
            ):
                conn.execute(
                    "UPDATE timers SET fired_at = ? WHERE id = ?",
                    (_now_iso(), row["id"]),
                )
                matched += 1

        if matched > 0:
            logger.debug("Cancelled %d alarms: kind=%s filter=%s", matched, kind, payload_filter)
        return matched


def fire_due_alarms() -> List[Dict[str, Any]]:
    """扫描所有到期的闹钟，emit 对应事件，标记已触发。

    每次 cron tick 调用一次。

    Returns:
        触发的事件列表 [{event_kind, event_id, payload}]
    """
    from domain.lifecycle.events import emit_event
    from domain.lifecycle.affairs.runtime import init_db

    init_db()
    now = _now_iso()
    fired: list[dict] = []

    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, event_kind, payload_json, fire_at FROM timers "
            "WHERE fired_at IS NULL AND fire_at <= ? "
            "ORDER BY fire_at ASC",
            (now,),
        ).fetchall()

        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except Exception:
                payload = {}

            try:
                # 传递 fire_at 给 emit_event，确保 events 表有正确的触发时间
                event_id = emit_event(
                    kind=row["event_kind"],
                    payload=payload,
                    fire_at=row["fire_at"],
                )
                fired.append({
                    "event_kind": row["event_kind"],
                    "event_id": event_id,
                    "payload": payload,
                })
            except Exception as exc:
                # Keep the exception logger.info rather than logger.exception —
                # now that the SQL column bug is fixed the except block rarely
                # fires. If you see this in logs again, switch back to
                # logger.exception to get the stack trace.
                logger.warning(
                    "Alarm fire failed: kind=%s id=%d: %s",
                    row["event_kind"], row["id"], exc,
                )

            conn.execute(
                "UPDATE timers SET fired_at = ? WHERE id = ?",
                (now, row["id"]),
            )

    if fired:
        logger.info("Alarms fired: %d events", len(fired))
    return fired


def list_pending_alarms(kind: Optional[str] = None) -> List[Dict[str, Any]]:
    """列出所有未触发的闹钟（调试用）。"""
    with _conn() as conn:
        if kind:
            rows = conn.execute(
                "SELECT * FROM timers WHERE fired_at IS NULL AND event_kind = ? ORDER BY fire_at",
                (kind,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM timers WHERE fired_at IS NULL ORDER BY fire_at",
            ).fetchall()
        return [dict(r) for r in rows]


def get_schedule_overview(days_ahead: int = 7) -> Dict[str, Any]:
    """聚合日程视图：一次性闹钟 + 未来作息 routine。

    返回结构：
      alarms: [{id, event_kind, fire_at, reason, from_routine}]
      recurring: [{id, name, time, description}]  # 合并去重后的作息
      next_wake: str | None  # ISO 时间字符串，最近一次唤醒时间
    """
    import json as _json
    from domain.lifecycle.clock import now_dt as _now_dt, parse_iso as _parse_iso
    from domain.lifecycle.routine_scheduler import load_routines

    now = _now_dt()
    future = now + timedelta(days=days_ahead)
    future_iso = future.isoformat(timespec="seconds")

    # 一次性闹钟（timer / awaiting_reply / routine 的今日/近期事件）
    pending = list_pending_alarms()
    alarms = []
    for row in pending:
        fire_str = row.get("fire_at", "")
        if not fire_str or fire_str > future_iso:
            continue
        try:
            payload = _json.loads(row["payload_json"]) if isinstance(row.get("payload_json"), str) else row.get("payload_json", {})
        except Exception:
            payload = {}
        reason = payload.get("reason") or payload.get("name") or payload.get("description") or ""
        alarms.append({
            "id": row["id"],
            "event_kind": row["event_kind"],
            "fire_at": fire_str,
            "reason": reason,
            "from_routine": row["event_kind"] == "routine",
        })

    # 未来作息的合并去重（只取启用的）
    # V6: 如果某时间点已有 pending alarm, 标注 has_alarm (前端不重复显示)
    pending_times = set()
    for a in alarms:
        # 提取 HH:MM 部分
        fa = a.get("fire_at", "")
        if "T" in fa:
            pending_times.add(fa[11:16])  # HH:MM

    routines = [r for r in load_routines() if r.get("enabled", True)]
    recurring = []
    seen_times = {}  # time -> first entry
    for r in routines:
        t = r.get("time", "")
        if t not in seen_times:
            seen_times[t] = r
    for t, r in sorted(seen_times.items()):
        recurring.append({
            "id": r["id"],
            "name": r["name"],
            "time": t,
            "description": r.get("description", ""),
            "has_alarm": t in pending_times,  # V6: 该时间点已有闹钟
        })

    # 最近一次唤醒时间
    next_wake = None
    if alarms:
        next_wake = alarms[0]["fire_at"]

    return {
        "alarms": alarms,
        "recurring": recurring,
        "next_wake": next_wake,
        "query_time": _beijing_iso(now),
        "days_ahead": days_ahead,
    }


def format_schedule_for_human(overview: Dict[str, Any]) -> str:
    """将日程概览格式化为自然语言，像人记事那样分段。

    分段方式：今晚(今天已过) / 今天剩余 / 明天 / 本周作息
    """
    from domain.lifecycle.clock import now_dt as _now_dt, parse_iso as _parse_iso

    now = _now_dt()
    today_str = now.strftime("%Y-%m-%d")
    today_date = now.date()

    alarms = overview.get("alarms", [])
    recurring = overview.get("recurring", [])
    next_wake = overview.get("next_wake")

    # 精力恢复说明：定性表达（不精确算时间），让模型知道休息后不只有固定闹钟会叫醒，
    # 精力恢复到阈值 + 空闲足够也会以 initiative 形式自然苏醒。两条返回路径都带这段。
    vital_hint = (
        "🌱 **精力恢复**\n"
        "  · 休息时精力会慢慢恢复，大致 1-2 小时后会以「主动探索」形式自然苏醒，"
        "可能比闹钟更早醒来"
    )

    if not alarms and not recurring:
        return f"📅 近期没有任何已注册的闹钟或作息。\n\n{vital_hint}"

    parts = []

    # 分段：将 alarms 按时间段分组
    tonight_items = []
    today_items = []
    tomorrow_items = []
    later_items = []

    for a in alarms:
        try:
            fire_dt = _parse_iso(a["fire_at"])
        except Exception:
            continue
        fire_date = fire_dt.date()
        fire_h = fire_dt.hour

        # 渲染每条闹钟时带 id（#<id>）——rest 工具 reuse 入参需要这个 id，
        # 让模型无需再调 sense_schedule 就能在休息时复用现有闹钟。
        alarm_id = a.get("id")
        id_tag = f" (#{alarm_id})" if alarm_id else ""
        key = f'{fire_dt.strftime("%H:%M")} → {a["reason"] or a["event_kind"]}{id_tag}'

        if fire_date == today_date:
            if fire_h >= 20 or fire_h < 4:
                tonight_items.append(key)
            else:
                today_items.append(key)
        elif fire_date == today_date + timedelta(days=1):
            tomorrow_items.append(key)
        else:
            later_items.append(key)

    if tonight_items:
        parts.append("🌙 **今晚**")
        for item in tonight_items:
            parts.append(f"  · {item}")
    if today_items:
        parts.append("📅 **今天**")
        for item in today_items:
            parts.append(f"  · {item}")
    if tomorrow_items:
        parts.append("⏳ **明天**")
        for item in tomorrow_items:
            parts.append(f"  · {item}")
    if later_items:
        parts.append("📆 **更晚**")
        for item in later_items:
            parts.append(f"  · {item}")

    # 作息（合并去重后）
    if recurring:
        parts.append("🔄 **每日作息**（已注册）")
        for r in recurring:
            parts.append(f"  · {r['time']} → {r['name']}")

    if next_wake:
        try:
            next_dt = _parse_iso(next_wake)
            rel = next_dt - now
            mins = int(rel.total_seconds() / 60)
            if mins < 60:
                rel_str = f"{mins}分钟后"
            elif mins < 120:
                rel_str = "约1小时后"
            else:
                rel_str = f"约{mins // 60}小时后"
            parts.insert(0, f"⏰ 最近唤醒：{next_dt.strftime('%H:%M')}（{rel_str}）")
        except Exception:
            pass

    # 精力恢复说明放在最后（在作息、next_wake 之后），作为通用提示。
    parts.append(vital_hint)

    return "\n".join(parts)
