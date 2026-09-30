"""服务型协作工具（刀 4b）——服务专属，实例默认不装载（工程可放行）。

工具面按模版角色收紧（services.tools_json，装配层应用）。原语：
  - 社交：sense_project_peers / send_chat_message（群 fan-out·同伴·客户三路由）
  - 文件：project_file_list / read / write（锚定项目目录；写限个人区）
  - 转正：deliver_to_shared（个人区 → shared/，显式交付）
  - 待办：project_todo_create / list / update（可挂 parent 拆解）
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

from infrastructure.config import get_app_instance_id
from interfaces.tools import registry

logger = logging.getLogger(__name__)

_MAX_FILE_CHARS = 200_000


def _current_service_id() -> str:
    return get_app_instance_id() or ""


def _my_project():
    from domain.project.customer import get_project_of_service

    return get_project_of_service(_current_service_id())


# ── 社交 ──────────────────────────────────────────────────────────────


def _handle_sense_project_peers(_args: Dict[str, Any], **_) -> str:
    sid = _current_service_id()
    project = _my_project()
    if not project:
        return registry.tool_error("本服务未挂项目（无协作队友）")
    from infrastructure.persistence import services_registry

    peers = services_registry.list_services_by_project(
        project["project_id"], status="active"
    )
    rows = [
        {
            "service_id": p["service_id"],
            "role": p.get("display_name") or "",
            "is_me": p["service_id"] == sid,
            "is_pm": p["service_id"] == (project.get("pm_id") or ""),
        }
        for p in peers
    ]
    return json.dumps(
        {
            "project_id": project["project_id"],
            "project_name": project["name"],
            "pm_id": project.get("pm_id") or "",
            "group_chat_id": f"svcgroup:{project['project_id']}",
            "peers": rows,
        },
        ensure_ascii=False,
    )


def _handle_send_chat_message(args: Dict[str, Any], **_) -> str:
    sid = _current_service_id()
    if not _my_project():
        return registry.tool_error("本服务未挂项目（无法发消息）")
    chat_id = (args.get("chat_id") or "").strip()
    text = (args.get("text") or "").strip()
    if not chat_id or not text:
        return registry.tool_error("必须传 chat_id 和 text")
    from domain.service.social import send_chat_message

    result = send_chat_message(sid, chat_id, text)
    if not result.get("ok"):
        return registry.tool_error(result.get("error") or "发送失败")
    return json.dumps(result, ensure_ascii=False)


# ── 文件（个人区自由写 / shared 只读 / deliver 转正） ────────────────


def _handle_project_file_list(args: Dict[str, Any], **_) -> str:
    sid = _current_service_id()
    project = _my_project()
    if not project:
        return registry.tool_error("本服务未挂项目（无项目目录）")
    sub = (args.get("subpath") or ".").strip() or "."
    scope = (args.get("scope") or "").strip()  # ""=两者 | personal | shared
    from domain.project.customer import member_personal_dir, project_root_dir, shared_dir

    targets: list[tuple[str, Path]] = []
    root = project_root_dir(project["project_id"])
    if scope in ("", "personal"):
        targets.append(("personal", member_personal_dir(project["project_id"], sid)))
    if scope in ("", "shared"):
        targets.append(("shared", shared_dir(project["project_id"])))
    out = []
    for label, base in targets:
        target = base if sub == "." else base / sub
        if not target.exists():
            continue
        for p in sorted(target.rglob("*"))[:200]:
            rel = str(p.relative_to(root))
            out.append({
                "path": rel,
                "area": label,
                "type": "dir" if p.is_dir() else "file",
                "size": p.stat().st_size if p.is_file() else 0,
            })
    return json.dumps({"entries": out}, ensure_ascii=False)


def _handle_project_file_read(args: Dict[str, Any], **_) -> str:
    sid = _current_service_id()
    if not _my_project():
        return registry.tool_error("本服务未挂项目（无项目目录）")
    relpath = (args.get("path") or "").strip()
    if not relpath:
        return registry.tool_error("必须传 path")
    try:
        from domain.project.customer import resolve_workspace_path

        p = resolve_workspace_path(sid, relpath)
        if not p.is_file():
            return registry.tool_error(f"文件不存在: {relpath}")
        return p.read_text(encoding="utf-8", errors="replace")[:_MAX_FILE_CHARS]
    except PermissionError as exc:
        return registry.tool_error(f"越界拒绝: {exc}")
    except Exception as exc:
        return registry.tool_error(f"读文件失败: {exc}")


def _handle_project_file_write(args: Dict[str, Any], **_) -> str:
    sid = _current_service_id()
    if not _my_project():
        return registry.tool_error("本服务未挂项目（无项目目录）")
    relpath = (args.get("path") or "").strip()
    content = args.get("content")
    if not relpath or content is None:
        return registry.tool_error("必须传 path 和 content")
    try:
        from domain.project.customer import resolve_workspace_path

        p = resolve_workspace_path(sid, relpath, for_write=True)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(content), encoding="utf-8")
        return json.dumps(
            {"written": True, "path": relpath, "chars": len(str(content))},
            ensure_ascii=False,
        )
    except PermissionError as exc:
        return registry.tool_error(f"越界拒绝: {exc}")
    except Exception as exc:
        return registry.tool_error(f"写文件失败: {exc}")


def _handle_deliver_to_shared(args: Dict[str, Any], **_) -> str:
    sid = _current_service_id()
    relpath = (args.get("path") or "").strip()
    if not relpath:
        return registry.tool_error("必须传 path（个人区相对路径）")
    try:
        from domain.project.customer import deliver_file

        result = deliver_file(sid, relpath)
        return json.dumps(result, ensure_ascii=False)
    except (PermissionError, ValueError) as exc:
        return registry.tool_error(str(exc))


# ── 待办 ──────────────────────────────────────────────────────────────


def _handle_project_todo_create(args: Dict[str, Any], **_) -> str:
    sid = _current_service_id()
    project = _my_project()
    if not project:
        return registry.tool_error("本服务未挂项目（无项目待办）")
    title = (args.get("title") or "").strip()
    if not title:
        return registry.tool_error("必须传 title")
    from infrastructure.persistence import services_registry

    assignee = (args.get("assignee_id") or "").strip()
    if assignee:
        valid = {
            p["service_id"] for p in services_registry.list_services_by_project(
                project["project_id"], status="active"
            )
        }
        if assignee not in valid:
            return registry.tool_error(
                f"assignee_id 不在本项目（用 sense_project_peers 查队友）"
            )
    todo = services_registry.create_project_todo(
        project["project_id"], title,
        parent_id=(args.get("parent_todo_id") or "").strip(),
        detail=(args.get("detail") or "").strip(),
        assignee_id=assignee,
        created_by=sid,
    )
    # 任务交接：指派给其他成员时通知对方（未指派/指派自己不通知）
    if assignee and assignee != sid:
        try:
            from domain.service import emit_to_service
            from domain.service.registry import get_service as _get_svc

            peer = _get_svc(assignee) or {}
            emit_to_service(assignee, "todo_assigned", {
                "todo_id": todo.get("todo_id", ""),
                "title": title,
                "detail": (args.get("detail") or "").strip()[:300],
                "assign_role": peer.get("display_name") or "",
                "from_role": project.get("pm_id") == sid and "项目经理" or (services_registry.lookup_service(sid) or {}).get("display_name", ""),
                "project_name": project.get("name", ""),
            })
        except Exception as _exc:  # noqa: BLE001 — 通知失败不拦创建
            pass
    return json.dumps(todo, ensure_ascii=False)


def _handle_project_todo_list(args: Dict[str, Any], **_) -> str:
    sid = _current_service_id()
    project = _my_project()
    if not project:
        return registry.tool_error("本服务未挂项目（无项目待办）")
    from infrastructure.persistence import services_registry

    scope = (args.get("scope") or "open").strip()
    status = None if scope == "all" else (None if scope == "mine" else scope)
    if scope not in ("open", "in_progress", "done", "all", "mine"):
        return registry.tool_error("scope 取值: open / in_progress / done / all / mine")
    assignee = sid if scope == "mine" else None
    if scope == "mine":
        status = None
    todos = services_registry.list_project_todos(
        project["project_id"], status=status, assignee_id=assignee
    )
    return json.dumps(
        [{"todo_id": t["todo_id"], "title": t["title"], "parent_id": t["parent_id"],
          "assignee": t["assignee_id"], "status": t["status"]} for t in todos],
        ensure_ascii=False,
    )


def _handle_project_todo_update(args: Dict[str, Any], **_) -> str:
    sid = _current_service_id()
    project = _my_project()
    if not project:
        return registry.tool_error("本服务未挂项目（无项目待办）")
    todo_id = (args.get("todo_id") or "").strip()
    if not todo_id:
        return registry.tool_error("必须传 todo_id")
    from infrastructure.persistence import services_registry

    todo = services_registry.get_project_todo(todo_id)
    if not todo or todo["project_id"] != project["project_id"]:
        return registry.tool_error(f"待办不在本项目: {todo_id}")
    status = (args.get("status") or "").strip() or None
    if status and status not in ("open", "in_progress", "done"):
        return registry.tool_error("status 取值: open / in_progress / done")
    ok = services_registry.update_project_todo(
        todo_id,
        title=(args.get("title") or "").strip() or None,
        detail=(args.get("detail") or "").strip() or None,
        assignee_id=(args.get("assignee_id") or "").strip() or None,
        status=status,
        actor=sid,
    )
    return json.dumps({"updated": ok, "todo_id": todo_id}, ensure_ascii=False)


# ── 注册 ──────────────────────────────────────────────────────────────


def _reg(name: str, description: str, params: dict | None = None, required: list | None = None, emoji: str = "🔧") -> None:
    schema = {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": params or {},
            "required": required or [],
        },
    }
    registry.register(name=name, toolset="actions", schema=schema,
                      handler=globals()[f"_handle_{name}"], emoji=emoji)


_reg(
    "sense_project_peers",
    "查看本项目：项目名、PM、群 chat_id、全部角色服务（含自己）。发消息/派待办前先调这个。",
    emoji="👥",
)
_reg(
    "send_chat_message",
    "发消息到指定窗口。chat_id 三种：项目群（svcgroup:开头，全体成员可见）、"
    "同伴私聊（对方的 service_id）、客户私聊（customer:开头，仅 PM 有）。"
    "群消息会唤醒其他成员。",
    params={
        "chat_id": {"type": "string", "description": "目标窗口 ID"},
        "text": {"type": "string", "description": "消息内容"},
    },
    required=["chat_id", "text"],
    emoji="📤",
)
_reg(
    "project_file_list",
    "列项目文件。scope: personal（我的个人区）/ shared（共享区）/ 空=两者。"
    "返回的 path 可直接用于 read/write/deliver。",
    params={"scope": {"type": "string", "description": "personal | shared | 空=两者"},
            "subpath": {"type": "string", "description": "子目录（默认全部）"}},
    emoji="📂",
)
_reg(
    "project_file_read",
    "读项目文件。可读自己的个人区（members/我/…）与共享区（shared/…）；不能读他人个人区。",
    params={"path": {"type": "string", "description": "list 返回的 path"}},
    required=["path"],
    emoji="📄",
)
_reg(
    "project_file_write",
    "写文件到自己的个人区（草稿自由写）。path 相对项目根，形如 members/<我的id>/xxx.md；"
    "通常只需写相对个人区的名字。写共享区必须走 deliver_to_shared 转正。",
    params={"path": {"type": "string", "description": "文件路径"},
            "content": {"type": "string", "description": "文件全文"}},
    required=["path", "content"],
    emoji="✍️",
)
_reg(
    "deliver_to_shared",
    "把个人区的文件转正到共享区（显式交付动作，全员可读；shared 存最新、.versions 留历史）。"
    "path 为个人区内相对路径。",
    params={"path": {"type": "string", "description": "个人区内相对路径"}},
    required=["path"],
    emoji="📦",
)
_reg(
    "project_todo_create",
    "建项目待办（可挂 parent_todo_id 拆解子待办、可 assignee_id 派给队友）。",
    params={
        "title": {"type": "string", "description": "待办标题"},
        "detail": {"type": "string", "description": "说明"},
        "parent_todo_id": {"type": "string", "description": "父待办 ID（拆解用）"},
        "assignee_id": {"type": "string", "description": "承办服务 ID（sense_project_peers 查）"},
    },
    required=["title"],
    emoji="☑️",
)
_reg(
    "project_todo_list",
    "列项目待办。scope: open / in_progress / done / all / mine（我的全部）。",
    params={"scope": {"type": "string", "description": "open|in_progress|done|all|mine"}},
    emoji="📋",
)
_reg(
    "project_todo_update",
    "更新待办：改状态（open/in_progress/done）、改派单、补说明。",
    params={
        "todo_id": {"type": "string", "description": "待办 ID"},
        "status": {"type": "string", "description": "open | in_progress | done"},
        "assignee_id": {"type": "string", "description": "改派给谁"},
        "title": {"type": "string", "description": "改标题"},
        "detail": {"type": "string", "description": "补说明"},
    },
    required=["todo_id"],
    emoji="🔄",
)
