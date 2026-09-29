"""服务管理 API — 管理台的服务视角（刀 3 管理面雏形）。

管理台（system/instances）只有实例视角；服务型运行体（services.db）的
运行过程——唤醒记录、队列积压、配置、项目群沟通——经本模块暴露，
配套零构建页面 /services。

鉴权：demo 最简（内网 8642，同 employee-console 现状——console 的
X-API-Key 也未校验，先对齐现状，账号体系与 alpha 契约一起做）。
"""

from __future__ import annotations

import json
import logging
import sqlite3

from aiohttp import web

logger = logging.getLogger("digital_life.api.services_admin")

_ROUTER = web.Application()


def _json(data, status: int = 200) -> web.Response:
    return web.json_response(data, status=status)


def _svc_db(service_id: str, filename: str):
    from infrastructure.config import resolve_runtime_dir

    return resolve_runtime_dir(service_id) / "data" / filename


def _service_stats(service_id: str) -> dict:
    """服务的运行统计：唤醒数/最近唤醒/待处理事件/消息数。"""
    stats = {"wakes": 0, "last_wake_at": None, "open_events": 0, "messages": 0}
    db = _svc_db(service_id, "runtime_log.db")
    if db.exists():
        try:
            conn = sqlite3.connect(str(db), timeout=3.0)
            stats["wakes"] = conn.execute("SELECT COUNT(*) FROM wake").fetchone()[0]
            row = conn.execute(
                "SELECT started_at FROM wake ORDER BY wake_seq DESC LIMIT 1"
            ).fetchone()
            if row and row[0]:
                from datetime import datetime

                try:
                    stats["last_wake_at"] = datetime.fromtimestamp(float(row[0])).strftime(
                        "%m-%d %H:%M"
                    )
                except (TypeError, ValueError):
                    stats["last_wake_at"] = str(row[0])[:16]
            conn.close()
        except sqlite3.Error:
            pass
    db = _svc_db(service_id, "state.db")
    if db.exists():
        try:
            conn = sqlite3.connect(str(db), timeout=3.0)
            stats["open_events"] = conn.execute(
                "SELECT COUNT(*) FROM events WHERE consumed_at IS NULL"
                " AND channel LIKE ?",
                (f"instance:{service_id}%",),
            ).fetchone()[0]
            conn.close()
        except sqlite3.Error:
            pass
    db = _svc_db(service_id, "messages.db")
    if db.exists():
        try:
            conn = sqlite3.connect(str(db), timeout=3.0)
            stats["messages"] = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            conn.close()
        except sqlite3.Error:
            pass
    return stats


# ── 服务 ──────────────────────────────────────────────────────────────


async def handle_list_services(_request: web.Request) -> web.Response:
    from infrastructure.persistence import services_registry

    rows = []
    for svc in services_registry.list_services():
        pid = svc.get("project_id") or ""
        project = services_registry.lookup_project(pid) if pid else None
        rows.append({
            "service_id": svc["service_id"],
            "role": svc.get("display_name") or "",
            "agent_def_id": svc["agent_def_id"],
            "project_id": pid,
            "project_name": (project or {}).get("name", ""),
            "status": svc["status"],
            "subscriptions": svc.get("subscriptions") or [],
            "tools": svc.get("tools"),
            "created_at": (svc.get("created_at") or "")[:16],
            **_service_stats(svc["service_id"]),
        })
    return _json({"ok": True, "services": rows})


async def handle_service_detail(request: web.Request) -> web.Response:
    sid = request.match_info["service_id"]
    from infrastructure.persistence import services_registry

    svc = services_registry.lookup_service(sid)
    if svc is None:
        return _json({"ok": False, "error": "服务不存在"}, 404)

    wakes = []
    db = _svc_db(sid, "runtime_log.db")
    if db.exists():
        from datetime import datetime

        conn = sqlite3.connect(str(db), timeout=3.0)
        try:
            rows = conn.execute(
                "SELECT wake_seq, session_id, started_at, ended_at, meta_json"
                " FROM wake ORDER BY wake_seq DESC LIMIT 20"
            ).fetchall()
        finally:
            conn.close()
        for seq, session_id, started, ended, meta in rows:
            try:
                m = json.loads(meta) if meta else {}
            except ValueError:
                m = {}
            def _ts(t):
                try:
                    return datetime.fromtimestamp(float(t)).strftime("%m-%d %H:%M:%S")
                except (TypeError, ValueError):
                    return ""
            wakes.append({
                "seq": seq,
                "session_id": session_id or "",
                "reason": (m.get("reason") or "")[:40],
                "started_at": _ts(started),
                "ended_at": _ts(ended),
                "tokens": m.get("total_tokens") or m.get("tokens"),
            })
    return _json({
        "ok": True,
        "service": {**svc, **_service_stats(sid)},
        "wakes": wakes,
    })


