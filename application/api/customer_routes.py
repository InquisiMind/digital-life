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

import json
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

    回放 PM 库中 customer:{cid} 窗口的双向消息（in=客户发言，out=PM 回复），
    附项目进展（团队成员 + 待办状态）供前端进展栏展示。
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

    # 项目进展：团队 + 待办（角色名映射，供客户侧展示）
    sid_to_role = {}
    team = []
    for svc in services_registry.list_services_by_project(
        project["project_id"], status="active"
    ):
        sid_to_role[svc["service_id"]] = svc.get("display_name") or ""
        team.append({
            "role": svc.get("display_name") or "",
            "sid": svc["service_id"],
            "is_pm": svc["service_id"] == project.get("pm_id"),
        })
    todos = [
        {
            "title": t["title"],
            "status": t["status"],
            "assignee": sid_to_role.get(t.get("assignee_id") or "", ""),
        }
        for t in services_registry.list_project_todos(project["project_id"])
    ]
    stage = "已完成" if todos and all(t["status"] == "done" for t in todos) else (
        "研究中" if todos else "受理中"
    )
    return _json({
        "ok": True,
        "customer_id": customer_id,
        "project_id": project["project_id"],
        "project_name": project["name"],
        "stage": stage,
        "team": team,
        "todos": todos,
        "messages": messages,
    })


async def handle_activity(request: web.Request) -> web.Response:
    """GET /api/customer/sessions/{cid}/activity — 过程动态（转译层 C14）+ 阶段步骤。"""
    customer_id = request.match_info["customer_id"]
    project = _find_project(customer_id, request.query.get("project_id", ""))
    if project is None:
        return _json({"ok": False, "error": "会话不存在"}, 404)
    from domain.project.activity import build_project_activity

    data = build_project_activity(project["project_id"])
    return _json({"ok": True, "project_id": project["project_id"], **data})


async def handle_list_sessions(request: web.Request) -> web.Response:
    """GET /api/customer/sessions?customer_id=… → 该客户的项目列表（多委托切换 C2）。"""
    from infrastructure.persistence import services_registry

    want = request.query.get("customer_id", "").strip()
    rows = []
    for p in services_registry.list_projects():
        if want and p.get("customer_id") != want:
            continue
        rows.append({
            "project_id": p["project_id"],
            "name": p["name"],
            "status": p["status"],
            "customer_id": p.get("customer_id") or "",
            "created_at": p["created_at"],
        })
    return _json({"ok": True, "projects": rows})


async def handle_team(request: web.Request) -> web.Response:
    """GET /api/customer/sessions/{cid}/team — 项目群沟通回放（过程透明选项）。"""
    customer_id = request.match_info["customer_id"]
    from infrastructure.persistence import services_registry

    project = _find_project(customer_id, request.query.get("project_id", ""))
    if project is None:
        return _json({"ok": False, "error": "会话不存在"}, 404)
    messages = []
    db = _pm_messages_db(project["pm_id"]) 
    if db.exists():
        conn = sqlite3.connect(str(db), timeout=3.0)
        try:
            rows = conn.execute(
                "SELECT ts, direction, sender_name, text FROM messages"
                " WHERE chat_id = ? ORDER BY id LIMIT 500",
                (f"svcgroup:{project['project_id']}",),
            ).fetchall()
        finally:
            conn.close()
        from datetime import datetime

        def _fmt(ts):
            try:
                return datetime.fromtimestamp(float(ts)).strftime("%m-%d %H:%M")
            except (TypeError, ValueError):
                return ""
        messages = [
            {"ts": _fmt(ts), "from": "self" if d == "out" else "peer",
             "sender_name": s or "", "text": t or ""}
            for ts, d, s, t in rows
        ]
    return _json({"ok": True, "messages": messages})


