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
    resp = web.json_response(data, status=status)
    # 管理/回放数据随库实时变，禁止浏览器启发式缓存旧响应
    resp.headers["Cache-Control"] = "no-store"
    return resp


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
        rows.append({
            "service_id": svc["service_id"],
            "role": svc.get("display_name") or "",
            "agent_def_id": svc["agent_def_id"],
            "project_id": svc.get("project_id") or "",
            "project_name": "",
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
        # role 与列表端点对齐（= display_name）；前端两处标题都依赖它
        "service": {**svc, "role": svc.get("display_name") or "", **_service_stats(sid)},
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


async def handle_list_defs(_request: web.Request) -> web.Response:
    """GET /api/admin/defs — 可用的 agent 定义目录（runtime_kind: definition）。"""
    import yaml as _yaml

    from infrastructure.config import get_project_root

    defs = []
    apps = get_project_root() / "apps"
    if apps.is_dir():
        for d in sorted(apps.iterdir()):
            cfg = d / "config" / "app.yaml"
            if not cfg.exists():
                continue
            try:
                data = _yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            if str(data.get("runtime_kind") or "") == "definition":
                defs.append({"def_id": d.name, "display_name": data.get("display_name") or d.name})
    return _json({"ok": True, "defs": defs})


async def handle_service_config(request: web.Request) -> web.Response:
    """PATCH /api/admin/services/{sid}/config — agent 详细配置编辑。"""
    sid = request.match_info["service_id"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    from infrastructure.persistence import services_registry

    svc = services_registry.lookup_service(sid)
    if svc is None:
        return _json({"ok": False, "error": "服务不存在"}, 404)
    kw = {}
    if "display_name" in body:
        # 空名是清空语义之外的误操作（前端表单读不到旧值时会提交 ''）——不覆盖
        name = str(body["display_name"]).strip()[:30]
        if name:
            kw["display_name"] = name
    if "subscriptions" in body and isinstance(body["subscriptions"], list):
        kw["subscriptions"] = [str(x) for x in body["subscriptions"]][:20]
    if "tools" in body and isinstance(body["tools"], list):
        kw["tools"] = [str(x) for x in body["tools"]][:40]
    if "capabilities" in body and isinstance(body["capabilities"], dict):
        caps = dict(svc.get("capabilities") or {})
        caps.update({k: bool(v) for k, v in body["capabilities"].items()})
        kw["capabilities"] = caps
    if not kw:
        return _json({"ok": False, "error": "无可更新字段"}, 400)
    services_registry.update_service_fields(sid, **kw)
    logger.info("SERVICE_CONFIG_UPDATED %s fields=%s", sid[:12], list(kw))
    return _json({"ok": True, "service": services_registry.lookup_service(sid)})


_ROUTER.router.add_get("/services", handle_list_services)
_ROUTER.router.add_get("/services/{service_id}", handle_service_detail)
_ROUTER.router.add_post("/services/{service_id}/status", handle_service_status)
_ROUTER.router.add_get("/defs", handle_list_defs)
_ROUTER.router.add_patch("/services/{service_id}/config", handle_service_config)


def add_services_admin_routes(app: web.Application) -> None:
    # agent 深度配置路由并进同一 _ROUTER——须在 add_subapp（freeze）之前；
    # 业务管理路由（模版/项目端点）同样经扩展点在冻结前注入。
    from application.api.agent_admin_routes import register_into

    register_into(_ROUTER)
    from infrastructure import extension

    extension.mount_admin_routers(_ROUTER)
    app.add_subapp("/api/admin/", _ROUTER)
    logger.info("Services admin routes registered: /api/admin/*")


def collect_services_overview() -> dict:
    """服务型 agent 总览摘要（供 system overview 聚合）。

    三层结构：def 清单（含服务实例计数）→ active 服务行（含运行统计）
    → 计数汇总。def 目录扫描与 services_registry 均为只读，失败返回
    空摘要而非抛错——不拖垮 overview 主数据。
    """
    from infrastructure.persistence import services_registry

    # def 清单（与 handle_list_defs 同源逻辑）
    def_rows: list[dict] = []
    try:
        import yaml as _yaml

        from infrastructure.config import get_project_root

        apps = get_project_root() / "apps"
        if apps.is_dir():
            for d in sorted(apps.iterdir()):
                cfg = d / "config" / "app.yaml"
                if not cfg.exists():
                    continue
                try:
                    data = _yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
                except Exception:
                    continue
                if str(data.get("runtime_kind") or "") == "definition":
                    def_rows.append(
                        {"def_id": d.name, "display_name": data.get("display_name") or d.name}
                    )
    except Exception:
        logger.exception("services overview: scan defs failed")

    # 服务实例（含运行统计）
    svc_rows: list[dict] = []
    try:
        for svc in services_registry.list_services():
            svc_rows.append(
                {
                    "service_id": svc["service_id"],
                    "agent_def_id": svc["agent_def_id"],
                    "role": svc.get("display_name") or "",
                    "project_id": svc.get("project_id") or "",
                    "project_name": "",
                    "status": svc["status"],
                    **_service_stats(svc["service_id"]),
                }
            )
    except Exception:
        logger.exception("services overview: list services failed")

    by_def: dict[str, dict] = {}
    for s in svc_rows:
        g = by_def.setdefault(
            s["agent_def_id"], {"service_count": 0, "active_count": 0}
        )
        g["service_count"] += 1
        if s["status"] == "active":
            g["active_count"] += 1
    for d in def_rows:
        d.update(by_def.get(d["def_id"], {"service_count": 0, "active_count": 0}))

    # 业务 enricher（项目名等）经扩展点补全——基座不知道"项目"是什么
    try:
        from infrastructure import extension

        extension.enrich_service_rows(svc_rows)
    except Exception:
        pass
    active = [s for s in svc_rows if s["status"] == "active"]
    last_wake = max(
        (s["last_wake_at"] for s in svc_rows if s.get("last_wake_at")), default=None
    )
    return {
        "defs": def_rows,
        "def_count": len(def_rows),
        "services": active,
        "service_count": len(active),
        "open_events_total": sum(s.get("open_events") or 0 for s in active),
        "last_activity": last_wake,
    }