async def handle_service_status(request: web.Request) -> web.Response:
    sid = request.match_info["service_id"]
    body: dict = {}
    try:
        body = await request.json()
    except Exception:
        pass
    status = str(body.get("status") or "").strip()
    if status not in ("active", "archived"):
        return _json({"ok": False, "error": "status 取 active|archived"}, 400)
    from domain.service import activate_service, archive_service

    ok = activate_service(sid) if status == "active" else archive_service(sid)
    return _json({"ok": ok})


# ── 项目 ──────────────────────────────────────────────────────────────


async def handle_list_projects(_request: web.Request) -> web.Response:
    from infrastructure.persistence import services_registry

    rows = []
    for p in services_registry.list_projects():
        members = services_registry.list_services_by_project(p["project_id"])
        todos = services_registry.list_project_todos(p["project_id"])
        rows.append({
            "project_id": p["project_id"],
            "name": p["name"],
            "status": p["status"],
            "pm_id": p.get("pm_id") or "",
            "customer_id": p.get("customer_id") or "",
            "watchdog": bool(p.get("watchdog_enabled")),
            "members": [m.get("display_name") or m["service_id"][:10] for m in members],
            "todos_done": sum(1 for t in todos if t["status"] == "done"),
            "todos_total": len(todos),
            "created_at": (p.get("created_at") or "")[:16],
        })
    return _json({"ok": True, "projects": rows})


async def handle_project_group(request: web.Request) -> web.Request:
    """项目群消息回放（从 PM 库 svcgroup 窗口读双向流）。"""
    pid = request.match_info["project_id"]
    from infrastructure.persistence import services_registry

    project = services_registry.lookup_project(pid)
    if project is None:
        return _json({"ok": False, "error": "项目不存在"}, 404)
    messages = []
    db = _svc_db(project["pm_id"], "messages.db")
    if db.exists():
        conn = sqlite3.connect(str(db), timeout=3.0)
        try:
            rows = conn.execute(
                "SELECT ts, direction, sender_name, text FROM messages"
                " WHERE chat_id = ? ORDER BY id LIMIT 500",
                (f"svcgroup:{pid}",),
            ).fetchall()
        finally:
            conn.close()
        messages = [
            {"ts": ts, "from": "self" if d == "out" else "peer",
             "sender_name": s or "", "text": t or ""}
            for ts, d, s, t in rows
        ]
    return _json({"ok": True, "project_id": pid, "messages": messages})


async def handle_project_files(request: web.Request) -> web.Response:
    """项目共享区文件浏览/读取（锚定 shared/ 内，防越界）。"""
    pid = request.match_info["project_id"]
    rel = request.query.get("path", "").strip()
    from infrastructure.config import get_project_root
    from infrastructure.persistence import services_registry

    if services_registry.lookup_project(pid) is None:
        return _json({"ok": False, "error": "项目不存在"}, 404)
    base = (get_project_root() / "projects" / pid / "shared").resolve()
    target = (base / rel).resolve() if rel else base
    if target != base and base not in target.parents:
        return _json({"ok": False, "error": "越界拒绝"}, 403)
    if not target.exists():
        return _json({"ok": False, "error": "路径不存在"}, 404)
    if target.is_file():
        return _json({
            "ok": True, "kind": "file", "path": rel,
            "content": target.read_text(encoding="utf-8", errors="replace")[:100_000],
        })
    entries = [
        {"name": p.name, "type": "dir" if p.is_dir() else "file",
         "size": p.stat().st_size if p.is_file() else 0,
         "rel": str(p.relative_to(base))}
        for p in sorted(target.iterdir()) if not p.name.startswith(".")
    ]
    return _json({"ok": True, "kind": "dir", "path": rel, "entries": entries})


_ROUTER.router.add_get("/services", handle_list_services)
_ROUTER.router.add_get("/services/{service_id}", handle_service_detail)
_ROUTER.router.add_post("/services/{service_id}/status", handle_service_status)
_ROUTER.router.add_get("/projects", handle_list_projects)
_ROUTER.router.add_get("/projects/{project_id}/group", handle_project_group)
_ROUTER.router.add_get("/projects/{project_id}/files", handle_project_files)


async def handle_project_activity(request: web.Request) -> web.Response:
    """GET /api/admin/projects/{pid}/activity — 转译动态流 + 阶段步骤（C14）。"""
    from domain.project.activity import build_project_activity

    return _json({"ok": True, **build_project_activity(request.match_info["project_id"])})


_ROUTER.router.add_get("/projects/{project_id}/activity", handle_project_activity)


async def _serve_page(_request: web.Request) -> web.Response:
    from infrastructure.config import get_project_root

    page = get_project_root() / "interfaces" / "web" / "services" / "index.html"
    if not page.exists():
        return web.Response(status=404, text="services page missing")
    return web.FileResponse(page)


def add_services_admin_routes(app: web.Application) -> None:
    app.add_subapp("/api/admin/", _ROUTER)
    app.router.add_get("/services", _serve_page)
    logger.info("Services admin routes registered: /services + /api/admin/*")
