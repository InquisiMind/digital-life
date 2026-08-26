"""Employee console HTTP API routes."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from aiohttp import web

from application.console import (
    EmployeeConsoleHttpIngress,
    EmployeeConsoleHttpInput,
    ConfigCenterWorkflow,
    EventLogConsoleWorkflow,
    MonitorConsoleWorkflow,
    PromptConsoleWorkflow,
    SessionConsoleWorkflow,
    TaskConsoleWorkflow,
)
from application.contracts import UseCaseResult

ROOT_DIR = Path(__file__).resolve().parents[2]


def _route_prefix(name: str, default: str) -> str:
    value = (os.getenv(name) or default).strip() or default
    return "/" + value.strip("/")


def _web_root() -> Path:
    configured = (os.getenv("DIGITAL_LIFE_EMPLOYEE_CONSOLE_WEB_ROOT") or "").strip()
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_absolute() else ROOT_DIR / path
    return ROOT_DIR / "interfaces" / "web" / "employee-console" / "dist"


def _default_employee_id() -> str:
    return (
        os.getenv("DIGITAL_LIFE_INSTANCE_ID")
        or os.getenv("L4_AGENT_ID")
        or os.getenv("DIGITAL_LIFE_EMPLOYEE_ID")
        or "default"
    ).strip("/") or "default"


class EmployeeConsoleAPIService:
    """HTTP adapter for the current Employee console/debug endpoints."""

    def __init__(self, adapter: Any) -> None:
        self.adapter = adapter
        self.api_prefix = "/api/employee"
        self.ingress = EmployeeConsoleHttpIngress()
        self.monitor = MonitorConsoleWorkflow()
        self.config = ConfigCenterWorkflow()
        self.sessions = SessionConsoleWorkflow()
        self.event_logs = EventLogConsoleWorkflow()
        self.prompts = PromptConsoleWorkflow()
        self.tasks = TaskConsoleWorkflow()

    async def _input(self, request: web.Request) -> EmployeeConsoleHttpInput:
        return await self.ingress.normalize(request)

    @staticmethod
    def _response(result: UseCaseResult) -> web.Response:
        return web.json_response(dict(result.payload), status=result.status_code)

    @staticmethod
    def _int(value: str | None, default: int) -> int:
        try:
            return int(value) if value is not None else default
        except (TypeError, ValueError):
            return default

    async def _handle_console_status(self, request: web.Request) -> web.Response:
        return self._response(self.monitor.status())

    async def _handle_console_budget(self, request: web.Request) -> web.Response:
        """读 token 预算 + 精力状态（前端.StatusTab 预算面板用）。

        每实例独立，request 经 _employee_middleware 已经把 ContextVar 染到
        对应实例。返回结构参 docs/design 二十三章。
        """
        try:
            from infrastructure.config import get_app_instance_id
            from infrastructure.budget import get_budget_state
            from domain.vital import get_current_vitals

            iid = get_app_instance_id() or ""
            state = get_budget_state(iid)
            try:
                vitals = get_current_vitals()
                energy = vitals.energy
                energy_segment = ""
                # 找段名
                try:
                    from domain.vital.simulation.engine import ENERGY_SEGMENTS
                    for name, lo, hi, _exp in ENERGY_SEGMENTS:
                        if lo <= energy <= hi:
                            energy_segment = name
                            break
                except Exception:
                    pass
            except Exception:
                energy, energy_segment = None, ""

            return web.json_response({
                "hour": {
                    "used": state.hour_used,
                    "limit": state.hour_limit,
                    "resets_at": state.hour_resets_at,
                },
                "day": {
                    "used": state.day_used,
                    "limit": state.day_limit,
                    "resets_at": state.day_resets_at,
                },
                "energy": {"current": energy, "segment": energy_segment},
                "is_throttled": state.is_throttled,
            })
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

    async def _handle_console_budget_series(self, request: web.Request) -> web.Response:
        """Token 用量时序明细（按时间桶 + kind 拆分），给前端图表。

        返回 { buckets: [{at_iso, input, output, total, input_summary,
        output_summary, total_summary, count_429}],
        day_total_used, hour_used }
        """
        try:
            from infrastructure.config import get_app_instance_id
            from infrastructure.budget import get_token_tracker
            data = await self._input(request)
            hours = self._int(data.query.get("hours"), 24)
            bucket = (data.query.get("bucket") or "hour").strip()
            iid = get_app_instance_id() or ""
            tracker = get_token_tracker()
            series = tracker.usage_series(hours=hours, bucket=bucket, instance_id=iid)
            day_used = tracker.usage_today(iid)
            hour_used = tracker.usage_last_hour(iid)
            return web.json_response({
                "buckets": series,
                "day_total_used": day_used,
                "hour_used": hour_used,
                "hours": hours,
                "bucket": bucket,
            })
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

    async def _handle_console_vitals_series(self, request: web.Request) -> web.Response:
        """精力采样序列（连续恢复曲线）+ nurture_log 事件点，给前端图表。

        返回 { samples: [{at_unix, energy, affair_state}],
               events: [recent_nurture_log entries] }
        """
        try:
            from domain.vital.state import vital_history_series, recent_nurture_log
            data = await self._input(request)
            hours = self._int(data.query.get("hours"), 24)
            samples = vital_history_series(hours=hours)
            events = recent_nurture_log(hours=hours)
            return web.json_response({
                "samples": samples,
                "events": events,
                "hours": hours,
            })
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

    async def _handle_console_social_status(self, request: web.Request) -> web.Response:
        """GET /api/employee/{iid}/social/status

        返回当前实例的社交接管授权状态 + 授权 URL。
        前端 ConfigTab 用: 已授权显示"已接管"+解除按钮; 未授权显示"接管飞书"链接。
        """
        try:
            from infrastructure.config import get_app_instance_id, get_instance_dir
            iid = get_app_instance_id() or ""
            instance_dir = get_instance_dir(iid)
            social_env = instance_dir / "config" / "social.env"
            authorized = False
            if social_env.exists():
                for line in social_env.read_text(encoding="utf-8").splitlines():
                    if line.startswith("FEISHU_USER_REFRESH_TOKEN="):
                        val = line.split("=", 1)[1].strip()
                        if val:
                            authorized = True
                            break
            # 授权 URL (即使已授权也返回, 让用户可重新授权刷新)
            oauth_url = ""
            if iid:
                try:
                    from application.api.oauth_routes import get_oauth_url
                    oauth_url = get_oauth_url(iid)
                except Exception:
                    pass
            return web.json_response({
                "authorized": authorized,
                "oauth_url": oauth_url,
                "instance_id": iid,
            })
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

    async def _handle_console_social_revoke(self, request: web.Request) -> web.Response:
        """POST /api/employee/{iid}/social/revoke

        解除授权: 删 social.env + 停 personal_assistant 项目 + 清待办。
        全解耦: 接管是因(OAuth→建项目), 解除是果(删 token→停项目)。
        """
        try:
            from infrastructure.config import get_app_instance_id, get_instance_dir
            iid = get_app_instance_id() or ""
            instance_dir = get_instance_dir(iid)
            social_env = instance_dir / "config" / "social.env"
            if social_env.exists():
                social_env.unlink()

            # 接管取消: 停项目 + 清待办
            try:
                from domain.lifecycle.schema import deactivate_personal_assistant
                deactivate_personal_assistant(iid)
            except Exception as exc:
                logger.warning("deactivate_personal_assistant failed: %s", exc)

            return web.json_response({"ok": True, "note": "授权已解除, 项目已停, 待办已清理"})
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

    async def _handle_console_sessions(self, request: web.Request) -> web.Response:
        data = await self._input(request)
        return self._response(self.sessions.list_sessions(self._int(data.query.get("limit"), 20)))

    async def _handle_console_session_detail(self, request: web.Request) -> web.Response:
        data = await self._input(request)
        employee_id = request.match_info.get("employee_id", "") or ""
        self.sessions.employee_id = employee_id
        return self._response(self.sessions.session_detail(data.path_params["session_id"]))

    async def _handle_console_session_raw(self, request: web.Request) -> web.Response:
        session_id = request.match_info["session_id"]
        safe = session_id.replace("/", "").replace("\\", "")
        if safe != session_id:
            return web.json_response({"error": "invalid session_id"}, status=400)
        json_path = ROOT_DIR / "apps" / request.match_info.get("employee_id", "") / "data" / "sessions" / f"{safe}.json"
        if not json_path.is_file():
            return web.json_response({"error": "session JSON not found"}, status=404)
        return web.FileResponse(json_path)

    async def _handle_console_session_full(self, request: web.Request) -> web.Response:
        session_id = request.match_info["session_id"]
        safe = session_id.replace("/", "").replace("\\", "")
        if safe != session_id:
            return web.json_response({"error": "invalid session_id"}, status=400)
        json_path = ROOT_DIR / "apps" / request.match_info.get("employee_id", "") / "data" / "sessions" / f"{safe}.json"
        if not json_path.is_file():
            # JSON not written yet (session was killed mid-run) — fall back to DB
            result = self.sessions.session_detail(safe)
            result.payload["fallback_db"] = True
            return self._response(result)
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            return web.json_response({"error": "failed to parse session JSON"}, status=500)
        raw_messages = data.get("messages") or []
        normalized: list[dict[str, Any]] = []
        base_ts = json_path.stat().st_mtime
        for i, m in enumerate(raw_messages):
            entry: dict[str, Any] = {"role": m.get("role", ""), "content": m.get("content")}
            entry["ts"] = base_ts + i * 0.01
            if m.get("tool_call_id"):
                entry["tool_call_id"] = m["tool_call_id"]
            if m.get("name"):
                entry["tool_name"] = m["name"]
            if m.get("tool_calls"):
                entry["tool_calls"] = m["tool_calls"]
            normalized.append(entry)
        return web.json_response({
            "session_id": session_id,
            "messages": normalized,
        })

    async def _handle_console_run_event_log(self, request: web.Request) -> web.Response:
        data = await self._input(request)
        return self._response(
            self.event_logs.run_event_log(
                data.path_params["run_id"],
                employee_id=data.path_params.get("employee_id"),
            )
        )

    async def _handle_console_memory_dates(self, request: web.Request) -> web.Response:
        data = await self._input(request)
        return self._response(self.monitor.memory_dates(data.path_params["name"]))

    async def _handle_console_memories(self, request: web.Request) -> web.Response:
        data = await self._input(request)
        return self._response(self.monitor.memory_content(data.path_params["name"], data.query.get("date")))

    async def _handle_console_events(self, request: web.Request) -> web.Response:
        return self._response(self.monitor.events())

    async def _handle_console_event_queue(self, request: web.Request) -> web.Response:
        return self._response(self.monitor.event_queue())

    async def _handle_console_associations(self, request: web.Request) -> web.Response:
        return self._response(self.monitor.associations())

    async def _handle_console_chunks(self, request: web.Request) -> web.Response:
        data = await self._input(request)
        return self._response(
            self.monitor.chunks(
                query=(data.query.get("q") or "").strip(),
                source=(data.query.get("source") or "").strip(),
                limit=self._int(data.query.get("limit"), 20),
                offset=self._int(data.query.get("offset"), 0),
            )
        )

    async def _handle_console_chunk_detail(self, request: web.Request) -> web.Response:
        data = await self._input(request)
        return self._response(self.monitor.chunk_detail(self._int(data.path_params.get("id"), -1)))

    async def _handle_console_config(self, request: web.Request) -> web.Response:
        data = await self._input(request)
        return self._response(self.config.config(data.path_params.get("employee_id")))

    async def _handle_console_update_config(self, request: web.Request) -> web.Response:
        data = await self._input(request)
        return self._response(self.config.update_config(dict(data.body), data.path_params.get("employee_id")))

    async def _handle_console_prompts(self, request: web.Request) -> web.Response:
        data = await self._input(request)
        return self._response(self.prompts.prompts(data.path_params.get("employee_id")))

    async def _handle_console_event_types(self, request: web.Request) -> web.Response:
        data = await self._input(request)
        return self._response(self.prompts.events(data.path_params.get("employee_id")))

    async def _handle_console_update_prompt(self, request: web.Request) -> web.Response:
        data = await self._input(request)
        return self._response(
            self.prompts.update_prompt(
                data.path_params.get("name", ""),
                str(data.body.get("content", "")),
                data.path_params.get("employee_id"),
            )
        )

    async def _handle_console_tasks(self, request: web.Request) -> web.Response:
        """GET /tasks?status=&source=&project_id=&linked_deliverable_id=

        多维过滤（任一可选）：
            status: planned/in_progress/done/cancelled/paused/idea
            source: 'personal' 或 'project:{pid}'
            project_id: 自动展开为 source 维度
            linked_deliverable_id: 关联 deliverable 的 id
        """
        data = await self._input(request)
        return self._response(
            self.tasks.list_tasks(
                status=data.query.get("status"),
                source=data.query.get("source"),
                project_id=data.query.get("project_id"),
                linked_deliverable_id=data.query.get("linked_deliverable_id"),
            )
        )

    async def _handle_console_task_detail(self, request: web.Request) -> web.Response:
        data = await self._input(request)
        return self._response(self.tasks.task_detail(data.path_params["id"]))

    async def _handle_console_create_task(self, request: web.Request) -> web.Response:
        data = await self._input(request)
        return self._response(self.tasks.create_task(dict(data.body)))

    async def _handle_console_update_task(self, request: web.Request) -> web.Response:
        data = await self._input(request)
        return self._response(self.tasks.update_task(data.path_params["id"], dict(data.body)))

    async def _handle_console_task_plans(self, request: web.Request) -> web.Response:
        data = await self._input(request)
        return self._response(self.tasks.task_plans(data.path_params["id"]))

    async def _handle_console_create_plan(self, request: web.Request) -> web.Response:
        data = await self._input(request)
        return self._response(self.tasks.create_plan(data.path_params["id"], dict(data.body)))

    async def _handle_console_update_plan(self, request: web.Request) -> web.Response:
        data = await self._input(request)
        return self._response(self.tasks.update_plan(self._int(data.path_params.get("pid"), -1), dict(data.body)))

    async def _handle_console_task_notes(self, request: web.Request) -> web.Response:
        data = await self._input(request)
        return self._response(self.tasks.task_notes(data.path_params["id"]))

    async def _handle_console_nurture(self, request: web.Request) -> web.Response:
        data = await self._input(request)
        return self._response(self.monitor.nurture(str(data.body.get("action", ""))))

    async def _handle_console_nurture_energy(self, request: web.Request) -> web.Response:
        data = await self._input(request)
        return self._response(self.monitor.nurture_energy(dict(data.body)))

    async def _handle_console_deltas(self, request: web.Request) -> web.Response:
        data = await self._input(request)
        return self._response(self.monitor.deltas(dict(data.body)))

    async def _handle_console_predictions(self, request: web.Request) -> web.Response:
        return self._response(self.monitor.predictions())

    async def _handle_console_schedules(self, request: web.Request) -> web.Response:
        return self._response(self.monitor.schedules())

    async def _handle_console_create_schedule(self, request: web.Request) -> web.Response:
        data = await self._input(request)
        return self._response(self.monitor.create_schedule(dict(data.body)))

    async def _handle_console_update_schedule(self, request: web.Request) -> web.Response:
        data = await self._input(request)
        return self._response(self.monitor.update_schedule(data.path_params["schedule_id"], dict(data.body)))

    async def _handle_console_delete_schedule(self, request: web.Request) -> web.Response:
        data = await self._input(request)
        return self._response(self.monitor.delete_schedule(data.path_params["schedule_id"]))

    async def _handle_console_wallet(self, request: web.Request) -> web.Response:
        return self._response(self.monitor.wallet())

    async def _handle_console_nurture_log(self, request: web.Request) -> web.Response:
        data = await self._input(request)
        return self._response(self.monitor.nurture_log(self._int(data.query.get("hours"), 24)))

    async def _handle_console_calendar(self, request: web.Request) -> web.Response:
        week_start = request.query.get("week_start", "")
        return self._response(self.monitor.calendar(week_start))

    async def _handle_console_panel(self, request: web.Request) -> web.StreamResponse:
        employee_id = request.match_info.get("employee_id") or _default_employee_id()
        index_path = _web_root() / "index.html"
        config = {
            "apiBase": f"{self.api_prefix}/{employee_id}",
            "employeeId": employee_id,
            "employeeName": os.getenv("DIGITAL_LIFE_DISPLAY_NAME", employee_id),
        }
        config_script = (
            "<script>"
            f"window.__EMPLOYEE_CONSOLE__ = {json.dumps(config, ensure_ascii=False)};"
            "</script>"
        )
        html = index_path.read_text(encoding="utf-8")
        if "</head>" in html:
            html = html.replace("</head>", config_script + "</head>", 1)
        else:
            html = config_script + html
        # 给所有 /assets/{file} 引用戳个 ?v=<mtime>,让浏览器永远拉新版
        # (即使浏览器 ETag 命中 304 用旧 cache,query 变了也会强制重新拉)
        import time as _time
        _v = str(int(index_path.stat().st_mtime))
        import re as _re_v
        html = _re_v.sub(r'(/assets/[^"?]+)"', r'\1?v=' + _v + '"', html)
        return web.Response(
            text=html, content_type="text/html",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    async def _redirect_to_default_employee(self, request: web.Request) -> web.Response:
        raise web.HTTPFound(f"{request.path.rstrip('/')}/{_default_employee_id()}/")

    async def _redirect_to_employee_panel(self, request: web.Request) -> web.Response:
        raise web.HTTPFound(f"{request.path}/")

    async def _handle_console_asset(self, request: web.Request) -> web.StreamResponse:
        filename = request.match_info.get("filename", "")
        asset_path = (_web_root() / "assets" / filename).resolve()
        assets_root = (_web_root() / "assets").resolve()
        if assets_root not in asset_path.parents:
            raise web.HTTPNotFound()
        if not asset_path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(asset_path)

    async def _handle_console_metrics(self, request: web.Request) -> web.Response:
        """Prometheus metrics endpoint."""
        try:
            self.monitor.collect_metrics()
            from application.console.metrics import MetricsCollector
            output = MetricsCollector.get_metrics_output()
            return web.Response(
                body=output,
                content_type="text/plain; version=0.0.4; charset=utf-8",
            )
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_console_health(self, request: web.Request) -> web.Response:
        """Health check endpoint."""
        try:
            health = self.monitor.health_check()
            status_code = 200 if health.get("status") == "ok" else 503
            return web.json_response(health, status=status_code)
        except Exception as e:
            return web.json_response({"status": "unhealthy", "error": str(e)}, status=503)

    async def _handle_console_alerts(self, request: web.Request) -> web.Response:
        """Check alerts."""
        try:
            alerts = self.monitor.check_alerts()
            return web.json_response(alerts)
        except Exception as e:
            return web.json_response({"status": "error", "message": str(e)}, status=500)

    async def _handle_console_alert_history(self, request: web.Request) -> web.Response:
        """Get alert history."""
        try:
            data = await self._input(request)
            hours = self._int(data.query.get("hours"), 24)
            history = self.monitor.get_alert_history(hours)
            return web.json_response(history)
        except Exception as e:
            return web.json_response({"status": "error", "message": str(e)}, status=500)

    async def _handle_console_instances(self, request: web.Request) -> web.Response:
        from infrastructure.config import discover_instances, get_instance_display_name, is_instance_active
        instances = []
        for inst_id in discover_instances():
            instances.append({
                "id": inst_id,
                "display_name": get_instance_display_name(inst_id),
                "active": is_instance_active(inst_id),
            })
        current = os.environ.get("DIGITAL_LIFE_INSTANCE_ID", "")
        if current:
            from infrastructure.config import resolve_instance_id
            current = resolve_instance_id(current)
        return web.json_response({"instances": instances, "current": current})

    async def _handle_console_toggle_instance(self, request: web.Request) -> web.Response:
        instance_id = request.match_info.get("instance_id", "")
        if not instance_id:
            return web.json_response({"ok": False, "reason": "instance_id required"}, status=400)
        try:
            data = await self._input(request)
            active = bool(data.body.get("active", False))
        except Exception:
            # Allow GET toggle
            active_param = request.query.get("active", "").lower()
            if active_param in ("true", "1", "yes"):
                active = True
            elif active_param in ("false", "0", "no"):
                active = False
            else:
                from infrastructure.config import is_instance_active
                active = not is_instance_active(instance_id)
        from infrastructure.config import set_instance_active
        set_instance_active(instance_id, active)

        # 联动 InstanceSupervisor：active=True → spawn 子进程；active=False → stop
        # 失败不致命：DB 状态已写，supervisor watch_loop 5s 内会按 last_active.json 漂移兜底
        supervisor = request.app.get("supervisor")
        if supervisor is not None:
            try:
                if active:
                    await supervisor.add_instance(instance_id)
                else:
                    await supervisor.remove_instance(instance_id)
            except Exception as exc:
                import logging
                logging.getLogger(__name__).warning(
                    "supervisor toggle for %s failed: %s (watch_loop will reconcile)",
                    instance_id[:8], exc,
                )

        return web.json_response({"ok": True, "instance_id": instance_id, "active": active})

    # ──────────────────────────────── Contacts (社交关系 + 多平台 ID + 黑名单) ────────────────────────────────

    async def _handle_console_contacts(self, request: web.Request) -> web.Response:
        """GET 列表全部 contacts（含 blocked 标志和 platform_ids）。

        每条 contact 额外带 last_message / last_ts / chat_id / chat_kind 字段,
        取自 messages.db 中该 contact 任一 platform_id 的最近一条入站消息。
        目的: 在 ContactsTab 上让用户看着 platform_id 时能瞬间知道"这人是谁",
        不为维护聊天记录。
        """
        from domain.contacts import list_contacts, ensure_schema
        ensure_schema()
        contacts = list_contacts() or []
        # 批拉 last inbound message: 收集所有 feishu platform_id, 一次 SQL 拿完
        try:
            from domain.messages import last_inbound_per_sender
            sender_ids = []
            for c in contacts:
                for pid in (c.get("platform_ids") or []):
                    pidv = pid.get("platform_id") or ""
                    if pidv:
                        sender_ids.append(pidv)
            last_map = last_inbound_per_sender(sender_ids) if sender_ids else {}
        except Exception:
            last_map = {}
        # 每个 contact 取任一 platform_id 在 last_map 里命中的那条(取 ts 最大)
        for c in contacts:
            best = None
            for pid in (c.get("platform_ids") or []):
                pidv = pid.get("platform_id") or ""
                hit = last_map.get(pidv)
                if hit and (best is None or hit.get("ts", "") > best.get("ts", "")):
                    best = hit
            if best:
                c["last_message"] = best.get("text", "")[:200]
                c["last_ts"] = best.get("ts", "")
                c["chat_id"] = best.get("chat_id", "")
                c["chat_kind"] = best.get("chat_kind", "")
            else:
                c["last_message"] = ""
                c["last_ts"] = ""
                c["chat_id"] = ""
                c["chat_kind"] = ""
        # 窗口档案（chats 表）一并返回——前端展示"窗口"栏
        try:
            from domain.contacts import list_chats
            chats = list_chats(limit=50) or []
        except Exception:
            chats = []
        return web.json_response({"contacts": contacts, "chats": chats})

    async def _handle_console_create_contact(self, request: web.Request) -> web.Response:
        """POST 新增 contact. body: {name, notes?, kind?, platform_ids: [{platform, platform_id}, ...]}"""
        data = await self._input(request)
        name = str(data.body.get("name") or "").strip()
        if not name:
            return web.json_response({"ok": False, "reason": "name 不能为空"}, status=400)
        notes = str(data.body.get("notes") or "").strip()
        kind = str(data.body.get("kind") or "human").strip().lower() or "human"
        # 建模口径（2026-08-26 终版）：contacts 只有人（human/bot/system），
        # 窗口（群/私聊）一律在 chats 表自动建档——不再支持群联系人
        if kind not in ("human", "bot", "system"):
            kind = "human"
        platform_ids = data.body.get("platform_ids") or []
        if not isinstance(platform_ids, list) or not platform_ids:
            return web.json_response({"ok": False, "reason": "至少需要一个 platform_id"}, status=400)
        cleaned = [
            {"platform": str(p.get("platform") or "").strip(), "platform_id": str(p.get("platform_id") or "").strip()}
            for p in platform_ids if isinstance(p, dict)
        ]
        cleaned = [p for p in cleaned if p["platform"] and p["platform_id"]]
        if not cleaned:
            return web.json_response({"ok": False, "reason": "platform_ids 字段不合法"}, status=400)
        from domain.contacts import create_contact, ensure_schema
        ensure_schema()
        contact = create_contact(name=name, notes=notes, kind=kind, platform_ids=cleaned)
        return web.json_response({"ok": True, "contact": contact}, status=201)

    async def _handle_console_update_chat_notes(self, request: web.Request) -> web.Response:
        """PATCH /api/employee/{iid}/chats/{chat_id}/notes — 编辑窗口备注。"""
        from domain.contacts import update_chat_notes
        chat_id = request.match_info.get("chat_id", "").strip()
        if not chat_id:
            return web.json_response({"ok": False, "reason": "chat_id 不能为空"}, status=400)
        data = await self._input(request)
        notes = str(data.body.get("notes") or "").strip()
        ok = update_chat_notes(chat_id, notes)
        if not ok:
            return web.json_response({"ok": False, "reason": "窗口不存在"}, status=404)
        return web.json_response({"ok": True})

    async def _handle_console_update_contact(self, request: web.Request) -> web.Response:
        """PATCH 修改 contact 字段. body: name? / notes? / platform_ids?"""
        data = await self._input(request)
        contact_id = data.path_params.get("id", "").strip()
        if not contact_id:
            return web.json_response({"ok": False, "reason": "id 不能为空"}, status=400)
        body = dict(data.body)
        kwargs: dict = {}
        for k in ("name", "notes"):
            if k in body:
                kwargs[k] = str(body[k] or "").strip()
        if "kind" in body:
            k = str(body["kind"] or "human").strip().lower()
            kwargs["kind"] = k if k in ("human", "bot", "system") else "human"
        if "platform_ids" in body:
            pids = body["platform_ids"] or []
            if not isinstance(pids, list):
                return web.json_response({"ok": False, "reason": "platform_ids 必须是数组"}, status=400)
            kwargs["platform_ids"] = [
                {"platform": str(p.get("platform") or "").strip(), "platform_id": str(p.get("platform_id") or "").strip()}
                for p in pids if isinstance(p, dict)
            ]
        from domain.contacts import update_contact, ensure_schema
        ensure_schema()
        contact = update_contact(contact_id, **kwargs)
        if not contact:
            return web.json_response({"ok": False, "reason": "contact not found"}, status=404)
        return web.json_response({"ok": True, "contact": contact})

    async def _handle_console_toggle_block_contact(self, request: web.Request) -> web.Response:
        """POST toggle blocked. body: {blocked: bool, reason?: str}"""
        data = await self._input(request)
        contact_id = data.path_params.get("id", "").strip()
        if not contact_id:
            return web.json_response({"ok": False, "reason": "id 不能为空"}, status=400)
        blocked = bool(data.body.get("blocked", False))
        reason = str(data.body.get("reason") or "").strip()
        from domain.contacts import set_blocked, ensure_schema
        ensure_schema()
        changed = set_blocked(contact_id, blocked, reason)
        return web.json_response({"ok": changed, "id": contact_id, "blocked": blocked})

    async def _handle_console_delete_contact(self, request: web.Request) -> web.Response:
        """DELETE 按 id 删除（连带 platform_ids 也清）。"""
        data = await self._input(request)
        contact_id = data.path_params.get("id", "").strip()
        if not contact_id:
            return web.json_response({"ok": False, "reason": "id 不能为空"}, status=400)
        from domain.contacts import del_contact, ensure_schema
        ensure_schema()
        removed = del_contact(contact_id)
        return web.json_response({"ok": removed, "id": contact_id})

    async def _handle_console_merge_contacts(self, request: web.Request) -> web.Response:
        """POST /api/employee/{iid}/contacts/merge — 合并两个联系人。

        body: {"source_id": "xxx", "target_id": "yyy"}
        source 的 platform_ids 迁移到 target，source 被删除。
        """
        data = await self._input(request)
        body = data.body or {}
        source_id = (body.get("source_id") or "").strip()
        target_id = (body.get("target_id") or "").strip()
        if not source_id or not target_id:
            return web.json_response({"ok": False, "reason": "source_id 和 target_id 必填"}, status=400)
        from domain.contacts import merge_contacts, ensure_schema
        ensure_schema()
        ok = merge_contacts(source_id, target_id)
        return web.json_response({"ok": ok, "source_id": source_id, "target_id": target_id})

    # Chat members 已随去中心化消息总线移除：每个实例自管 subscriptions.yaml，
    # 跨实例广播走 HTTP 广播端点（见 docs/architecture/decentralized-message-bus.md）。

    # ──────────────────────────────── Wake Snapshot Detail (v5 R2) ────────────────────────────────

    async def _handle_console_wakes(self, request: web.Request) -> web.Response:
        """GET /wakes?limit=50&offset=0&chat_id=... — 最近唤醒列表（来自新 audit DB）。

        支持分页：limit 控制每页大小，offset 控制起始位置。
        返回 {wakes: [...], total: N, has_more: bool}。
        """
        data = await self._input(request)
        limit = self._int(data.query.get("limit"), 50)
        offset = self._int(data.query.get("offset"), 0)
        chat_id = data.query.get("chat_id") or None
        try:
            from infrastructure.persistence.instance import get_audit
            audit = get_audit()
            wakes = audit.list_wakes(chat_id=chat_id, limit=limit, offset=offset)
            total = audit.count_wakes(chat_id=chat_id)
            return web.json_response({
                "wakes": wakes,
                "total": total,
                "has_more": (offset + len(wakes)) < total,
            })
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

    async def _handle_console_wake_detail(self, request: web.Request) -> web.Response:
        """GET /wakes/{wake_id} — 单 wake 详情：turns + injections 完整列表。"""
        try:
            wake_id = int(request.match_info.get("wake_id", ""))
        except ValueError:
            return web.json_response({"error": "invalid wake_id"}, status=400)
        try:
            from infrastructure.persistence.instance import get_audit
            audit = get_audit()
            wake = audit.get_wake(wake_id)
            if not wake:
                return web.json_response({"error": "not found"}, status=404)
            turns = audit.list_turns(wake_id)
            injections = audit.list_injections(wake_id)
            return web.json_response({
                "wake": wake,
                "turns": turns,
                "injections": injections,
            })
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

    async def _handle_console_wake_stream(self, request: web.Request) -> web.StreamResponse:
        """GET /wakes/{wake_id}/stream — SSE:turns 增量推送,让前端 stay 时新自动看到。

        协议:
          event: snapshot  data: {wake, turns, injections}  —— 首帧(降级 = 当前完整)
          event: turn      data: {turn}                      —— 后续增量(id 增长序)
          event: end       data: {reason}                    —— wake 结束

        实现:
          polling tail(0.6s/次)按 turn.id > last_seen 查询。
          master 进程直接读 apps/<iid>/data/runtime_log.db file(WAL)即可。
          client 断开(request.transport.is_closing())自动结束, 不泄漏。
        """
        import asyncio
        import json as _json
        import time

        try:
            wake_id = int(request.match_info.get("wake_id", ""))
        except (ValueError, TypeError):
            return web.json_response({"error": "invalid wake_id"}, status=400)

        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # nginx 不缓冲
            },
        )
        await resp.prepare(request)

        async def _send(event_name: str, data: dict) -> None:
            payload = f"event: {event_name}\ndata: {_json.dumps(data, ensure_ascii=False, default=str)}\n\n"
            await resp.write(payload.encode("utf-8"))
            await resp.drain()

        from infrastructure.persistence.instance import get_audit
        audit = get_audit()

        try:
            wake = audit.get_wake(wake_id)
            if not wake:
                await _send("error", {"error": "wake not found"})
                return resp
            # 首帧 snapshot
            turns = audit.list_turns(wake_id)
            injections = audit.list_injections(wake_id)
            await _send("snapshot", {"wake": wake, "turns": turns, "injections": injections})
            last_id = max((t.get("id") or 0) for t in turns) if turns else 0

            # polling tail
            tick = 0
            while True:
                # 客户端断开 → 退出
                if request.transport is None or request.transport.is_closing():
                    break
                await asyncio.sleep(0.6)
                # 读 wake 是否结束(ended_at 已填)
                w_now = audit.get_wake(wake_id) or {}
                # 增量 turns
                try:
                    # 直接 SQL 查 id > last_id 防止 audit.list_turns 全量重读成本
                    all_turns = audit.list_turns(wake_id)
                    new_turns = [t for t in all_turns if (t.get("id") or 0) > last_id]
                    for t in new_turns:
                        await _send("turn", {"turn": t})
                        last_id = max(last_id, t.get("id") or 0)
                except Exception:
                    pass
                # wake 结束 → 发 end 后退出
                if w_now.get("ended_at"):
                    await _send("end", {
                        "reason": (w_now.get("meta_json") or {}).get("end_reason") if isinstance(w_now.get("meta_json"), dict) else None,
                    })
                    break
                tick += 1
                # 兜底:最长 4h 后强制断(防僵尸连接)
                if tick > 24000:
                    await _send("end", {"reason": "stream_timeout"})
                    break
        except ConnectionResetError:
            pass  # client 断了
        except Exception as exc:
            try:
                await _send("error", {"error": str(exc)})
            except Exception:
                pass
        finally:
            try:
                await resp.write_eof()
            except Exception:
                pass
        return resp

    async def _handle_console_wake_call_input(self, request: web.Request) -> web.Response:
        """GET /wakes/{wake_id}/input/{call_seq} — 已重组的某次 LLM call 的输入 messages。

        优先从 sessions_dumps/ 字面 JSON 读取（ground truth），找不到再 fallback
        到旧的 render_input_for_call 重组路径。dump 路径最近部署，老数据可能没覆盖。
        """
        try:
            wake_id = int(request.match_info.get("wake_id", ""))
            call_seq = int(request.match_info.get("call_seq", "0"))
        except ValueError:
            return web.json_response({"error": "invalid wake_id/call_seq"}, status=400)
        # 路由里的 employee_id 是当前实例
        employee_id = request.match_info.get("employee_id") or ""
        try:
            # 路径 1: 优先读 sessions_dumps 字面 JSON
            try:
                from infrastructure.config import get_runtime_home, set_current_instance_id
                from pathlib import Path
                # get_runtime_home() 已经返回 apps/<id>/data —— 这一层不能再拼 apps/<id>/data
                dumps_dir = Path(get_runtime_home()) / "sessions_dumps"
                if dumps_dir.is_dir():
                    import json as _json
                    # 按 mtime 倒序扫，最多 200 份最近的
                    files = sorted(dumps_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
                    for f in files[:200]:
                        try:
                            d = _json.loads(f.read_text(encoding="utf-8"))
                            if d.get("wake_id") == wake_id and d.get("call_seq") == call_seq:
                                return web.json_response({
                                    "messages": d.get("messages", []),
                                    "source": "sessions_dumps",
                                    "session_id": d.get("session_id"),
                                    "model": d.get("model"),
                                    "timestamp": d.get("timestamp"),
                                })
                        except Exception:
                            continue
            except Exception:
                pass

            # 路径 2: fallback 到 render_input_for_call
            from infrastructure.persistence.instance import get_audit
            audit = get_audit()

            def _persona_loader(ref: str) -> str:
                if not ref or not ref.startswith("instance:"):
                    return ""
                iid = ref.split(":", 1)[1]
                try:
                    from pathlib import Path
                    from infrastructure.config import get_instance_persona_path
                    p = get_instance_persona_path(iid)
                    if p.is_file():
                        return p.read_text(encoding="utf-8")
                except Exception:
                    pass
                return ""

            msgs = audit.render_input_for_call(wake_id, call_seq, persona_loader=_persona_loader)
            return web.json_response({"messages": msgs, "source": "render_input_for_call"})
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

    # ──────────────────────────────── Entity / Memory advisor ─────────────────────────────────

    async def _handle_console_entities(self, request: web.Request) -> web.Response:
        """GET /entities?q=&need_profile=true
        List all entities in current instance's index, optionally filtered."""
        data = await self._input(request)
        try:
            from domain.memory.memory.consciousness.entity_index import (
                list_entity_names, get_entity_summary,
            )
            names = list_entity_names()
            q = (data.query.get("q") or "").strip()
            if q:
                ql = q.lower()
                names = [n for n in names if ql in n.lower()]
            need_profile_only = data.query.get("need_profile") == "true"
            rows = []
            for n in names[:200]:  # cap for HTTP
                info = get_entity_summary(n) or {}
                mem_count = len(info.get("memories", []))
                has_profile = bool(info.get("profile"))
                if need_profile_only and not has_profile:
                    continue
                rows.append({
                    "name": n,
                    "kind": info.get("type"),
                    "aliases": info.get("aliases", []),
                    "memory_count": mem_count,
                    "has_profile": has_profile,
                    "summary": (info.get("profile") or {}).get("summary", ""),
                })
            return web.json_response({"entities": rows, "total": len(rows)})
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

    async def _handle_console_entity_detail(self, request: web.Request) -> web.Response:
        """GET /entities/{name} — full profile + recent memories."""
        name = request.match_info.get("name", "")
        if not name:
            return web.json_response({"error": "name required"}, status=400)
        try:
            from domain.memory.memory.consciousness.entity_index import get_entity_summary
            info = get_entity_summary(name)
            if not info:
                return web.json_response({"error": "not found"}, status=404)
            return web.json_response({"entity": name, "info": info})
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

    async def _handle_console_entity_profile(self, request: web.Request) -> web.Response:
        """PUT/POST /entities/{name}/profile — set profile (summary / facts / kind / aliases).
        Real-person writer only. Replaces existing profile.
        """
        name = request.match_info.get("name", "")
        if not name:
            return web.json_response({"error": "name required"}, status=400)
        data = await self._input(request)
        body = data.body or {}
        try:
            import json as _json
            from domain.memory.memory.consciousness.entity_index import set_entity_profile
            extra_raw = body.get("extra") or "{}"
            try:
                extra = _json.loads(extra_raw) if isinstance(extra_raw, str) else extra_raw
            except Exception:
                extra = {}
            result = set_entity_profile(
                name,
                kind=(body.get("kind") or "").strip() or None,
                aliases=body.get("aliases") or [],
                summary=(body.get("summary") or "").strip(),
                facts=body.get("facts") or [],
                extra=extra if isinstance(extra, dict) else {},
            )
            return web.json_response(result)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

    async def _handle_console_entity_health(self, request: web.Request) -> web.Response:
        """GET /entity-health — index audit (missing profiles / merge candidates)."""
        try:
            from domain.memory.memory.consciousness.entity_index import index_health_check
            return web.json_response(index_health_check())
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

    async def _handle_console_entity_merge(self, request: web.Request) -> web.Response:
        """POST /entities/merge — merge {alias} into {primary}.
        """
        data = await self._input(request)
        primary = (data.body.get("primary") or "").strip()
        alias = (data.body.get("alias") or "").strip()
        if not primary or not alias:
            return web.json_response({"ok": False, "reason": "primary 和 alias 必填"}, status=400)
        try:
            from domain.memory.memory.consciousness.entity_index import merge_entities
            result = merge_entities(primary, alias)
            return web.json_response(result)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

    async def _handle_console_entity_prune(self, request: web.Request) -> web.Response:
        """POST /entities/{name}/prune?keep=N — trim fragments after profile extraction.
        """
        name = request.match_info.get("name", "")
        data = await self._input(request)
        keep = int(data.body.get("keep") or data.query.get("keep") or 5)
        if not name:
            return web.json_response({"ok": False, "reason": "name 必填"}, status=400)
        try:
            from domain.memory.memory.consciousness.entity_index import prune_fragments_for_entity
            result = prune_fragments_for_entity(name, keep=keep)
            return web.json_response(result)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

    # ──────────────────────────────── Todos (v5 待办) ────────────────────────────────

    async def _handle_console_todos(self, request: web.Request) -> web.Response:
        """GET /todos?status=&project_id=&assignee=&include_unassigned=

        新语义（global_todos.db 重构后 2026-06-14）：
          - 数据源是 global_todos.db.todos（不再是 todo_triggers 或 instance todos.db）
          - 默认查"分配给当前实例的 todos"（assignee=me）
          - include_unassigned=1 同时返回未指派的（例如新建的根 todo）
          - project_id=可过滤某个项目
        """
        data = await self._input(request)
        from infrastructure.config import get_app_instance_id
        my_iid = get_app_instance_id() or ""

        from domain.todos import list_tasks
        assignee_param = data.query.get("assignee") or None
        # 没显式传 assignee 时默认查"分给我的"(产品语义:查我自己要做的事)
        # 用 _explicit=1 来跳过自动过滤(-- 想查全部时用)
        if assignee_param is None and data.query.get("all") != "1":
            assignee_param = my_iid

        items = list_tasks(
            assignee_instance=assignee_param,
            include_unassigned=(data.query.get("include_unassigned") == "1"),
            project_id=data.query.get("project_id") or None,
        )
        return web.json_response({"todos": items})

    async def _handle_console_create_todo(self, request: web.Request) -> web.Response:
        data = await self._input(request)
        body = dict(data.body)
        task_id = str(body.get("task_id") or "").strip()
        if not task_id:
            return web.json_response({"ok": False, "reason": "task_id 必填"}, status=400)
        content = str(body.get("content") or "").strip()
        if not content:
            return web.json_response({"ok": False, "reason": "content 必填"}, status=400)
        from infrastructure.config import get_app_instance_id
        assignee = str(body.get("assignee") or get_app_instance_id() or "").strip()
        from domain.todos import create_todo
        result = create_todo(
            task_id, assignee, content,
            trigger_type=str(body.get("trigger_type") or "time").strip().lower(),
            due_at=str(body.get("due_at") or "").strip() or None,
            condition=str(body.get("condition") or "").strip() or None,
        )
        return web.json_response(result, status=201 if result.get("ok") else 409)

    async def _handle_console_update_todo(self, request: web.Request) -> web.Response:
        data = await self._input(request)
        try:
            todo_id = int(data.path_params.get("todo_id", ""))
        except ValueError:
            return web.json_response({"ok": False, "reason": "todo_id 必须是数字"}, status=400)
        from domain.todos import update_todo
        body = dict(data.body)
        kw = {}
        for k in ("content", "due_at", "trigger_condition", "status"):
            if k in body and body[k] is not None:
                kw[k] = body[k]
        result = update_todo(todo_id, **kw)
        return web.json_response(result)

    async def _handle_console_delete_todo(self, request: web.Request) -> web.Response:
        data = await self._input(request)
        try:
            todo_id = int(data.path_params.get("todo_id", ""))
        except ValueError:
            return web.json_response({"ok": False, "reason": "todo_id 必须是数字"}, status=400)
        from domain.todos import delete_todo
        result = delete_todo(todo_id)
        return web.json_response(result)

    # ──────────────────────────────── Projects (v5 项目视图) ────────────────────────────────

    async def _handle_console_projects(self, request: web.Request) -> web.Response:
        """GET /projects — 列所有项目"""
        try:
            from domain.project.loader import load_all_projects
            all_p = load_all_projects() or {}
            items = []
            for pid, cfg in all_p.items():
                items.append({
                    "id": pid,
                    "name": cfg.name,
                    "description": cfg.description,
                    "status": cfg.status,
                    "manager": cfg.manager,
                    "group_chat_id": getattr(cfg, "group_chat_id", "") or "",
                    "positions": [
                        {"id": p.id, "name": p.name, "assignees": p.assignees}
                        for p in (cfg.positions or [])
                    ],
                })
            return web.json_response({"projects": items})
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

    async def _handle_console_project_detail(self, request: web.Request) -> web.Response:
        """GET /projects/{pid}"""
        project_id = request.match_info.get("project_id", "")
        try:
            from domain.project.loader import load_project
            cfg = load_project(project_id)
            if not cfg:
                return web.json_response({"error": "not found"}, status=404)
            return web.json_response({
                "project": {
                    "id": project_id,
                    "name": cfg.name,
                    "description": cfg.description,
                    "status": cfg.status,
                    "manager": cfg.manager,
                    "group_chat_id": getattr(cfg, "group_chat_id", "") or "",
                    "positions": [
                        {"id": p.id, "name": p.name, "assignees": p.assignees,
                         "responsibilities": p.responsibilities}
                        for p in (cfg.positions or [])
                    ],
                }
            })
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

    async def _handle_console_project_tasks(self, request: web.Request) -> web.Response:
        """GET /projects/{pid}/tasks —— Phase 4:走 global_todos.db WHERE project_id=pid"""
        project_id = request.match_info.get("project_id", "")
        try:
            from domain.project.crud import list_deliverables
            items = list_deliverables(db=None, project_id=project_id)
            return web.json_response({"tasks": items})
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

    async def _handle_console_create_project(self, request: web.Request) -> web.Response:
        """POST /projects — 前端创建项目。

        做三件事：
          1. 调 domain.project.manager.create_project_full() 建项目骨架
          2. 给项目经理实例 emit「project_created」事件（产品语义：
             用户通过控制台做了一个动作 = 给项目经理发一条事实通知，
             触发它走 project_bootstrap skill 完善流程）
          3. 返 project_id 给前端

        事件路由：通过 set_current_instance_id(manager_iid) 指定 emit 目标实例，
        emit 后 reset 回 handler 当前上下文。
        """
        data = await self._input(request)
        body = dict(data.body)
        name = str(body.get("name") or "").strip()
        if not name:
            return web.json_response({"ok": False, "reason": "name 必填"}, status=400)
        description = str(body.get("description") or "")
        manager = str(body.get("manager") or "").strip()
        if not manager:
            return web.json_response({"ok": False, "reason": "manager 必填（实例 id）"}, status=400)
        group_chat_id = str(body.get("group_chat_id") or "")
        positions = body.get("positions") or []
        created_by = str(body.get("created_by") or "")

        # 生成 project_id（proj-NNN 顺序）
        import os as _os
        try:
            existing = sorted([d for d in _os.listdir("projects")
                              if _os.path.isdir(f"projects/{d}") and d.startswith("proj-")])
            max_n = 0
            for d in existing:
                try:
                    n = int(d.replace("proj-", ""))
                    max_n = max(max_n, n)
                except ValueError:
                    continue
            project_id = f"proj-{max_n + 1:03d}"
        except Exception:
            project_id = f"proj-{int(__import__('time').time()) % 10000:04d}"

        try:
            from domain.project.manager import create_project_full
            ok = create_project_full(
                project_id=project_id,
                name=name,
                description=description,
                manager=manager,
                positions=positions if isinstance(positions, list) else [],
                group_chat_id=group_chat_id,
            )
            if not ok:
                return web.json_response({"ok": False, "reason": "create_project_full 返回 False"}, status=500)
        except Exception as exc:
            return web.json_response({"ok": False, "reason": str(exc)}, status=500)

        # emit project_created 事件给 manager 实例
        try:
            from infrastructure.config import set_current_instance_id, get_app_instance_id
            from domain.lifecycle.events import emit_event
            from contextlib import contextmanager

            @contextmanager
            def _scoped_instance(iid: str):
                token = set_current_instance_id(iid)
                try:
                    yield
                finally:
                    if token is not None:
                        set_current_instance_id(token)

            payload = {
                "project_id": project_id,
                "project_name": name,
                "manager": manager,
                "created_by": created_by or "user",
                "basic_positions": positions if isinstance(positions, list) else [],
            }
            with _scoped_instance(manager):
                event_id = emit_event("project_created", payload=payload)
        except Exception as exc:
            # 事件 emit 失败不应阻塞响应；项目已创建
            import logging
            logging.getLogger("digital_life.api.console").warning(
                "emit project_created failed: %s", exc, exc_info=True
            )
            event_id = 0

        return web.json_response({
            "ok": True,
            "project_id": project_id,
            "event_id": event_id,
            "message": f"项目已创建并通知项目经理实例 {manager[:8]}…",
        })

    async def _handle_console_create_project_task(self, request: web.Request) -> web.Response:
        """POST /projects/{pid}/tasks —— Phase 4:走 global_todos.create_task(以 deliverable 形态创建)"""
        project_id = request.match_info.get("project_id", "")
        data = await self._input(request)
        body = dict(data.body)
        title = str(body.get("title") or "").strip()
        if not title:
            return web.json_response({"ok": False, "reason": "title 必填"}, status=400)
        # Phase 4:跨 instance 写 — 前端创建的项目任务。不传 instance context,
        # create_task 内部拿当前 ContextVar 兜底(空也没关系—— assignee_instance 显式传)。
        try:
            from domain.project.crud import create_deliverable
            did = create_deliverable(
                db=None,
                title=title,
                description=str(body.get("description") or ""),
                priority=str(body.get("priority") or "medium"),
                assignee_instance=str(body.get("assignee_instance") or ""),
                assignee_position=str(body.get("assignee_position") or ""),
                project_id=project_id,
                acceptance_criteria=str(body.get("acceptance_criteria") or ""),
            )
        except Exception as exc:
            return web.json_response({"ok": False, "reason": str(exc)}, status=500)
        return web.json_response({"ok": bool(did), "task_id": did}, status=201 if did else 500)

    async def _handle_console_update_project_task(self, request: web.Request) -> web.Response:
        """PATCH /projects/{pid}/tasks/{tid} —— Phase 4:走 update_deliverable(= update_task)"""
        project_id = request.match_info.get("project_id", "")
        task_id = request.match_info.get("task_id", "")
        data = await self._input(request)
        from domain.project.crud import update_deliverable
        ok = update_deliverable(db=None, deliverable_id=task_id, project_id=project_id, **dict(data.body))
        return web.json_response({"ok": ok})

    async def _handle_console_create_instance(self, request: web.Request) -> web.Response:
        data = await self._input(request)
        display_name = str(data.body.get("display_name") or data.body.get("name", "")).strip()
        if not display_name:
            return web.json_response({"ok": False, "reason": "实例显示名称不能为空"}, status=400)
        if len(display_name) > 64:
            return web.json_response({"ok": False, "reason": "实例显示名称不能超过 64 个字符"}, status=400)
        try:
            from infrastructure.bootstrap.instance import init_instance
            inst_dir = init_instance(display_name)
            return web.json_response({
                "ok": True,
                "display_name": display_name,
                "path": str(inst_dir),
                "hint": "请编辑人设文件后重启 gateway",
            }, status=201)
        except SystemExit as e:
            msg = str(e) or "创建失败"
            return web.json_response({"ok": False, "reason": msg}, status=400)
        except Exception as e:
            return web.json_response({"ok": False, "reason": str(e)}, status=500)


