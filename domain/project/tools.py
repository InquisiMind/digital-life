"""Project tools registration.

Registers: sense_projects, sense_project_detail, sense_project_todos,
           project_todo, project_deliver, project_info.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict

logger = logging.getLogger("digital_life.domain.project")


def _j(obj):
    return json.dumps(obj, ensure_ascii=False, default=str)


def _get_my_instance_id() -> str:
    try:
        from infrastructure.config import get_app_instance_id
        return get_app_instance_id()
    except Exception:
        return ""


def register_project_tools(
    *,
    registry: Any | None = None,
    consume_energy: Callable[[float], Any] | None = None,
) -> None:
    if registry is None:
        from interfaces.tools.registry import registry as _registry
        registry = _registry

    _consume = consume_energy or (lambda amount: None)

    # ── sense_projects ──
    def _handle_sense_projects(args: Dict[str, Any], **_) -> str:
        try:
            from .loader import load_all_projects, list_project_ids
            my_iid = _get_my_instance_id()
            all_pids = list_project_ids()
            all_cfgs = load_all_projects()

            lines = []
            for pid in all_pids:
                cfg = all_cfgs.get(pid)
                if not cfg:
                    continue
                pos = cfg.get_position_for_instance(my_iid)
                pos_name = pos.name if pos else "未参与"
                is_manager = cfg.manager == my_iid
                tag = " [项目经理]" if is_manager else ""
                lines.append(f"- **{cfg.name}** ({cfg.status}) — 岗位: {pos_name}{tag}")
                lines.append(f"  - {cfg.description}" if cfg.description else "  - 暂无描述")
                members = []
                for p in cfg.positions:
                    for a in p.assignees:
                        members.append(f"{a}({p.name})")
                lines.append(f"  - 成员: {', '.join(members)}")
            if not lines:
                return _j({"ok": True, "projects": [], "message": "暂无参与的项目"})
            return _j({"ok": True, "projects": all_pids, "overview": "\n".join(lines)})
        except Exception as e:
            return _j({"ok": False, "reason": str(e)})

    registry.register(
        name="sense_projects",
        toolset="senses",
        schema={
            "name": "sense_projects",
            "description": "列出我参与的所有项目，包括项目名称、状态、我的岗位、成员",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=_handle_sense_projects,
        check_fn=lambda: True,
        emoji="🏗️",
    )

    # ── sense_project_detail ──
    def _handle_sense_project_detail(args: Dict[str, Any], **_) -> str:
        project_id = (args.get("project_id") or "").strip()
        if not project_id:
            return _j({"ok": False, "reason": "project_id 必填"})
        try:
            from .loader import load_project
            cfg = load_project(project_id)
            if not cfg:
                return _j({"ok": False, "reason": f"项目 {project_id} 不存在"})
            result = {
                "id": cfg.id,
                "name": cfg.name,
                "description": cfg.description,
                "status": cfg.status,
                "manager": cfg.manager,
                "positions": [
                    {
                        "id": p.id,
                        "name": p.name,
                        "assignees": p.assignees,
                        "responsibilities": p.responsibilities,
                    }
                    for p in cfg.positions
                ],
            }
            return _j({"ok": True, "project": result})
        except Exception as e:
            return _j({"ok": False, "reason": str(e)})

    registry.register(
        name="sense_project_detail",
        toolset="senses",
        schema={
            "name": "sense_project_detail",
            "description": "查看单个项目详情: project.yaml 的 goal/KPI/deadline + 岗位分工 + 成员 + 状态。project_id 必填(从 sense_projects / sense_my_projects 拿)。",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "项目ID"},
                },
                "required": ["project_id"],
            },
        },
        handler=_handle_sense_project_detail,
        check_fn=lambda: True,
        emoji="📋",
    )

    # ── sense_project_todos ──
    def _handle_sense_project_todos(args: Dict[str, Any], **_) -> str:
        project_id = (args.get("project_id") or "").strip()
        if not project_id:
            return _j({"ok": False, "reason": "project_id 必填"})
        try:
            from .loader import load_project
            cfg = load_project(project_id)
            if not cfg:
                return _j({"ok": False, "reason": f"项目 {project_id} 不存在"})
            # Phase 4: list_deliverables 内部走 global_todos.db
            from .crud import list_deliverables
            todos = list_deliverables(db=None, project_id=project_id)
            result = []
            for t in todos:
                item = dict(t)
                if t.get("assignee_instance"):
                    item["assignee_name"] = cfg.get_assignee_name(t["assignee_instance"])
                result.append(item)
            return _j({"ok": True, "project_name": cfg.name, "todos": result})
        except Exception as e:
            return _j({"ok": False, "reason": str(e)})

    registry.register(
        name="sense_project_todos",
        toolset="senses",
        schema={
            "name": "sense_project_todos",
            "description": "查看项目待办看板：所有 deliverables 及其分配状态",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "项目ID"},
                },
                "required": ["project_id"],
            },
        },
        handler=_handle_sense_project_todos,
        check_fn=lambda: True,
        emoji="📊",
    )

    # ── project_todo ──
    def _handle_project_todo(args: Dict[str, Any], **_) -> str:
        action = (args.get("action") or "").strip().lower()
        project_id = (args.get("project_id") or "").strip()
        if not project_id:
            return _j({"ok": False, "reason": "project_id 必填"})

        # Phase 4: crud 已是 thin wrapper 到 global_todos。db 参数不用传,
        # 但保留 None 调用兼容。
        try:
            from .crud import (
                create_deliverable, get_deliverable, list_deliverables, update_deliverable,
            )
        except Exception as e:
            return _j({"ok": False, "reason": str(e)})

        try:
            if action == "create":
                title = (args.get("title") or "").strip()
                if not title:
                    return _j({"ok": False, "reason": "title 必填"})
                did = create_deliverable(
                    db=None,
                    title=title,
                    description=(args.get("description") or "").strip(),
                    priority=(args.get("priority") or "medium").strip(),
                    assignee_instance=(args.get("assignee_instance") or "").strip(),
                    assignee_position=(args.get("assignee_position") or "").strip(),
                    project_id=project_id,
                    acceptance_criteria=(args.get("acceptance_criteria") or "").strip(),
                )
                return _j({"ok": True, "id": did})

            elif action == "list":
                # Phase 4: list_deliverables 只走 project_id 过滤。
                # status / assignee_instance 子筛选由 caller-side 处理(否则在 global 表全表查)
                todos = list_deliverables(db=None, project_id=project_id)
                # 可选过滤
                status_filter = args.get("status")
                assignee_filter = args.get("assignee_instance")
                if status_filter:
                    todos = [t for t in todos if t.get("status") == status_filter]
                if assignee_filter:
                    todos = [t for t in todos if t.get("assignee_instance") == assignee_filter]
                return _j({"ok": True, "todos": todos})

            elif action == "update":
                deliverable_id = (args.get("deliverable_id") or "").strip()
                if not deliverable_id:
                    return _j({"ok": False, "reason": "deliverable_id 必填"})
                updates = {}
                for key in ("status", "assignee_instance", "assignee_position", "priority", "title"):
                    if key in args and args[key]:
                        updates[key] = args[key].strip()
                ok = update_deliverable(db=None, deliverable_id=deliverable_id, project_id=project_id, **updates)
                return _j({"ok": ok})

            elif action == "get":
                deliverable_id = (args.get("deliverable_id") or "").strip()
                if not deliverable_id:
                    return _j({"ok": False, "reason": "deliverable_id 必填"})
                todo = get_deliverable(db=None, deliverable_id=deliverable_id)
                if not todo:
                    return _j({"ok": False, "reason": "deliverable 不存在"})
                return _j({"ok": True, "deliverable": todo})

            else:
                return _j({"ok": False, "reason": f"未知 action: {action}"})
        except Exception as e:
            return _j({"ok": False, "reason": str(e)})

    registry.register(
        name="project_todo",
        toolset="actions",
        schema={
            "name": "project_todo",
            "description": (
                "管理项目待办(deliverable)。"
                "action='create' 创建待办（需 title, project_id, assignee_instance, assignee_position）；"
                "action='list' 列出待办；"
                "action='update' 更新待办状态或分配；"
                "action='get' 查看单个待办详情。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "create | list | update | get",
                        "enum": ["create", "list", "update", "get"],
                    },
                    "project_id": {"type": "string", "description": "项目ID"},
                    "deliverable_id": {"type": "string", "description": "待办ID（update/get时必填）"},
                    "title": {"type": "string", "description": "标题（create时必填）"},
                    "description": {"type": "string", "description": "描述"},
                    "priority": {"type": "string", "description": "优先级: low/medium/high/urgent"},
                    "assignee_instance": {"type": "string", "description": "分配给哪个实例。必填**完整 UUID**(如 c2a5c8e8-e4f5-4c69-be3e-aac49903081d),不要用名字简称(alpha/zero)。用 sense_projects 看项目 yaml 里 positions.assignees 拿到。所有 todos 通过 UUID 反查实例,简称查不到。"},
                    "assignee_position": {"type": "string", "description": "分配给哪个岗位"},
                    "status": {"type": "string", "description": "更新状态: planned/in_progress/done"},
                    "acceptance_criteria": {"type": "string", "description": "完成标准（create 时强烈建议写,会同时同步到对应 todo 的 acceptance_criteria 给承担者看）"},
                },
                "required": ["action", "project_id"],
            },
        },
        handler=_handle_project_todo,
        check_fn=lambda: True,
        emoji="🎯",
    )

    # ── project_deliver ──
    def _handle_project_deliver(args: Dict[str, Any], **_) -> str:
        project_id = (args.get("project_id") or "").strip()
        from_path = (args.get("from_path") or "").strip()
        if not project_id or not from_path:
            return _j({"ok": False, "reason": "project_id 和 from_path 必填"})

        try:
            from ._infra import deliverables_dir
            src = Path(from_path)
            if not src.is_absolute():
                return _j({"ok": False, "reason": "from_path 必须是绝对路径"})
            if not src.exists():
                return _j({"ok": False, "reason": f"文件/目录不存在: {from_path}"})
            dest = deliverables_dir(project_id)

            import shutil
            if src.is_dir():
                dest = dest / src.name
                shutil.copytree(src, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dest / src.name)
            return _j({"ok": True, "delivered_to": str(dest)})
        except Exception as e:
            return _j({"ok": False, "reason": str(e)})

    registry.register(
        name="project_deliver",
        toolset="actions",
        schema={
            "name": "project_deliver",
            "description": (
                "将任务产出提交到项目成果区。"
                "from_path 是任务工作区中的文件路径（绝对路径），"
                "project_id 是目标项目ID。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "项目ID"},
                    "from_path": {"type": "string", "description": "要提交的文件/目录的绝对路径"},
                },
                "required": ["project_id", "from_path"],
            },
        },
        handler=_handle_project_deliver,
        check_fn=lambda: True,
        emoji="📦",
    )

    # ── project_info ──
    def _handle_project_info(args: Dict[str, Any], **_) -> str:
        action = (args.get("action") or "edit").strip().lower()
        project_id = (args.get("project_id") or "").strip()
        if not project_id:
            return _j({"ok": False, "reason": "project_id 必填"})

        try:
            from .manager import update_project_info
            from .loader import load_project
            my_iid = _get_my_instance_id()
            cfg = load_project(project_id)
            if not cfg:
                return _j({"ok": False, "reason": f"项目 {project_id} 不存在"})

            if action == "edit":
                if cfg.manager != my_iid:
                    return _j({"ok": False, "reason": f"只有项目经理({cfg.manager})可以编辑项目信息"})
                updates = {}
                for key in ("name", "description", "manager", "group_chat_id"):
                    if key in args and args[key]:
                        updates[key] = args[key].strip()
                if updates:
                    update_project_info(project_id, **updates)
                return _j({"ok": True})
            else:
                return _j({"ok": False, "reason": f"未知 action: {action}"})
        except Exception as e:
            return _j({"ok": False, "reason": str(e)})

    registry.register(
        name="project_info",
        toolset="actions",
        schema={
            "name": "project_info",
            "description": "编辑项目信息（仅项目经理可用）。action='edit' 更新字段。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "edit"},
                    "project_id": {"type": "string", "description": "项目ID"},
                    "name": {"type": "string", "description": "新名称"},
                    "description": {"type": "string", "description": "新描述"},
                },
                "required": ["action", "project_id"],
            },
        },
        handler=_handle_project_info,
        check_fn=lambda: True,
        emoji="⚙️",
    )

    # ── project_bootstrap ──
    def _handle_project_bootstrap(args: Dict[str, Any], **_) -> str:
        project_id = (args.get("project_id") or "").strip()
        name = (args.get("name") or "").strip()
        manager = (args.get("manager") or "").strip()
        if not project_id or not name or not manager:
            return _j({"ok": False, "reason": "project_id / name / manager 必填"})
        description = (args.get("description") or "").strip()
        group_chat_id = (args.get("group_chat_id") or "").strip()
        positions = args.get("positions") or []

        try:
            from .manager import create_project_full
            ok = create_project_full(
                project_id=project_id,
                name=name,
                description=description,
                manager=manager,
                positions=positions,
                group_chat_id=group_chat_id,
            )
            if not ok:
                return _j({"ok": False, "reason": f"项目 {project_id} 已存在"})
            return _j({"ok": True, "project_id": project_id})
        except Exception as e:
            return _j({"ok": False, "reason": str(e)})

    registry.register(
        name="project_bootstrap",
        toolset="actions",
        schema={
            "name": "project_bootstrap",
            "description": (
                "一次性创建新项目(含目录、project.yaml、初始 3 条骨架 todo 和岗位分工)。"
                "Phase 4 (2026-06) 之后不再有项目本地 todos.db —— 初始 todo 树直接"
                "写到 global_todos.db.todos,通过 project_id 反向关联回项目。"
                "骨架 todo 包括:项目根(project_root) + 项目分工(task_breakdown, "
                "分给 manager 自己消费 task_breakdown skill 拆解执行 todo) + 项目管理。"
                "本工具不直接拆解执行 todo,那是 task_breakdown skill 的活。"
                "positions 是岗位数组,每项含 id/name/assignees(list)/responsibilities(list)。"
                "建议先 invoke_skill('project_bootstrap') 查看完整流程。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "项目ID（如 proj-001）"},
                    "name": {"type": "string", "description": "项目名称"},
                    "description": {"type": "string", "description": "项目描述"},
                    "manager": {"type": "string", "description": "项目经理的 instance_id"},
                    "group_chat_id": {"type": "string", "description": "项目群聊 chat_id（可选）"},
                    "positions": {
                        "type": "array",
                        "description": "岗位列表",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "name": {"type": "string"},
                                "assignees": {"type": "array", "items": {"type": "string"}},
                                "responsibilities": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["id", "name", "assignees"],
                        },
                    },
                },
                "required": ["project_id", "name", "manager"],
            },
        },
        handler=_handle_project_bootstrap,
        check_fn=lambda: True,
        emoji="🚀",
    )

    # ── task_from_deliverable ──
    def _handle_task_from_deliverable(args: Dict[str, Any], **_) -> str:
        """Phase 4 简化版:deliverable 就是 todo 本身,认领 = 单条 UPDATE。

        旧逻辑:create task + 反向 update deliverable(两套表)。
        Phase 4 后:只有 global_todos.db 一套表,deliverable_id == todo.id,
        所以认领 = `update_task(deliv_id, status='in_progress', assignee_instance=mine)`。
        """
        project_id = (args.get("project_id") or "").strip()
        deliverable_id = (args.get("deliverable_id") or "").strip()
        if not project_id or not deliverable_id:
            return _j({"ok": False, "reason": "project_id 和 deliverable_id 必填"})

        try:
            from .crud import get_deliverable, update_deliverable
            d = get_deliverable(db=None, deliverable_id=deliverable_id)
            if not d:
                return _j({"ok": False, "reason": f"deliverable {deliverable_id} 不存在"})

            my_iid = _get_my_instance_id()
            current_assignee = (d.get("assignee_instance") or "").strip()
            if current_assignee and my_iid and current_assignee != my_iid:
                return _j({"ok": False, "reason": f"deliverable 已分配给 {current_assignee}"})

            # 单条 UPDATE:认领 + 开始干活
            ok = update_deliverable(
                db=None,
                deliverable_id=deliverable_id,
                project_id=project_id,
                status="in_progress",
                assignee_instance=my_iid or current_assignee,
            )
            return _j({"ok": bool(ok), "deliverable_id": deliverable_id, "task": d})
        except Exception as e:
            return _j({"ok": False, "reason": str(e)})

    registry.register(
        name="task_from_deliverable",
        toolset="actions",
        schema={
            "name": "task_from_deliverable",
            "description": (
                "把项目 deliverable 原子地转为个人 task："
                "1) 在个人 tasks.db 创建 task（source='project:<id>', linked_deliverable_id 回引）"
                "2) 将项目 deliverable 状态改为 in_progress。"
                "推荐用此工具而非手动 task create，可避免忘记设 source。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "项目ID"},
                    "deliverable_id": {"type": "string", "description": "项目 deliverable ID"},
                },
                "required": ["project_id", "deliverable_id"],
            },
        },
        handler=_handle_task_from_deliverable,
        check_fn=lambda: True,
        emoji="🔄",
    )


# 模块加载即注册到默认 registry —— _ensure_tools_loaded 通过 __import__ 触发本模块时
# 必须立刻完成注册,否则 agent 看不到 project 工具(同 domain.todos.tools 历史 BUG)
try:
    register_project_tools()
except Exception:
    import logging
    logging.getLogger("digital_life.domain.project").warning(
        "Auto-register project tools failed at import", exc_info=True)
