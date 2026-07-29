"""Employee console monitor workflow for the current digital employee instance."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from application.contracts import UseCaseResult
from application.runtime_provider import console_runtime_adapter
from infrastructure.config import get_runtime_config_path, get_runtime_memories_dir, get_runtime_state_db_path


def _unused_legacy_wake_worker_removed() -> None:
    """Placeholder.

    The old ``_try_wake_for_nurture`` helper was removed in
    ``refactor/emit-driven-wake``: emit_event now also triggers wake decision
    (or mid-session injection for RUNNING affairs). The webhook-style emit
    architecture collapses all caller-side wake decisions into a single
    canonical path.If you need instance context to be set (e.g. inside a
    request handler thread), set it via ``set_instance_context`` /
    ``set_current_instance_id`` before calling emit_event.
    """


class MonitorConsoleWorkflow:
    """Coordinate monitor reads/actions without owning HTTP details."""

    def _db(self) -> Optional[sqlite3.Connection]:
        try:
            conn = sqlite3.connect(str(get_runtime_state_db_path()))
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            return conn
        except Exception:
            return None

    def status(self) -> UseCaseResult:
        try:
            from domain.execution.semantics import get_vitals_engine

            state = get_vitals_engine().get_state()
            affair_data = None
            db = self._db()
            if db:
                try:
                    affair = self._fetch_life_affair(db)
                    affair_data = dict(affair) if affair else None
                    if affair_data and "meta_json" in affair_data:
                        try:
                            affair_data["meta"] = json.loads(affair_data.pop("meta_json"))
                        except Exception:
                            pass
                    state["affair"] = affair_data
                    affair_id = affair["affair_id"] if affair else ""
                    state["wait_intent"] = self._fetch_wait_intent(db, affair_id)
                    state["last_heartbeat"] = self._fetch_last_heartbeat(db)
                finally:
                    db.close()
            state["runtime"] = self._build_runtime_status(state, affair_data)
            return UseCaseResult(state)
        except Exception as exc:
            return UseCaseResult({"error": str(exc)}, 500)

    def _build_runtime_status(self, state: dict[str, Any], affair: dict[str, Any] | None) -> dict[str, Any]:
        energy = self._number(state.get("energy"), 0.0)
        mode = self._runtime_mode(energy, affair)
        recovery_rate = self._recovery_rate(state)
        return {
            "energy": round(energy),
            "energy_segment": state.get("segment") or self._energy_segment(energy),
            "mode": mode,
            "mode_label": self._runtime_mode_label(mode),
            "last_rest_at": self._last_rest_at(state),
            "recovery_rate": recovery_rate,
            "estimated_full_at": self._estimate_full_at(energy, recovery_rate),
            "recommendation": self._runtime_recommendation(energy, mode),
            "policy": self._runtime_policy(energy),
            "workload": self._workload_budget(energy),
            "recent_energy_events": self._recent_energy_events(),
            "event_queue": self._event_queue_summary(),
        }

    @staticmethod
    def _number(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _runtime_mode(self, energy: float, affair: dict[str, Any] | None) -> str:
        status = str((affair or {}).get("status") or "").lower()
        if status == "running":
            return "working"
        if energy < 20:
            return "resting"
        if status == "blocked":
            return "blocked"
        if energy < 35:
            return "conserving"
        return "idle"

    @staticmethod
    def _runtime_mode_label(mode: str) -> str:
        return {
            "working": "工作中",
            "resting": "恢复中",
            "blocked": "阻塞",
            "conserving": "节能",
            "idle": "待命",
        }.get(mode, mode)

    @staticmethod
    def _energy_segment(energy: float) -> str:
        from domain.vital.simulation.engine import ENERGY_SEGMENTS
        for name, lo, hi, _ in ENERGY_SEGMENTS:
            if lo <= energy <= hi:
                return name
        return "未知"

    def _recovery_rate(self, state: dict[str, Any]) -> float:
        from domain.vital.simulation.engine import ENERGY_RECOVERY_PER_HOUR
        return ENERGY_RECOVERY_PER_HOUR

    @staticmethod
    def _last_rest_at(state: dict[str, Any]) -> Any:
        wait_intent = state.get("wait_intent") or {}
        heartbeat = state.get("last_heartbeat") or {}
        return wait_intent.get("resume_when") or heartbeat.get("fired_at")

    def _estimate_full_at(self, energy: float, recovery_rate: float) -> str | None:
        if energy >= 100 or recovery_rate <= 0:
            return None
        hours = max(0.0, (100 - energy) / recovery_rate)
        from domain.lifecycle import clock as _clock
        eta = _clock.now_dt() + timedelta(hours=hours)
        return eta.isoformat(timespec="seconds")

    @staticmethod
    def _runtime_recommendation(energy: float, mode: str) -> str:
        if energy < 20:
            return "精力已进入耗尽区，建议优先休息，不启动新的长任务。"
        if energy < 35:
            return "精力偏低，只适合轻量检查、短回复和状态整理。"
        if energy < 55:
            return "精力有限，适合推进明确的小任务，避免连续工具调用。"
        if mode == "blocked":
            return "当前阻塞，建议先处理等待条件或向用户简短同步状态。"
        if energy >= 80:
            return "精力充足，可以处理深度任务或主动推进计划。"
        return "精力稳定，适合处理中等负载任务并保持节奏。"

    @staticmethod
    def _runtime_policy(energy: float) -> dict[str, Any]:
        return {
            "auto_rest_below": 20,
            "light_work_below": 35,
            "deep_work_above": 70,
            "current_band": "deep" if energy >= 70 else "normal" if energy >= 35 else "light" if energy >= 20 else "rest",
        }

    @staticmethod
    def _workload_budget(energy: float) -> dict[str, Any]:
        return {
            "light": energy >= 20,
            "medium": energy >= 45,
            "deep": energy >= 70,
        }

    def _recent_energy_events(self) -> list[dict[str, Any]]:
        try:
            rows = console_runtime_adapter().recent_nurture_log(hours=72)
        except Exception:
            return []
        events = []
        for row in rows:
            deltas = row.get("deltas") or {}
            if "energy" not in deltas:
                continue
            events.append({
                "at": row.get("at"),
                "source": row.get("source") or row.get("kind") or "runtime",
                "delta": deltas.get("energy"),
                "summary": row.get("raw_text") or self._energy_delta_summary(deltas.get("energy")),
            })
            if len(events) >= 8:
                break
        return events

    @staticmethod
    def _energy_delta_summary(delta: Any) -> str:
        value = MonitorConsoleWorkflow._number(delta, 0.0)
        if value > 0:
            return f"恢复精力 +{value:g}"
        if value < 0:
            return f"消耗精力 {value:g}"
        return "精力无变化"

    def _event_queue_summary(self) -> dict[str, Any]:
        """未消费事件摘要，特别标注会阻塞 express_to_human 的人类/群聊消息。"""
        try:
            from domain.lifecycle.events import pop_due_events
            events = pop_due_events(limit=200)
        except Exception:
            return {"total": 0, "messages": 0, "group_messages": 0, "items": []}

        kinds = {}
        items = []
        for ev in events:
            kind = ev.get("kind", "unknown")
            kinds[kind] = kinds.get(kind, 0) + 1
            if kind in ("message", "group_message"):
                payload = ev.get("payload", {})
                items.append({
                    "event_id": ev.get("event_id"),
                    "kind": kind,
                    "preview": (payload.get("text") or "")[:60],
                    "sender": payload.get("sender_name", ""),
                    "at": ev.get("created_at") or ev.get("at", ""),
                })

        return {
            "total": len(events),
            "messages": kinds.get("message", 0),
            "group_messages": kinds.get("group_message", 0),
            "by_kind": kinds,
            "unread_items": items[:5],
            "blocks_express": bool(kinds.get("message", 0) or kinds.get("group_message", 0)),
        }

    @staticmethod
    def _fetch_life_affair(db: sqlite3.Connection) -> Any:
        try:
            return db.execute(
                "SELECT affair_id, goal, status, updated_at, meta_json FROM affairs "
                "WHERE meta_json LIKE '%life%'"
            ).fetchone()
        except Exception:
            return None

    @staticmethod
    def _fetch_wait_intent(db: sqlite3.Connection, affair_id: str) -> dict[str, Any] | None:
        try:
            row = db.execute("SELECT * FROM wait_intents WHERE affair_id=?", (affair_id,)).fetchone()
            return dict(row) if row else None
        except Exception:
            return None

    @staticmethod
    def _fetch_last_heartbeat(db: sqlite3.Connection) -> dict[str, Any] | None:
        try:
            row = db.execute("SELECT fired_at, notes FROM heartbeats ORDER BY hb_id DESC LIMIT 1").fetchone()
            return dict(row) if row else None
        except Exception:
            return None

    @staticmethod
    def parse_memory_dates(content: str) -> list[str]:
        year = datetime.now().year
        dates = set()
        for line in content.splitlines():
            match = re.match(r"^##\s*(\d{4}-\d{2}-\d{2})T", line)
            if match:
                dates.add(match.group(1))
                continue
            match = re.match(r"^\[.+?\]\s*(\d+)月(\d+)日", line)
            if match:
                dates.add(f"{year}-{int(match.group(1)):02d}-{int(match.group(2)):02d}")
                continue
            match = re.match(r"^##\s*(\d+)/(\d+)\s+\d+:\d+", line)
            if match:
                dates.add(f"{year}-{int(match.group(1)):02d}-{int(match.group(2)):02d}")
        return sorted(dates, reverse=True)

    @staticmethod
    def slice_memory_by_date(content: str, date: str) -> str:
        month_day = f"{int(date[5:7])}月{int(date[8:10])}日"
        short_md = f"{int(date[5:7])}/{int(date[8:10])}"
        collected: list[str] = []
        inside = False
        for line in content.splitlines():
            if re.match(r"^##\s*\d{4}-\d{2}-\d{2}T", line):
                if line[3:13] == date:
                    inside = True
                elif inside:
                    break
            elif re.match(r"^\[.+?\]\s*\d+月\d+日", line):
                if month_day in line:
                    inside = True
                elif inside:
                    break
            elif re.match(r"^##\s*\d+/\d+\s+\d+:\d+", line):
                if short_md in line[:10]:
                    inside = True
                elif inside:
                    break
            if inside:
                collected.append(line)
        return "\n".join(collected)

    @staticmethod
    def _memory_file_map() -> dict[str, str]:
        # 前端 MemoriesTab 展示的 kind 必须全部在这里映射,否则 404。
        # 已退役(goals/him)从可选 list 移除,read_goals/read_about_him 改返退役提示。
        return {
            "consciousness": "CONSCIOUSNESS.md",
            "consciousness_archive": "CONSCIOUSNESS.archive.md",  # 老的意识流归档
            "scratchpad":    "SCRATCHPAD.md",
            "sent_log":      "SENT_LOG.md",
            "plans":         "PLANS.md",
            "daily":         "DAILY.md",
            "rules":         "RULES.md",
            "context":       "CONTEXT.md",
            "lessons":       "LESSONS.md",
            "insights":      "INSIGHTS.md",
            "user":          "USER.md",
            "memory":        "MEMORY.md",
        }

    def memory_dates(self, name: str) -> UseCaseResult:
        try:
            mem_dir = get_runtime_memories_dir()
            if name == "diary":
                return UseCaseResult({"dates": self._diary_dates(mem_dir)})
            filename = self._memory_file_map().get(name)
            if not filename:
                return UseCaseResult({"error": f"unknown memory: {name}"}, 404)
            path = mem_dir / filename
            if not path.exists():
                return UseCaseResult({"dates": []})
            return UseCaseResult({"dates": self.parse_memory_dates(path.read_text(encoding="utf-8"))})
        except Exception as exc:
            return UseCaseResult({"error": str(exc)}, 500)

    def memory_content(self, name: str, date_filter: str | None = None) -> UseCaseResult:
        try:
            mem_dir = get_runtime_memories_dir()
            if name == "diary":
                return self._diary_content(mem_dir, date_filter)
            filename = self._memory_file_map().get(name)
            if not filename:
                return UseCaseResult({"error": f"unknown memory: {name}"}, 404)
            path = mem_dir / filename
            # 意识流特殊处理: dream 整理把老内容切到 .archive.md, UI 想看全量历史必须合并
            # 不再做单独 archive tab —— "归档" 是实现细节, 用户视角是同一个意识流时间序列
            parts: list[str] = []
            archive_text = ""
            if name == "consciousness":
                archive_path = mem_dir / "CONSCIOUSNESS.archive.md"
                if archive_path.exists():
                    archive_text = archive_path.read_text(encoding="utf-8").strip()
                if path.exists():
                    cur_text = path.read_text(encoding="utf-8").strip()
                else:
                    cur_text = ""
                # 当前内容在前(最新), archive 追加在后(较老); 前端按天分组倒序自然正确
                parts = [p for p in [cur_text, archive_text] if p]
                content = "\n\n".join(parts)
            elif path.exists():
                content = path.read_text(encoding="utf-8")
            else:
                content = ""
            dates = self.parse_memory_dates(content)
            if date_filter:
                content = self.slice_memory_by_date(content, date_filter)
            return UseCaseResult({"content": content, "name": name, "dates": dates})
        except Exception as exc:
            return UseCaseResult({"error": str(exc)}, 500)

    def _diary_dates(self, mem_dir: Any) -> list[str]:
        diary_dir = mem_dir / "diary"
        dates: list[str] = []
        if diary_dir.exists():
            for path in sorted(diary_dir.glob("????-??-??.md"), reverse=True):
                match = re.match(r"(\d{4}-\d{2}-\d{2})", path.stem)
                if match:
                    dates.append(match.group(1))
        old = mem_dir / "DIARY.md"
        if old.exists():
            for date in self.parse_memory_dates(old.read_text(encoding="utf-8")):
                if date not in dates:
                    dates.append(date)
            dates.sort(reverse=True)
        return dates

    def _diary_content(self, mem_dir: Any, date_filter: str | None) -> UseCaseResult:
        diary_dir = mem_dir / "diary"
        dates = self._diary_dates(mem_dir)
        old = mem_dir / "DIARY.md"
        if date_filter:
            daily_file = diary_dir / f"{date_filter}.md"
            if daily_file.exists():
                return UseCaseResult({"content": daily_file.read_text(encoding="utf-8"), "name": "diary", "dates": dates})
            if old.exists():
                content = self.slice_memory_by_date(old.read_text(encoding="utf-8"), date_filter)
                return UseCaseResult({"content": content, "name": "diary", "dates": dates})
            return UseCaseResult({"content": "", "name": "diary", "dates": dates})
        # 默认: 合并所有日记文件 (最新在前), 让前端按天分组展示历史全量
        # 以前只返回 dates[0] 一天的内容, 用户看到 "日记为啥只有很少"
        if dates:
            parts: list[str] = []
            for d in dates:
                daily_file = diary_dir / f"{d}.md"
                if daily_file.exists():
                    txt = daily_file.read_text(encoding="utf-8").strip()
                    if txt:
                        parts.append(txt)
            if old.exists():
                legacy = old.read_text(encoding="utf-8").strip()
                if legacy:
                    parts.append(f"## (legacy DIARY.md)\n\n{legacy}")
            return UseCaseResult({"content": "\n\n".join(parts), "name": "diary", "dates": dates})
        return UseCaseResult({"content": "", "name": "diary", "dates": dates})

    def events(self) -> UseCaseResult:
        db = self._db()
        if not db:
            return UseCaseResult({"error": "state.db unavailable"}, 500)
        try:
            # 未消费事件
            unconsumed_rows = db.execute(
                "SELECT event_id, channel, payload, created_at, consumed_at, fire_at, kind "
                "FROM events WHERE consumed_at IS NULL ORDER BY created_at DESC LIMIT 50"
            ).fetchall()
            unconsumed = [self._event_dict(row) for row in unconsumed_rows]

            # 已消费事件（近 200 条），直接使用 consumed_by_session_id 列
            consumed_rows = db.execute(
                "SELECT event_id, channel, payload, created_at, consumed_at, fire_at, kind, "
                "consumed_by_session_id FROM events "
                "WHERE consumed_at IS NOT NULL ORDER BY consumed_at DESC LIMIT 200"
            ).fetchall()
            consumed = []
            for row in consumed_rows:
                event = self._event_dict(row)
                event["consumed_by_session_id"] = row["consumed_by_session_id"]
                consumed.append(event)

            return UseCaseResult({
                "unconsumed": unconsumed,
                "consumed": consumed,
            })
        except Exception as exc:
            return UseCaseResult({"error": str(exc)}, 500)
        finally:
            db.close()

    @staticmethod
    def _event_dict(row: Any) -> dict[str, Any]:
        event = dict(row)
        try:
            event["payload"] = json.loads(event.get("payload") or "{}")
        except Exception:
            pass
        return event

    def event_queue(self) -> UseCaseResult:
        db = self._db()
        if not db:
            return UseCaseResult({"error": "state.db unavailable"}, 500)
        try:
            from domain.lifecycle import clock as _clock
            tz8 = _clock.BEIJING
            now = _clock.beijing_now_dt()
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            today_end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
            # now_iso 用于与存储中的 UTC ISO 字段做字典序比较：必须也是 UTC。
            now_iso = _clock.now_iso()
            rows = db.execute(
                "SELECT event_id, channel, payload, created_at, consumed_at, fire_at, kind "
                "FROM events WHERE consumed_at IS NULL ORDER BY "
                "CASE WHEN fire_at IS NULL OR fire_at <= ? THEN 0 ELSE 1 END, "
                "fire_at ASC, event_id ASC LIMIT 200",
                (now_iso,),
            ).fetchall()
            events = [self._event_dict(row) for row in rows] + self._wait_intent_events(db)
            today_events, upcoming_events = self._classify_events(events, today_start, today_end, now_iso, tz8)
            return UseCaseResult({
                "today_events": today_events,
                "upcoming_events": upcoming_events,
                "routine_summary": self._routine_summary(today_events),
            })
        except Exception as exc:
            return UseCaseResult({"error": str(exc)}, 500)
        finally:
            db.close()

    @staticmethod
    def _wait_intent_events(db: sqlite3.Connection) -> list[dict[str, Any]]:
        try:
            rows = db.execute(
                "SELECT affair_id, wait_type, resume_when, reason, resume_action, meta_json "
                "FROM wait_intents WHERE wait_type='until' AND resume_when IS NOT NULL"
            ).fetchall()
        except Exception:
            return []
        events: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                meta = json.loads(item.get("meta_json") or "{}")
            except Exception:
                meta = {}
            events.append({
                "event_id": f"wait_intent:{item.get('affair_id')}",
                "channel": "timer",
                "payload": {
                    "reason": item.get("reason") or "",
                    "resume_action": item.get("resume_action") or "",
                    "affair_id": item.get("affair_id"),
                    **meta,
                },
                "created_at": None,
                "consumed_at": None,
                "fire_at": item.get("resume_when"),
                "kind": "timer",
            })
        return events

    def _classify_events(
        self,
        events: list[dict[str, Any]],
        today_start: datetime,
        today_end: datetime,
        now_iso: str,
        tz8: timezone,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        today_events: list[dict[str, Any]] = []
        upcoming_events: list[dict[str, Any]] = []
        for event in events:
            payload = event.get("payload") or {}
            text = payload.get("text", "") or payload.get("item", "") or ""
            if not text and event.get("kind") == "vital_threshold":
                text = "{} → {} ({})".format(payload.get("from_seg", ""), payload.get("to_seg", ""), payload.get("dimension", ""))
            elif not text and event.get("kind") == "task_reminder":
                text = payload.get("content", "")
            elif not text and event.get("kind") == "timer":
                text = f"⏰ {payload.get('reason') or '休息'} → 醒来"
            elif not text and event.get("kind") == "routine":
                text = payload.get("name", "") or payload.get("description", "") or "作息事件"
            event["text"] = text
            fire = event.get("fire_at")
            event["due"] = fire is None or fire <= now_iso
            event["recurring"] = event.get("kind") in {"vital_threshold", "task_reminder", "routine"}
            if fire:
                try:
                    fire_time = datetime.fromisoformat(fire)
                    if fire_time.tzinfo is None:
                        fire_time = fire_time.replace(tzinfo=tz8)
                    if today_start <= fire_time <= today_end:
                        today_events.append(event)
                    else:
                        upcoming_events.append(event)
                    continue
                except Exception:
                    pass
            today_events.append(event)
        return today_events, upcoming_events

    @staticmethod
    def _routine_summary(today_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        from domain.lifecycle.routine_scheduler import load_routines
        routines = load_routines()
        registered_ids = set()
        for event in today_events:
            if event.get("kind") == "routine":
                sid = (event.get("payload") or {}).get("schedule_id", "")
                if sid:
                    registered_ids.add(sid)
        result = []
        for entry in routines:
            if not entry.get("enabled", True):
                continue
            time_str = entry.get("time", "00:00")
            parts = time_str.split(":")
            result.append({
                "hour": int(parts[0]),
                "minute": int(parts[1]),
                "schedule_id": entry.get("id", ""),
                "label": entry.get("name", ""),
                "registered": entry.get("id", "") in registered_ids,
            })
        return result

    def schedules(self) -> UseCaseResult:
        """List all routine schedules, 标注 matches_today."""
        from domain.lifecycle.routine_scheduler import load_routines, _matches_today
        from domain.lifecycle.clock import now_dt
        routines = load_routines()
        now = now_dt()
        for r in routines:
            r["matches_today"] = _matches_today(r, now)
        return UseCaseResult({"schedules": routines})

    def create_schedule(self, body: dict[str, Any]) -> UseCaseResult:
        """Create a new routine schedule."""
        from domain.lifecycle.routine_scheduler import load_routines, save_routines
        routines = load_routines()
        if not body.get("id"):
            return UseCaseResult({"error": "id is required"}, 400)
        if any(r.get("id") == body["id"] for r in routines):
            return UseCaseResult({"error": f"schedule {body['id']} already exists"}, 409)
        entry = {
            "id": body["id"],
            "name": body.get("name", ""),
            "time": body.get("time", "12:00"),
            "description": body.get("description", ""),
            "prompt_template": body.get("prompt_template", ""),
            "recurrence": body.get("recurrence", "daily"),
            "priority": body.get("priority", 4),
            "enabled": body.get("enabled", True),
        }
        if body.get("recurrence") == "once":
            entry["date"] = body.get("date", "")
        routines.append(entry)
        save_routines(routines)
        return UseCaseResult({"schedule": entry})

    def update_schedule(self, schedule_id: str, body: dict[str, Any]) -> UseCaseResult:
        """Update an existing routine schedule."""
        from domain.lifecycle.routine_scheduler import load_routines, save_routines
        routines = load_routines()
        for entry in routines:
            if entry.get("id") == schedule_id:
                for key in ("name", "time", "description", "prompt_template", "recurrence", "priority", "enabled", "date"):
                    if key in body:
                        entry[key] = body[key]
                save_routines(routines)
                return UseCaseResult({"schedule": entry})
        return UseCaseResult({"error": f"schedule {schedule_id} not found"}, 404)

    def delete_schedule(self, schedule_id: str) -> UseCaseResult:
        """Delete a routine schedule."""
        from domain.lifecycle.routine_scheduler import load_routines, save_routines
        routines = load_routines()
        new_routines = [r for r in routines if r.get("id") != schedule_id]
        if len(new_routines) == len(routines):
            return UseCaseResult({"error": f"schedule {schedule_id} not found"}, 404)
        save_routines(new_routines)
        return UseCaseResult({"deleted": schedule_id})

    def calendar(self, week_start: str) -> UseCaseResult:
        """Return a week's calendar: upcoming items by day + consumed events by session."""
        from domain.lifecycle.routine_scheduler import load_routines

        db = self._db()
        if not db:
            return UseCaseResult({"error": "state.db unavailable"}, 500)

        try:
            from domain.lifecycle import clock as _clock
            tz8 = _clock.BEIJING
            ws = datetime.fromisoformat(week_start).replace(tzinfo=tz8)
            we = ws + timedelta(days=7)
            # 字典序比较要求 ws_iso/we_iso 与存储格式（本地时间 ISO）一致
            ws_iso = _clock.to_storage_iso(ws)
            we_iso = _clock.to_storage_iso(we)

            # ── 拉 win 内所有 wake (来自 runtime_log.db) 用于 sid→wake_id 时间匹配 ──
            # session 跟 wake 没 id 关联,只靠 started_at(~0.1s 差)。
            # 给每个 session 解析出对应的 wake_id;前端 calendar 点击 session 跳转用 wake_id。
            # _wake_type_labels = session.id → 中文名映射,calendar 全局共用
            _wake_type_labels = {
                "initiative": "主动探索",
                "group_message": "群聊消息",
                "message": "私聊消息",
                "timer": "定时唤醒",
                "awaiting_reply": "等待回复",
                "routine": "作息节奏",
                "vital_threshold": "精力阈值",
                "l4_wake": "L4 唤醒",
                "self_iteration": "自我审查",
                "nurture_energy": "鸡腿投喂",
                "task_momentum": "任务惯性",
                "task_reminder": "任务提醒",
                "birth": "出生事件",
                "project_created": "项目新建",
            }
            session_wake_map: dict[str, int | None] = {}
            try:
                import sqlite3 as _sqlite3
                from infrastructure.config import get_project_root, get_app_instance_id
                _iid = get_app_instance_id() or ""
                if _iid:
                    runtime_db = get_project_root() / "apps" / _iid / "data" / "runtime_log.db"
                    if runtime_db.exists():
                        with _sqlite3.connect(str(runtime_db)) as rconn:
                            rconn.row_factory = _sqlite3.Row
                            wts = ws.timestamp()
                            wte = we.timestamp()
                            wake_rows = rconn.execute(
                                "SELECT id, started_at FROM wake WHERE started_at >= ? AND started_at <= ?",
                                (wts, wte),
                            ).fetchall()
                            # 时间索引,供 session 查 nearest
                            wake_times = [(row["id"], float(row["started_at"])) for row in wake_rows]
            except Exception:
                wake_times = []

            def _find_wake_id(target_ts: float, tolerance: float = 5.0) -> int | None:
                """找最接近 target_ts 的 wake id,tolerance 内匹配不到返 None。
                session 跟 wake 通常差 < 0.5s;放宽 5s 兜底边界 wake。"""
                if not wake_times or target_ts is None:
                    return None
                best_id, best_diff = None, tolerance
                for wid, wt in wake_times:
                    diff = abs(wt - target_ts)
                    if diff < best_diff:
                        best_diff = diff
                        best_id = wid
                return best_id

            # 查询 timers 表获取未触发的闹钟
            timer_rows = db.execute(
                "SELECT id, event_kind, fire_at, payload_json FROM timers "
                "WHERE fired_at IS NULL AND fire_at >= ? AND fire_at < ? "
                "ORDER BY fire_at ASC",
                (ws_iso, we_iso),
            ).fetchall()
            timers = [dict(row) for row in timer_rows]

            routines = load_routines()

            weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
            today = _clock.beijing_now_dt().date()
            now_dt = _clock.beijing_now_dt()

            # Active UNTIL wait_intents (timers set by agent via rest(until=...))
            wait_intents = []
            try:
                wi_rows = db.execute(
                    "SELECT affair_id, wait_type, resume_when, reason, resume_action "
                    "FROM wait_intents WHERE wait_type='until' AND resume_when IS NOT NULL "
                    "AND resume_when >= ? AND resume_when < ?",
                    (ws_iso, we_iso),
                ).fetchall()
                wait_intents = [dict(row) for row in wi_rows]
            except Exception:
                pass

            # --- Consumed sessions: 先查出来，按结束日期归入各天 ---
            ws_ts = ws.timestamp()
            we_ts = we.timestamp()

            session_rows = db.execute(
                """SELECT id, source, started_at, ended_at, end_reason, message_count, tool_call_count, title
                   FROM sessions
                   WHERE ended_at IS NOT NULL AND ended_at >= ? AND ended_at < ?
                   ORDER BY ended_at ASC""",
                (ws_ts, we_ts),
            ).fetchall()

            sessions_by_day: dict[str, list[dict[str, Any]]] = {}
            consumed_sessions: list[dict[str, Any]] = []
            _source_labels = {"l4_wake": "唤醒", "initiative": "探索", "timer": "定时", "message": "回复", "group_message": "群聊"}
            for row in session_rows:
                sid_val = row[0]
                ended_ts_val = row[3]
                started_ts_val = row[2]
                try:
                    ended_dt = datetime.fromtimestamp(ended_ts_val, tz=tz8)
                except Exception:
                    continue
                day_key = ended_dt.date().isoformat()
                source_label = _source_labels.get(row[1], row[1])
                # 解析 session 对应的 wake_id(session.started_at 最近匹配)
                wake_id = _find_wake_id(started_ts_val) if started_ts_val else None
                # 解析 session 中文名: title 优先,否则 session.id 反解 wake type 中文
                import re as _re2
                _sid_str = str(sid_val or "")
                _m2 = _re2.match(r"tx_([a-z_]+?)_\d{4}_\d{4}_[a-f0-9]+$", _sid_str)
                if _m2:
                    _default_name = _wake_type_labels.get(_m2.group(1), _m2.group(1))
                else:
                    _src_short = _source_labels.get(row[1], str(row[1])[:4])
                    _default_name = _src_short
                session_item = {
                    "session_id": sid_val,
                    "wake_id": wake_id,
                    "name": (row[7] if len(row) > 7 else "") or _default_name,
                    "source": row[1],
                    "started_at": row[2],
                    "ended_at": ended_ts_val,
                    "ended_at_display": ended_dt.strftime("%H:%M"),
                    "ended_at_iso": ended_dt.isoformat(timespec="seconds"),
                    "end_reason": row[4],
                    "message_count": row[5],
                    "tool_call_count": row[6] or 0,
                    "title": row[7] or "",
                }
                sessions_by_day.setdefault(day_key, []).append(session_item)
                consumed_sessions.append(session_item)
            consumed_sessions.reverse()  # 最新在前

            # --- Grid: unconsumed items + consumed sessions for each day ---
            days = []
            for i in range(7):
                day = ws.date() + timedelta(days=i)
                day_str = day.isoformat()
                is_today = day == today
                is_past_day = day < today
                items = []

                registered_sids = set()  # schedule_ids already in DB for this day

                # 从 timers 表获取当天未触发的闹钟
                for timer in timers:
                    fire_at = timer.get("fire_at")
                    if not fire_at:
                        continue
                    try:
                        fire_dt = datetime.fromisoformat(fire_at)
                    except Exception:
                        continue
                    if fire_dt.date() != day:
                        continue

                    kind = timer.get("event_kind", "")
                    try:
                        payload = json.loads(timer.get("payload_json", "{}"))
                    except Exception:
                        payload = {}

                    scid = payload.get("schedule_id") if isinstance(payload, dict) else None
                    if scid:
                        registered_sids.add(scid)

                    # 过去的天：跳过（只显示 virtual routines）
                    if is_past_day:
                        continue

                    # 今天：跳过已过时的
                    if is_today and fire_dt <= now_dt:
                        continue

                    items.append({
                        "id": f"timer:{timer['id']}",
                        "kind": kind,
                        "name": self._timer_display_name(timer),
                        "time": fire_dt.strftime("%H:%M"),
                        "fire_at": fire_at,
                        "consumed": False,
                        "event_id": None,
                        "consumed_by_session_id": None,
                        "payload": payload,
                        "schedule_id": scid,
                        "source": "timer",
                    })

                # Virtual routines (not yet in DB)
                # Also dedup against timers by (time, kind) since payload may
                # not carry schedule_id for older events.
                db_time_kinds = {(it["time"], it["kind"]) for it in items if it.get("source") == "timer"}
                if not is_past_day:
                    for r in routines:
                        if not r.get("enabled", True):
                            continue
                        sid = r.get("id", "")
                        recurrence = r.get("recurrence", "daily")

                        if recurrence == "once":
                            if r.get("date", "") != day_str:
                                continue
                        else:
                            if not self._routine_matches_day(r, day):
                                continue

                        if sid in registered_sids:
                            continue

                        rkind = "alarm" if recurrence == "once" else "routine"
                        if (r["time"], rkind) in db_time_kinds:
                            continue

                        h_str, m_str = r["time"].split(":")
                        fire_dt = datetime(day.year, day.month, day.day, int(h_str), int(m_str), 0, tzinfo=tz8)

                        items.append({
                            "id": f"{rkind}:{sid}",
                            "kind": rkind,
                            "name": r.get("name", ""),
                            "time": r["time"],
                            "fire_at": fire_dt.isoformat(timespec="seconds"),
                            "event_id": None,
                            "consumed_by_session_id": None,
                            "payload": r,
                            "schedule_id": sid,
                            "recurrence": recurrence,
                            "source": rkind,
                        })

                # Active timer wait_intents (UNTHIL alarms set by agent via rest())
                for wi in wait_intents:
                    resume = wi.get("resume_when")
                    if not resume:
                        continue
                    try:
                        resume_dt = datetime.fromisoformat(resume)
                    except Exception:
                        continue
                    if resume_dt.date() != day:
                        continue
                    reason = wi.get("reason", "") or ""
                    items.append({
                        "id": f"timer:{wi.get('affair_id', '')}",
                        "kind": "timer",
                        "name": "⏰ " + (reason[:30] + "…" if len(reason) > 30 else reason),
                        "time": resume_dt.strftime("%H:%M"),
                        "fire_at": resume,
                        "event_id": None,
                        "consumed_by_session_id": None,
                        "consumed": False,
                        "payload": {"reason": reason, "resume_when": resume},
                        "source": "timer",
                    })

                items.sort(key=lambda x: x.get("fire_at", ""))

                # 把当天的已完成 session 追加到 items
                # session.name + session.wake_id 已在构造 session_item 时算好,直接用
                for sess in sessions_by_day.pop(day_str, []):
                    items.append({
                        "id": f"session:{sess['session_id']}",
                        "kind": "session",
                        "name": sess.get("name") or sess["session_id"],
                        "time": sess["ended_at_display"],
                        "fire_at": sess.get("ended_at_iso", ""),
                        "consumed": True,
                        "consumed_by_session_id": sess["session_id"],
                        "wake_id": sess.get("wake_id"),  # 跳 sessions 页用 wake_id(不是 sid)
                        "payload": sess,
                        "source": "session",
                    })

                days.append({
                    "date": day_str,
                    "weekday": weekday_names[i],
                    "is_today": is_today,
                    "items": items,
                })

            # --- Consumed sessions: 先查出来，按结束日期归入各天 ---
            ws_ts = ws.timestamp()
            we_ts = we.timestamp()

            session_rows = db.execute(
                """SELECT id, source, started_at, ended_at, end_reason, message_count, tool_call_count, title
                   FROM sessions
                   WHERE ended_at IS NOT NULL AND ended_at >= ? AND ended_at < ?
                   ORDER BY ended_at DESC""",
                (ws_ts, we_ts),
            ).fetchall()

            sessions_by_day: dict[str, list] = {}
            for row in session_rows:
                ended_ts_val = row[3]
                try:
                    ended_dt = datetime.fromtimestamp(ended_ts_val, tz=tz8)
                except Exception:
                    continue
                day_key = ended_dt.date().isoformat()
                sessions_by_day.setdefault(day_key, []).append({
                    "session_id": row[0],
                    "source": row[1],
                    "started_at": row[2],
                    "ended_at": ended_ts_val,
                    "ended_at_display": ended_dt.strftime("%H:%M"),
                    "end_reason": row[4],
                    "message_count": row[5],
                    "tool_call_count": row[6] or 0,
                    "title": row[7] or "",
                })

            return UseCaseResult({
                "week_start": ws_iso,
                "week_end": we_iso,
                "days": days,
                "consumed_sessions": consumed_sessions,
            })
        except Exception as exc:
            return UseCaseResult({"error": str(exc)}, 500)
        finally:
            db.close()

    @staticmethod
    def _routine_matches_day(routine: dict, day: datetime.date) -> bool:
        recurrence = routine.get("recurrence", "daily")
        if recurrence == "daily":
            return True
        wd = day.weekday()
        if recurrence == "weekdays":
            return wd < 5
        if recurrence == "weekends":
            return wd >= 5
        days_list = [d.strip().lower()[:3] for d in recurrence.split(",")]
        day_names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        return day_names[wd] in days_list

    @staticmethod
    def _event_display_name(event: dict) -> str:
        kind = event.get("kind", "")
        payload = event.get("payload", {}) or {}
        if not isinstance(payload, dict):
            payload = {}
        if kind == "routine":
            return payload.get("name", "") or payload.get("description", "") or "作息"
        if kind == "vital_threshold":
            dim = payload.get("dimension", "")
            to_seg = payload.get("to_seg", "")
            return f"{dim}→{to_seg}" if dim and to_seg else "阈值预警"
        if kind == "task_reminder" or kind == "task_momentum":
            return payload.get("task_title", "") or payload.get("content", "") or kind
        if kind == "timer":
            return payload.get("reason", "") or "定时唤醒"
        if kind == "initiative":
            return f"空闲 {payload.get('elapsed_hours', '?')}h 探索"
        if kind == "nurture_energy":
            return f"{payload.get('action_label', '加鸡腿')} +{payload.get('energy_added', 0)}"
        if kind == "message":
            text = payload.get("text", "") or ""
            return text[:30] if text else "消息"
        if kind == "awaiting_reply":
            return "等待回复"
        return payload.get("text", "") or payload.get("description", "") or kind

    @staticmethod
    def _timer_display_name(timer: dict) -> str:
        """生成闹钟的显示名称。"""
        kind = timer.get("event_kind", "")
        try:
            payload = json.loads(timer.get("payload_json", "{}"))
        except Exception:
            payload = {}

        if kind == "timer":
            return payload.get("reason", "") or "定时唤醒"
        if kind == "routine":
            return payload.get("name", "") or "作息"
        if kind == "task_reminder":
            return payload.get("task_title", "") or payload.get("content", "") or kind
        if kind == "vital_threshold":
            dim = payload.get("dimension", "")
            to_seg = payload.get("to_seg", "")
            return f"{dim}→{to_seg}" if dim and to_seg else "阈值预警"
        if kind == "nurture_energy":
            return f"{payload.get('action_label', '加鸡腿')} +{payload.get('energy_added', 0)}"
        if kind == "initiative":
            return f"空闲 {payload.get('elapsed_hours', '?')}h 探索"
        return payload.get("reason", "") or payload.get("text", "") or kind

    def config(self) -> UseCaseResult:
        try:
            from domain.vital.simulation.engine import (
                ENERGY_MAX, ENERGY_RECOVERY_PER_HOUR, ENERGY_COST_PER_CALL,
                INITIATIVE_ENERGY_THRESHOLD, INITIATIVE_IDLE_HOURS,
            )
            cfg = {
                "energy": {
                    "max": ENERGY_MAX,
                    "recovery_per_hour": ENERGY_RECOVERY_PER_HOUR,
                    "cost_per_call": ENERGY_COST_PER_CALL,
                },
                "defaults": {"energy": ENERGY_MAX * 0.7},
                "initiative": {
                    "energy_threshold": INITIATIVE_ENERGY_THRESHOLD,
                    "idle_hours": INITIATIVE_IDLE_HOURS,
                },
            }
            return UseCaseResult({"config": cfg})
        except Exception as exc:
            return UseCaseResult({"error": str(exc)}, 500)

    def update_config(self, body: dict[str, Any]) -> UseCaseResult:
        """配置已简化为硬编码常量，不再支持运行时修改。"""
        return UseCaseResult({"ok": False, "error": "runtime config modification not supported in simplified mode"})

    def associations(self) -> UseCaseResult:
        try:
            db = self._vector_db()
            if db is None:
                return UseCaseResult({"chunks": 0, "associations": 0, "top_links": [], "sources": []})
            try:
                chunk_count = db.execute("SELECT COUNT(*) as c FROM chunks").fetchone()["c"]
                assoc_count = db.execute("SELECT COUNT(*) as c FROM associations").fetchone()["c"]
                top = db.execute(
                    "SELECT a.chunk_a, a.chunk_b, "
                    "a.chunk_a as source_chunk_id, a.chunk_b as target_chunk_id, "
                    "a.weight, "
                    "ca.source as source_source, cb.source as target_source, "
                    "substr(ca.text, 1, 120) as source_text, substr(cb.text, 1, 120) as target_text "
                    "FROM associations a "
                    "JOIN chunks ca ON a.chunk_a = ca.id "
                    "JOIN chunks cb ON a.chunk_b = cb.id "
                    "ORDER BY a.weight DESC LIMIT 20"
                ).fetchall()
                sources = db.execute("SELECT source, COUNT(*) as c FROM chunks GROUP BY source ORDER BY c DESC").fetchall()
                return UseCaseResult({
                    "chunks": chunk_count,
                    "associations": assoc_count,
                    "top_links": [dict(row) for row in top],
                    "sources": [{"source": row["source"], "count": row["c"]} for row in sources],
                })
            finally:
                db.close()
        except Exception as exc:
            return UseCaseResult({"error": str(exc)}, 500)

    def chunks(self, query: str = "", source: str = "", limit: int = 20, offset: int = 0) -> UseCaseResult:
        try:
            db = self._vector_db()
            if db is None:
                return UseCaseResult({"chunks": [], "total": 0})
            try:
                where_parts = []
                params: list[Any] = []
                if query:
                    where_parts.append("text LIKE ?")
                    params.append(f"%{query}%")
                if source:
                    where_parts.append("source = ?")
                    params.append(source)
                where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
                capped_limit = min(max(int(limit), 1), 100)
                safe_offset = max(int(offset), 0)
                total = db.execute(f"SELECT COUNT(*) as c FROM chunks{where_sql}", params).fetchone()["c"]
                # Feature 002 (T028+D): 列表带 phase / source_kind / authority /
                # freshness / cognition_state / supersede_by, 让前端能展示切片层属性
                # (chunk_detail 已含全 P3 schema, 这里只是 list 加 5 个关键列)
                rows = db.execute(
                    f"""SELECT id, source, substr(text, 1, 200) as text, created_at,
                              phase, source_kind, authority, freshness, cognition_state
                        FROM chunks{where_sql}
                        ORDER BY created_at DESC LIMIT ? OFFSET ?""",
                    params + [capped_limit, safe_offset],
                ).fetchall()
                return UseCaseResult({"chunks": [dict(row) for row in rows], "total": total})
            finally:
                db.close()
        except Exception as exc:
            return UseCaseResult({"error": str(exc)}, 500)

    def chunk_detail(self, chunk_id: int) -> UseCaseResult:
        try:
            db = self._vector_db()
            if db is None:
                return UseCaseResult({"error": "not found"}, 404)
            try:
                # P3 T041: SELECT 加新切片字段(phase/source_kind/authority/permanence/
                # freshness/cognition_state/session_id/segment_index/derived_from)
                row = db.execute(
                    """SELECT id, source, text, created_at, file_mtime,
                              phase, source_kind, authority, permanence, freshness,
                              activation, verification, evidence_count, challenge_count,
                              cognition_state, supersede_by,
                              session_id, segment_index, derived_from, derive_kind,
                              entity_links, attention_tokens, provenance
                       FROM chunks WHERE id=?""",
                    (chunk_id,),
                ).fetchone()
                if not row:
                    return UseCaseResult({"error": "not found"}, 404)
                chunk = dict(row)
                linked = db.execute(
                    "SELECT c.id, c.source, substr(c.text, 1, 150) as text, a.weight "
                    "FROM associations a "
                    "JOIN chunks c ON (CASE WHEN a.chunk_a=? THEN c.id=a.chunk_b ELSE c.id=a.chunk_a END) "
                    "WHERE a.chunk_a=? OR a.chunk_b=? "
                    "ORDER BY a.weight DESC LIMIT 20",
                    (chunk_id, chunk_id, chunk_id),
                ).fetchall()
                chunk["linked"] = [dict(row) for row in linked]
                return UseCaseResult(chunk)
            finally:
                db.close()
        except Exception as exc:
            return UseCaseResult({"error": str(exc)}, 500)

    @staticmethod
    def _vector_db() -> sqlite3.Connection | None:
        path = get_runtime_memories_dir() / "memory_vectors.db"
        if not path.exists():
            return None
        db = sqlite3.connect(str(path))
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only = ON")
        return db

    def nurture(self, action: str) -> UseCaseResult:
        try:
            console_runtime_adapter().apply_nurture(kind=f"monitor:{action}", deltas={"energy": 0}, source="monitor")
            return UseCaseResult({"ok": True, "action": action})
        except Exception as exc:
            return UseCaseResult({"error": str(exc)}, 500)

    def deltas(self, body: dict[str, Any]) -> UseCaseResult:
        try:
            from domain.vital.simulation import get_engine

            valid_dims = {"energy"}
            deltas = {key: value for key, value in body.items() if key in valid_dims and isinstance(value, (int, float))}
            if not deltas:
                return UseCaseResult({"error": "no valid deltas"}, 400)
            engine = get_engine()
            engine.apply_deltas(deltas)
            console_runtime_adapter().apply_nurture(kind="monitor:deltas", deltas=deltas, source="monitor")
            return UseCaseResult({"ok": True, "deltas": deltas, "vitals": engine.get_state()["vitals"]})
        except Exception as exc:
            return UseCaseResult({"error": str(exc)}, 500)

    def nurture_energy(self, body: dict[str, Any]) -> UseCaseResult:
        """前端 "加鸡腿" 按钮触发：恢复精力 + 发射事件 + 唤醒。"""
        try:
            from domain.vital import apply_nurture
            from domain.vital.simulation import get_engine, reset_engine
            from domain.lifecycle.events import emit_event

            amount = float(body.get("amount", 30))
            label = str(body.get("label", "加了鸡腿"))

            if amount <= 0:
                return UseCaseResult({"error": "amount must be positive"}, 400)

            # 通过 state.py 统一写入（持久化 + nurture_log + 更新 updated_at）
            new_snapshot = apply_nurture(
                kind=f"monitor:nurture_energy",
                deltas={"energy": amount},
                raw_text=label,
                source="monitor",
            )
            current_energy = round(new_snapshot.energy)

            # 重载 engine 缓存，使 engine 读到最新的 vitals
            reset_engine()

            # 发射 nurture_energy 事件（让 Agent 知道谁加了鸡腿）。
            # emit_event 内部会自动尝试叫醒(BLOCKED→wake / RUNNING→mid-session inject),
            # 不再需要 _try_wake_for_nurture 手动决策(refactor/emit-driven-wake)。
            event_id = emit_event("nurture_energy", {
                "energy_added": int(amount),
                "current_energy": current_energy,
                "action_label": label,
            })

            return UseCaseResult({"ok": True, "energy": current_energy, "added": int(amount)})
        except Exception as exc:
            import traceback, logging
            _logger = logging.getLogger("monitor.nurture_energy")
            _logger.exception("nurture_energy failed: %s", exc)
            return UseCaseResult({"error": str(exc), "trace": traceback.format_exc()[-500:]}, 500)

    def wallet(self) -> UseCaseResult:
        return UseCaseResult({"balance": 0, "positions": {}, "note": "wallet removed"})

    def nurture_log(self, hours: int = 24) -> UseCaseResult:
        try:
            return UseCaseResult({"logs": console_runtime_adapter().recent_nurture_log(hours=min(int(hours), 168))})
        except Exception as exc:
            return UseCaseResult({"error": str(exc)}, 500)

    def collect_metrics(self) -> dict[str, Any]:
        """收集所有监控指标供Prometheus导出"""
        try:
            from application.console.metrics import MetricsCollector
            from pathlib import Path
            import sqlite3

            status = self.status()
            if status.status_code == 200:
                MetricsCollector.collect_from_status(status.payload)

            state_db_path = get_runtime_state_db_path()
            memory_vectors_path = get_runtime_memories_dir() / "memory_vectors.db"

            state_db_size = state_db_path.stat().st_size if state_db_path.exists() else 0
            memory_vectors_size = memory_vectors_path.stat().st_size if memory_vectors_path.exists() else 0
            MetricsCollector.collect_storage_metrics(state_db_size, memory_vectors_size)

            db = self._vector_db()
            if db:
                try:
                    count = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
                    MetricsCollector.collect_from_memory_stats({'vector_count': count})
                except Exception:
                    pass
                finally:
                    db.close()

            db = self._db()
            if db:
                try:
                    pending = db.execute("SELECT COUNT(*) FROM events WHERE consumed_at IS NULL").fetchone()[0]
                    recent_sessions = db.execute(
                        "SELECT COUNT(*) FROM sessions WHERE created_at > datetime('now', '-1 day')"
                    ).fetchone()[0]
                    MetricsCollector.collect_event_metrics(pending, recent_sessions)
                except Exception:
                    pass
                finally:
                    db.close()

            return {'status': 'ok'}
        except Exception as e:
            return {'status': 'error', 'message': str(e)}

    def health_check(self) -> dict[str, Any]:
        """简化的健康检查"""
        try:
            from domain.execution.semantics import get_vitals_engine

            state = get_vitals_engine().get_state()
            vitals = state.get('vitals', {})
            energy = vitals.get('energy', 0) if isinstance(vitals, dict) else 0

            checks = {
                'energy': energy,
                'healthy': energy >= 0,
            }
            return {'status': 'ok', 'checks': checks}
        except Exception as e:
            return {'status': 'error', 'message': str(e)}




    def check_alerts(self) -> dict[str, Any]:
        """检查告警规则"""
        try:
            from application.console.alerts import AlertManager
            
            # 获取当前状态
            status_result = self.status()
            if status_result.status_code != 200:
                return {'status': 'error', 'message': 'Failed to get status'}
            
            # 收集指标数据
            status_data = status_result.payload
            
            # 补充存储大小
            state_db_path = get_runtime_state_db_path()
            status_data['state_db_size'] = state_db_path.stat().st_size if state_db_path.exists() else 0
            
            # 补充待处理事件数
            db = self._db()
            if db:
                try:
                    pending = db.execute("SELECT COUNT(*) FROM events WHERE consumed_at IS NULL").fetchone()[0]
                    status_data['pending_events_count'] = pending
                except Exception:
                    status_data['pending_events_count'] = 0
                finally:
                    db.close()
            
            # 检查告警
            alert_mgr = AlertManager()
            alerts = alert_mgr.check_alerts(status_data)
            
            return {
                'status': 'ok',
                'alerts': alerts,
                'total': len(alerts)
            }
        except Exception as e:
            return {'status': 'error', 'message': str(e)}
    
    def get_alert_history(self, hours: int = 24) -> dict[str, Any]:
        """检查告警历史"""
        try:
            from application.console.alerts import AlertManager
            
            alert_mgr = AlertManager()
            alerts = alert_mgr.get_recent_alerts(hours)
            
            return {
                'status': 'ok',
                'alerts': alerts,
                'total': len(alerts)
            }
        except Exception as e:
            return {'status': 'error', 'message': str(e)}


__all__ = ["MonitorConsoleWorkflow"]
