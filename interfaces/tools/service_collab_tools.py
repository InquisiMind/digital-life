"""服务型协作工具（刀 4）——服务专属，实例不装载。

三个协作原语（设计文档特性 6/8）：
  - sense_project_peers：同项目服务清单（我是谁、队友是谁）
  - send_to_peer：给同项目某服务发协作消息——走对方事件队列（不偷看对方
    私有记忆；共享的是项目工作区文件，不是对话上下文）
  - project_file_list / read / write：项目共享工作区文件操作——全部过
    resolve_workspace_path 锚定，越界（含 symlink 逃逸）直接拒绝

实例型不装载这组工具（见 domain/service/capabilities.SERVICE_ONLY_TOOLS）：
实例间协作走 broadcast 对等链，语义不同。
"""

from __future__ import annotations

import json
import logging
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
            "status": p["status"],
        }
        for p in peers
    ]
    return json.dumps(
        {"project_id": project["project_id"], "project_name": project["name"],
         "stage": project.get("stage") or "", "peers": rows},
        ensure_ascii=False,
    )


def _handle_send_to_peer(args: Dict[str, Any], **_) -> str:
    sid = _current_service_id()
    project = _my_project()
    if not project:
        return registry.tool_error("本服务未挂项目（无法发协作消息）")
    peer_id = (args.get("peer_service_id") or "").strip()
    text = (args.get("text") or "").strip()
    if not peer_id or not text:
        return registry.tool_error("必须传 peer_service_id 和 text")
    if peer_id == sid:
        return registry.tool_error("不能给自己发协作消息")

    from infrastructure.persistence import services_registry

    peers = services_registry.list_services_by_project(
        project["project_id"], status="active"
    )
    if peer_id not in {p["service_id"] for p in peers}:
        return registry.tool_error(
            f"目标服务不在本项目 {project['project_id']} 内（用 sense_project_peers 查队友）"
        )

    from domain.service import emit_to_service

    me = services_registry.lookup_service(sid) or {}
    event_id = emit_to_service(
        peer_id,
        "message",
        {
            "text": text,
            "from_service_id": sid,
            "from_role": me.get("display_name") or "",
            "project_id": project["project_id"],
            "collab": True,
        },
    )
    if not event_id:
        return registry.tool_error(
            "协作消息被拒（对方归档或未订阅 message）——event 未入队"
        )
    return json.dumps({"sent": True, "event_id": event_id, "peer": peer_id},
                      ensure_ascii=False)


def _handle_project_file_list(args: Dict[str, Any], **_) -> str:
    sid = _current_service_id()
    if not _my_project():
        return registry.tool_error("本服务未挂项目（无共享工作区）")
    try:
        from domain.project.customer import resolve_workspace_path

        base = resolve_workspace_path(sid, ".")
        sub = (args.get("subpath") or ".").strip() or "."
        target = resolve_workspace_path(sid, sub)
        if not target.exists():
            return registry.tool_error(f"路径不存在: {sub}")
        entries = []
        for p in sorted(target.iterdir()):
            entries.append({
                "name": p.name,
                "type": "dir" if p.is_dir() else "file",
                "size": p.stat().st_size if p.is_file() else 0,
            })
        return json.dumps({"path": sub, "entries": entries}, ensure_ascii=False)
    except Exception as exc:
        return registry.tool_error(f"列目录失败: {exc}")


def _handle_project_file_read(args: Dict[str, Any], **_) -> str:
    sid = _current_service_id()
    if not _my_project():
        return registry.tool_error("本服务未挂项目（无共享工作区）")
    relpath = (args.get("path") or "").strip()
    if not relpath:
        return registry.tool_error("必须传 path（工作区相对路径）")
    try:
        from domain.project.customer import resolve_workspace_path

        p = resolve_workspace_path(sid, relpath)
        if not p.is_file():
            return registry.tool_error(f"文件不存在: {relpath}")
        text = p.read_text(encoding="utf-8", errors="replace")[:_MAX_FILE_CHARS]
        return text
    except Exception as exc:
        return registry.tool_error(f"读文件失败: {exc}")


def _handle_project_file_write(args: Dict[str, Any], **_) -> str:
    sid = _current_service_id()
    if not _my_project():
        return registry.tool_error("本服务未挂项目（无共享工作区）")
    relpath = (args.get("path") or "").strip()
    content = args.get("content")
    if not relpath or content is None:
        return registry.tool_error("必须传 path 和 content")
    try:
        from domain.project.customer import resolve_workspace_path

        p = resolve_workspace_path(sid, relpath)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(content), encoding="utf-8")
        return json.dumps({"written": True, "path": relpath, "chars": len(str(content))},
                          ensure_ascii=False)
    except PermissionError as exc:
        return registry.tool_error(f"越界拒绝: {exc}")
    except Exception as exc:
        return registry.tool_error(f"写文件失败: {exc}")


# ── 注册 ──────────────────────────────────────────────────────────────

registry.register(
    name="sense_project_peers",
    toolset="actions",
    schema={
        "name": "sense_project_peers",
        "description": (
            "查看本项目队友：项目名、当前阶段、全部角色服务（含自己）。"
            "发协作消息前先调这个拿 peer_service_id。"
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    handler=_handle_sense_project_peers,
    emoji="👥",
)

registry.register(
    name="send_to_peer",
    toolset="actions",
    schema={
        "name": "send_to_peer",
        "description": (
            "给同项目另一个服务发协作消息（进对方事件队列，对方会被唤醒处理）。\n"
            "适合：请队友做一件事、交付物交接、阶段流转通知。\n"
            "注意：这是唯一的服务间通信方式——不要试图读对方私有记忆，"
            "共享资料放项目工作区文件（project_file_write）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "peer_service_id": {"type": "string", "description": "目标服务 ID（sense_project_peers 查）"},
                "text": {"type": "string", "description": "协作消息内容（对方醒来直接看到）"},
            },
            "required": ["peer_service_id", "text"],
        },
    },
    handler=_handle_send_to_peer,
    emoji="📤",
)

registry.register(
    name="project_file_list",
    toolset="actions",
    schema={
        "name": "project_file_list",
        "description": "列出项目共享工作区的文件/目录（可选 subpath 子目录）。",
        "parameters": {
            "type": "object",
            "properties": {"subpath": {"type": "string", "default": "."}},
            "required": [],
        },
    },
    handler=_handle_project_file_list,
    emoji="📂",
)

registry.register(
    name="project_file_read",
    toolset="actions",
    schema={
        "name": "project_file_read",
        "description": "读项目共享工作区里的文件（客户资料、队友交付物）。path 为工作区相对路径。",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "工作区相对路径"}},
            "required": ["path"],
        },
    },
    handler=_handle_project_file_read,
    emoji="📄",
)

registry.register(
    name="project_file_write",
    toolset="actions",
    schema={
        "name": "project_file_write",
        "description": (
            "写文件到项目共享工作区（客户资料、研究底稿、阶段交付物）。"
            "项目内所有服务可见。路径被锚定，越界（含 symlink 逃逸）会被拒绝。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "工作区相对路径"},
                "content": {"type": "string", "description": "文件全文"},
            },
            "required": ["path", "content"],
        },
    },
    handler=_handle_project_file_write,
    emoji="✍️",
)