async def handle_files(request: web.Request) -> web.Response:
    """GET /api/customer/sessions/{cid}/files?path= — 交付物浏览/读取（只读 shared）。"""
    customer_id = request.match_info["customer_id"]
    rel = request.query.get("path", "").strip()
    from infrastructure.config import get_project_root

    project = _find_project(customer_id, request.query.get("project_id", ""))
    if project is None:
        return _json({"ok": False, "error": "会话不存在"}, 404)
    base = (get_project_root() / "projects" / project["project_id"] / "shared").resolve()
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


_TRACE_STEP_CAP = 220


def _member_trace(sid: str, limit_wakes: int = 3) -> list[dict]:
    """成员最近 N 次唤醒的执行轨迹：唤醒上下文 → 思考/工具调用/工具结果/发言 → 休息。

    数据源即审计真相（该服务私有库的 wake × session messages），不另建结构。
    """
    from infrastructure.config import resolve_runtime_dir

    out: list[dict] = []
    rdb = resolve_runtime_dir(sid) / "data" / "runtime_log.db"
    if not rdb.exists():
        return out
    from datetime import datetime

    conn = sqlite3.connect(str(rdb), timeout=3.0)
    try:
        wakes = conn.execute(
            "SELECT wake_seq, session_id, started_at, meta_json FROM wake"
            " ORDER BY wake_seq DESC LIMIT ?",
            (limit_wakes,),
        ).fetchall()
    finally:
        conn.close()

    sdb = resolve_runtime_dir(sid) / "data" / "state.db"
    for seq, session_id, started, meta in wakes:
        try:
            m = json.loads(meta) if meta else {}
        except ValueError:
            m = {}
        try:
            at = datetime.fromtimestamp(float(started)).strftime("%m-%d %H:%M")
        except (TypeError, ValueError):
            at = ""
        trace: dict = {
            "seq": seq, "reason": (m.get("reason") or "")[:30], "at": at,
            "context": "", "steps": [],
        }
        if session_id and sdb.exists():
            c2 = sqlite3.connect(str(sdb), timeout=3.0)
            try:
                rows = c2.execute(
                    "SELECT role, content, tool_calls, tool_name, reasoning FROM messages"
                    " WHERE session_id = ? ORDER BY id",
                    (session_id,),
                ).fetchall()
            finally:
                c2.close()
            for role, content, tool_calls, tool_name, reasoning in rows:
                content = content or ""
                if role == "user":
                    trace["context"] = content[:400]
                elif role == "assistant":
                    if reasoning:
                        trace["steps"].append({"kind": "think", "text": reasoning[:_TRACE_STEP_CAP]})
                    if tool_calls:
                        try:
                            calls = json.loads(tool_calls)
                            names = "、".join(
                                (c.get("function", {}) or {}).get("name", "?") for c in calls
                            ) or (tool_name or "工具")
                        except (ValueError, TypeError):
                            names = tool_name or "工具"
                        trace["steps"].append({"kind": "tool", "text": f"调用 {names}"})
                    if content.strip():
                        trace["steps"].append({"kind": "say", "text": content[:_TRACE_STEP_CAP]})
                elif role == "tool":
                    if "__l4_block__" in content:
                        trace["steps"].append({"kind": "rest", "text": "进入休息，等待下次唤醒"})
                    else:
                        trace["steps"].append({"kind": "result", "text": content[:_TRACE_STEP_CAP]})
            trace["steps"] = trace["steps"][:60]
        out.append(trace)
    return out


async def handle_member_detail(request: web.Request) -> web.Response:
    """GET /api/customer/sessions/{cid}/members/{sid} — 成员明细 + 最近唤醒执行轨迹。"""
    customer_id = request.match_info["customer_id"]
    sid = request.match_info["service_id"]
    project = _find_project(customer_id, request.query.get("project_id", ""))
    if project is None:
        return _json({"ok": False, "error": "会话不存在"}, 404)
    from infrastructure.persistence import services_registry

    members = services_registry.list_services_by_project(
        project["project_id"], status="active"
    )
    if sid not in {m["service_id"] for m in members}:
        return _json({"ok": False, "error": "成员不在本项目"}, 403)

    svc = services_registry.lookup_service(sid) or {}
    traces = _member_trace(sid)
    return _json({
        "ok": True,
        "role": svc.get("display_name") or "",
        "service_id": sid,
        "wakes": [{"seq": t["seq"], "reason": t["reason"], "at": t["at"]} for t in traces],
        "traces": traces,
    })