@web.middleware
async def _instance_context_middleware(request: web.Request, handler) -> web.StreamResponse:
    employee_id = request.match_info.get("employee_id")
    if not employee_id:
        return await handler(request)
    from infrastructure.config import resolve_instance_id, set_current_instance_id, reset_current_instance_id
    resolved = resolve_instance_id(employee_id)
    # Set BOTH ContextVars so DB paths (config.py) AND event channels (events.py) are correct.
    # os.environ is process-global and races with cron's env var switches; ContextVars are
    # per-asyncio-task and immune to that race.
    from domain.lifecycle.events import set_instance_context, reset_instance_context
    ev_ctx = set_instance_context(resolved)
    cfg_ctx = set_current_instance_id(resolved)
    prev_env = os.environ.get("DIGITAL_LIFE_INSTANCE_ID")
    os.environ["DIGITAL_LIFE_INSTANCE_ID"] = resolved
    try:
        return await handler(request)
    finally:
        os.environ["DIGITAL_LIFE_INSTANCE_ID"] = prev_env if prev_env is not None else ""
        if prev_env is None:
            os.environ.pop("DIGITAL_LIFE_INSTANCE_ID", None)
        reset_current_instance_id(cfg_ctx)
        reset_instance_context(ev_ctx)


def _add_console_api_routes(app: web.Application, api_prefix: str, service: EmployeeConsoleAPIService) -> None:
    app.router.add_get(f"{api_prefix}/status", service._handle_console_status)
    app.router.add_get(f"{api_prefix}/sessions", service._handle_console_sessions)
    app.router.add_get(f"{api_prefix}/sessions/{{session_id}}", service._handle_console_session_detail)
    app.router.add_get(f"{api_prefix}/sessions/{{session_id}}/raw", service._handle_console_session_raw)
    app.router.add_get(f"{api_prefix}/sessions/{{session_id}}/full", service._handle_console_session_full)
    app.router.add_get(f"{api_prefix}/event-log/runs/{{run_id}}", service._handle_console_run_event_log)
    app.router.add_get(f"{api_prefix}/memories/{{name}}", service._handle_console_memories)
    app.router.add_get(f"{api_prefix}/memories/{{name}}/dates", service._handle_console_memory_dates)
    app.router.add_get(f"{api_prefix}/events", service._handle_console_events)
    app.router.add_get(f"{api_prefix}/events/queue", service._handle_console_event_queue)
    app.router.add_get(f"{api_prefix}/associations", service._handle_console_associations)
    app.router.add_get(f"{api_prefix}/chunks", service._handle_console_chunks)
    app.router.add_get(f"{api_prefix}/chunks/{{id}}", service._handle_console_chunk_detail)
    app.router.add_get(f"{api_prefix}/tasks", service._handle_console_tasks)
    app.router.add_get(f"{api_prefix}/tasks/{{id}}", service._handle_console_task_detail)
    app.router.add_post(f"{api_prefix}/tasks", service._handle_console_create_task)
    app.router.add_patch(f"{api_prefix}/tasks/{{id}}", service._handle_console_update_task)
    app.router.add_get(f"{api_prefix}/tasks/{{id}}/plans", service._handle_console_task_plans)
    app.router.add_post(f"{api_prefix}/tasks/{{id}}/plans", service._handle_console_create_plan)
    app.router.add_patch(f"{api_prefix}/tasks/{{id}}/plans/{{pid}}", service._handle_console_update_plan)
    app.router.add_get(f"{api_prefix}/tasks/{{id}}/notes", service._handle_console_task_notes)
    app.router.add_post(f"{api_prefix}/nurture", service._handle_console_nurture)
    app.router.add_post(f"{api_prefix}/nurture-energy", service._handle_console_nurture_energy)
    app.router.add_post(f"{api_prefix}/deltas", service._handle_console_deltas)
    app.router.add_get(f"{api_prefix}/predictions", service._handle_console_predictions)
    app.router.add_get(f"{api_prefix}/schedules", service._handle_console_schedules)
    app.router.add_post(f"{api_prefix}/schedules", service._handle_console_create_schedule)
    app.router.add_patch(f"{api_prefix}/schedules/{{schedule_id}}", service._handle_console_update_schedule)
    app.router.add_delete(f"{api_prefix}/schedules/{{schedule_id}}", service._handle_console_delete_schedule)
    app.router.add_get(f"{api_prefix}/calendar", service._handle_console_calendar)
    app.router.add_get(f"{api_prefix}/wallet", service._handle_console_wallet)
    app.router.add_get(f"{api_prefix}/nurture-log", service._handle_console_nurture_log)
    app.router.add_get(f"{api_prefix}/config", service._handle_console_config)
    app.router.add_patch(f"{api_prefix}/config", service._handle_console_update_config)
    app.router.add_get(f"{api_prefix}/prompts", service._handle_console_prompts)
    app.router.add_get(f"{api_prefix}/event-types", service._handle_console_event_types)
    app.router.add_patch(f"{api_prefix}/prompts/{{name}}", service._handle_console_update_prompt)
    app.router.add_get(f"{api_prefix}/metrics", service._handle_console_metrics)
    app.router.add_get(f"{api_prefix}/health", service._handle_console_health)
    app.router.add_get(f"{api_prefix}/instances", service._handle_console_instances)
    app.router.add_post(f"{api_prefix}/instances", service._handle_console_create_instance)
    app.router.add_post(f"{api_prefix}/instances/{{instance_id}}/active", service._handle_console_toggle_instance)
    app.router.add_get(f"{api_prefix}/alerts", service._handle_console_alerts)
    app.router.add_get(f"{api_prefix}/alerts/history", service._handle_console_alert_history)

    # Contacts (社交关系 + 多平台 ID + 黑名单 toggle)
    app.router.add_get(f"{api_prefix}/contacts", service._handle_console_contacts)
    app.router.add_post(f"{api_prefix}/contacts", service._handle_console_create_contact)
    app.router.add_patch(f"{api_prefix}/contacts/{{id}}", service._handle_console_update_contact)
    app.router.add_post(f"{api_prefix}/contacts/{{id}}/block", service._handle_console_toggle_block_contact)
    app.router.add_delete(f"{api_prefix}/contacts/{{id}}", service._handle_console_delete_contact)
    app.router.add_post(f"{api_prefix}/contacts/merge", service._handle_console_merge_contacts)
    app.router.add_patch(f"{api_prefix}/chats/{{chat_id}}/notes", service._handle_console_update_chat_notes)

    # todos (v5)
    app.router.add_get(f"{api_prefix}/todos", service._handle_console_todos)
    app.router.add_post(f"{api_prefix}/todos", service._handle_console_create_todo)
    app.router.add_patch(f"{api_prefix}/todos/{{todo_id}}", service._handle_console_update_todo)
    app.router.add_delete(f"{api_prefix}/todos/{{todo_id}}", service._handle_console_delete_todo)

    # wake snapshot detail (v5 R2)
    app.router.add_get(f"{api_prefix}/wakes", service._handle_console_wakes)
    app.router.add_get(f"{api_prefix}/wakes/{{wake_id}}", service._handle_console_wake_detail)
    app.router.add_get(f"{api_prefix}/wakes/{{wake_id}}/stream", service._handle_console_wake_stream)
    app.router.add_get(f"{api_prefix}/wakes/{{wake_id}}/input/{{call_seq}}", service._handle_console_wake_call_input)
    # Memory advisor (entities + profiles + merges + audit):
    app.router.add_get(f"{api_prefix}/entities", service._handle_console_entities)
    app.router.add_get(f"{api_prefix}/entity-health", service._handle_console_entity_health)
    app.router.add_get(f"{api_prefix}/entities/{{name}}", service._handle_console_entity_detail)
    app.router.add_put(f"{api_prefix}/entities/{{name}}/profile", service._handle_console_entity_profile)
    app.router.add_post(f"{api_prefix}/entities/{{name}}/profile", service._handle_console_entity_profile)
    app.router.add_post(f"{api_prefix}/entities/merge", service._handle_console_entity_merge)
    app.router.add_post(f"{api_prefix}/entities/{{name}}/prune", service._handle_console_entity_prune)

    # projects (v5)
    app.router.add_get(f"{api_prefix}/projects", service._handle_console_projects)
    app.router.add_post(f"{api_prefix}/projects", service._handle_console_create_project)
    app.router.add_get(f"{api_prefix}/projects/{{project_id}}", service._handle_console_project_detail)
    app.router.add_get(f"{api_prefix}/projects/{{project_id}}/tasks", service._handle_console_project_tasks)
    app.router.add_post(f"{api_prefix}/projects/{{project_id}}/tasks", service._handle_console_create_project_task)
    app.router.add_patch(f"{api_prefix}/projects/{{project_id}}/tasks/{{task_id}}", service._handle_console_update_project_task)
    app.router.add_get(f"{api_prefix}/budget", service._handle_console_budget)
    app.router.add_get(f"{api_prefix}/budget/series", service._handle_console_budget_series)
    app.router.add_get(f"{api_prefix}/vitals/series", service._handle_console_vitals_series)

    # 社交接管: 授权入口 + 状态查询 + 解除授权
    app.router.add_get(f"{api_prefix}/social/status", service._handle_console_social_status)
    app.router.add_post(f"{api_prefix}/social/revoke", service._handle_console_social_revoke)


def register(app: web.Application, adapter: Any) -> None:
    service = EmployeeConsoleAPIService(adapter)
    app["employee_console_service"] = service
    web_prefix = _route_prefix("DIGITAL_LIFE_EMPLOYEE_CONSOLE_WEB_ROUTE_PREFIX", "/employee")
    api_prefix = _route_prefix("DIGITAL_LIFE_EMPLOYEE_CONSOLE_API_PREFIX", "/api/employee")
    service.api_prefix = api_prefix

    app.middlewares.append(_instance_context_middleware)

    app.router.add_get(web_prefix, service._redirect_to_default_employee)
    app.router.add_get(f"{web_prefix}/", service._redirect_to_default_employee)
    app.router.add_get(f"{web_prefix}/{{employee_id}}", service._redirect_to_employee_panel)
    app.router.add_get(f"{web_prefix}/{{employee_id}}/", service._handle_console_panel)
    app.router.add_get(f"{web_prefix}/{{employee_id}}/assets/{{filename:.*}}", service._handle_console_asset)

    _add_console_api_routes(app, api_prefix, service)
    _add_console_api_routes(app, f"{api_prefix}/{{employee_id}}", service)
