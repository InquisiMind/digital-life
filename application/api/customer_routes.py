"""客户会话 API — web 客户通道（刀 3 最小闭环）。

产品流（2026-09-29 定稿）："客户点 AI 战略咨询入口 → 发言即自动建项目
（按模版批量建服务+社交圈）→ 首条消息投 PM"。本模块是 web 入口的 HTTP 壳，
内部与 CLI project-start 调同一 domain API。

投递闭环：客户→PM 走 customer_message_to_pm（事件唤醒）；PM→客户的回复
本来就落在 PM 的 messages.db 客户窗口（chat_id=customer:{cid}），GET 直接
回放该窗口双向消息——不引入 websocket，轮询即可（demo 形态）。

鉴权（设计文档 6.1）：demo 最简版——customer_id 即令牌；接口形态按
"账号-项目-区"三级留位（session/message 两级已分层）。
"""

from __future__ import annotations

import logging
import sqlite3
import uuid

from aiohttp import web

logger = logging.getLogger("digital_life.api.customer")

_ROUTER = web.Application()


def _json(data, status: int = 200) -> web.Response:
    return web.json_response(data, status=status)


async def _body(request: web.Request) -> dict:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# ── 会话：发言即建项目（S00） ─────────────────────────────────────────


async def handle_start_session(request: web.Request) -> web.Response:
    """POST /api/customer/sessions {customer_name, text, template?}

    无 active 项目的客户 → 按模版建项目（社交圈+待办骨架）→ 首条消息投 PM。
    已有项目 → 直接投 PM（幂等入口，前端也可以只调这一个）。
    """
    body = await _body(request)
    customer_name = str(body.get("customer_name") or "").strip() or "客户"
    text = str(body.get("text") or "").strip()
    template = str(body.get("template") or "ai_strategy").strip()
    if not text:
        return _json({"ok": False, "error": "text 必填"}, 400)

    from domain.project.customer import create_project_from_template
    from domain.service.social import customer_message_to_pm
    from infrastructure.persistence import services_registry

    # 已有该客户的 active 项目 → 直接投 PM（幂等）
    customer_id = str(body.get("customer_id") or "").strip()
    if customer_id:
        for p in services_registry.list_projects(status="active"):
            if p.get("customer_id") == customer_id:
                eid = customer_message_to_pm(
                    p["pm_id"], {"id": customer_id, "name": customer_name}, text
                )
                return _json({
                    "ok": True, "customer_id": customer_id,
                    "project_id": p["project_id"], "event_id": eid,
                    "created": False,
                })

    # 新客户 → S00：建项目 + 投首条消息
    customer_id = customer_id or f"cust-{uuid.uuid4().hex[:10]}"
    try:
        result = create_project_from_template(
            name=f"{customer_name}的战略咨询",
            template_id=template,
            customer={"id": customer_id, "name": customer_name},
        )
    except ValueError as exc:
        return _json({"ok": False, "error": str(exc)}, 400)
    eid = customer_message_to_pm(
        result["pm_service_id"], {"id": customer_id, "name": customer_name}, text
    )
    return _json({
        "ok": True, "customer_id": customer_id,
        "project_id": result["project"]["project_id"],
        "event_id": eid, "created": True,
    })


# ── 对话回放 ──────────────────────────────────────────────────────────


def _pm_messages_db(pm_service_id: str):
    from infrastructure.config import resolve_runtime_dir

    return resolve_runtime_dir(pm_service_id) / "data" / "messages.db"


async def handle_get_messages(request: web.Request) -> web.Response:
    """GET /api/customer/sessions/{customer_id}/messages

    回放 PM 库中 customer:{cid} 窗口的双向消息（in=客户发言，out=PM 回复）。
    """
    customer_id = request.match_info["customer_id"]
    from infrastructure.persistence import services_registry

    project = None
    for p in services_registry.list_projects(status="active"):
        if p.get("customer_id") == customer_id:
            project = p
            break
    if project is None:
        return _json({"ok": False, "error": "会话不存在"}, 404)

    db = _pm_messages_db(project["pm_id"])
    messages: list[dict] = []
    if db.exists():
        conn = sqlite3.connect(str(db), timeout=3.0)
        try:
            rows = conn.execute(
                "SELECT ts, direction, sender_name, text FROM messages"
                " WHERE chat_id = ? ORDER BY id",
                (f"customer:{customer_id}",),
            ).fetchall()
        finally:
            conn.close()
        for ts, direction, sender, text in rows:
            messages.append({
                "ts": ts,
                "from": "customer" if direction == "in" else "consultant",
                "sender_name": sender or "",
                "text": text or "",
            })
    return _json({
        "ok": True,
        "customer_id": customer_id,
        "project_id": project["project_id"],
        "project_name": project["name"],
        "messages": messages,
    })


async def handle_list_sessions(request: web.Request) -> web.Response:
    """GET /api/customer/sessions?customer_id=… → 客户的项目列表（demo 管理用）。"""
    from infrastructure.persistence import services_registry

    rows = []
    for p in services_registry.list_projects():
        rows.append({
            "project_id": p["project_id"],
            "name": p["name"],
            "status": p["status"],
            "customer_id": p.get("customer_id") or "",
            "created_at": p["created_at"],
        })
    return _json({"ok": True, "projects": rows})


_ROUTER.router.add_post("/sessions", handle_start_session)
_ROUTER.router.add_get("/sessions", handle_list_sessions)
_ROUTER.router.add_get("/sessions/{customer_id}/messages", handle_get_messages)


async def _serve_page(_request: web.Request) -> web.Response:
    from infrastructure.config import get_project_root

    page = get_project_root() / "interfaces" / "web" / "customer" / "index.html"
    if not page.exists():
        return web.Response(status=404, text="customer page missing")
    return web.FileResponse(page)


def add_customer_routes(app: web.Application) -> None:
    app.add_subapp("/api/customer/", _ROUTER)
    app.router.add_get("/customer", _serve_page)
    logger.info("Customer chat routes registered: /customer + /api/customer/*")