async def handle_attachment(request: web.Request) -> web.Response:
    """POST /api/customer/sessions/{cid}/attachments（multipart file）

    客户上传材料 → 存项目 shared/需求附件/ → 以一条消息通知 PM（含文件名）。
    """
    customer_id = request.match_info["customer_id"]
    project = _find_project(customer_id, request.query.get("project_id", ""))
    if project is None:
        return _json({"ok": False, "error": "会话不存在"}, 404)
    reader = await request.multipart()
    field = await reader.next()
    if field is None or field.name != "file":
        return _json({"ok": False, "error": "multipart 字段名须为 file"}, 400)
    import re as _re
    import uuid as _uuid

    raw_name = field.filename or "材料"
    safe = _re.sub(r"[^\w\u4e00-\u9fa5.-]+", "_", raw_name)[:60] or "材料"
    fname = f"{_uuid.uuid4().hex[:6]}_{safe}"
    from infrastructure.config import get_project_root

    dst = get_project_root() / "projects" / project["project_id"] / "shared" / "需求附件" / fname
    dst.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    with open(dst, "wb") as fh:
        while True:
            chunk = await field.read_chunk(64 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > 20 * 1024 * 1024:
                return _json({"ok": False, "error": "文件超过 20MB 限制"}, 413)
            fh.write(chunk)
    from domain.service.social import customer_message_to_pm

    eid = customer_message_to_pm(
        project["pm_id"], {"id": customer_id, "name": "客户"},
        f"我上传了材料《{raw_name}》（{size/1000:.0f} KB），已存入项目需求附件，请在分析中使用。",
    )
    return _json({"ok": True, "file": raw_name, "path": f"需求附件/{fname}", "size": size,
                  "event_id": eid})


def _find_project(customer_id: str, project_id: str = ""):
    """客户的当前项目；带 project_id 时可选中已完成项目（多委托切换 C2）。"""
    from infrastructure.persistence import services_registry

    if project_id:
        p = services_registry.lookup_project(project_id)
        if p and p.get("customer_id") == customer_id:
            return p
        return None
    for p in services_registry.list_projects(status="active"):
        if p.get("customer_id") == customer_id:
            return p
    # 无 active 时回退最近一个（看历史）
    rows = [p for p in services_registry.list_projects() if p.get("customer_id") == customer_id]
    return rows[-1] if rows else None


_ROUTER.router.add_post("/sessions", handle_start_session)
_ROUTER.router.add_get("/sessions", handle_list_sessions)
_ROUTER.router.add_get("/sessions/{customer_id}/messages", handle_get_messages)
_ROUTER.router.add_get("/sessions/{customer_id}/team", handle_team)
_ROUTER.router.add_get("/sessions/{customer_id}/files", handle_files)
_ROUTER.router.add_get(
    "/sessions/{customer_id}/members/{service_id}", handle_member_detail
)
_ROUTER.router.add_get("/sessions/{customer_id}/activity", handle_activity)
_ROUTER.router.add_post("/sessions/{customer_id}/attachments", handle_attachment)


async def _serve_page(_request: web.Request) -> web.Response:
    from infrastructure.config import get_project_root

    page = get_project_root() / "interfaces" / "web" / "customer" / "index.html"
    if not page.exists():
        return web.Response(status=404, text="customer page missing")
    resp = web.FileResponse(page)
    resp.headers["Cache-Control"] = "no-cache"  # 内联脚本的零构建页：旧缓存会让用户看不到新版
    return resp


def add_customer_routes(app: web.Application) -> None:
    app.add_subapp("/api/customer/", _ROUTER)
    app.router.add_get("/customer", _serve_page)
    logger.info("Customer chat routes registered: /customer + /api/customer/*")
