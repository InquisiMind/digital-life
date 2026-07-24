"""Task tool registration — registers task/task_plan/task_note/sense_tools with the tool registry."""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict

from .crud import (
    create_task, list_tasks, get_task, update_task, search_tasks,
    create_plan, list_plans, complete_plan, skip_plan, update_plan,
    add_note, read_notes,
    create_todo, list_todos, get_todo, update_todo, delete_todo,
)
from .session_tracking import session_has_execution_attempt, completion_ready
from .scheduler import schedule_task_wakeup
from .board import render_my_board
from ._infra import consume_energy as _consume_energy_hook

logger = logging.getLogger("digital_life.domain.todos")


def register_task_tools(
    *,
    registry: Any | None = None,
    energy_cost_per_thought: float = 0.05,
    consume_energy: Callable[[float], Any] | None = None,
) -> None:
    if registry is None:
        from interfaces.tools.registry import registry as _registry
        registry = _registry
    consume_energy_fn = consume_energy or _consume_energy_hook

    def _j(obj):
        return json.dumps(obj, ensure_ascii=False, default=str)

    # ── task ──
    def _handle_task(args: Dict[str, Any], **context) -> str:
        action = (args.get("action") or "list").strip().lower()
        task_id = (args.get("todo_id") or args.get("task_id") or "").strip()
        title = (args.get("title") or "").strip()
        description = (args.get("description") or "").strip()
        priority = (args.get("priority") or "medium").strip()
        deadline = (args.get("deadline") or "").strip() or None
        tags = args.get("tags") or []
        query = (args.get("query") or "").strip()

        if action == "create":
            if not title:
                return _j({"ok": False, "reason": "title 必填"})
            snap = consume_energy_fn(energy_cost_per_thought)
            source = (args.get("source") or "personal").strip() or "personal"
            linked = (args.get("linked_deliverable_id") or "").strip() or None
            task_type = (args.get("type") or "").strip()
            acceptance_criteria = (args.get("acceptance_criteria") or "").strip()
            detail = (args.get("detail") or "").strip()
            result = create_task(
                title, description, priority, deadline, tags,
                status="planned", source=source, linked_deliverable_id=linked, type=task_type,
                acceptance_criteria=acceptance_criteria,
                detail=detail,
            )
            result["energy"] = round(snap.energy, 1)
            return _j(result)

        if action == "list":
            snap = consume_energy_fn(energy_cost_per_thought)
            tasks = list_tasks()
            # 待办看板可能很长（实测全量 dump 单条可达 69KB）。默认只回最近
            # 30 条即可支撑模型决策；count 保留全部条数，避免误判「没活干」。
            limit = int(args.get("limit") or 30)
            total = len(tasks)
            sliced = tasks[:limit] if limit > 0 else tasks
            payload = {"ok": True, "count": total, "returned": len(sliced),
                       "tasks": sliced, "energy": round(snap.energy, 1)}
            if len(sliced) < total:
                payload["note"] = f"仅展示最近 {len(sliced)} 条，共 {total} 条；如需更多请提高 limit"
            return _j(payload)

        if action == "get":
            if not task_id:
                return _j({"ok": False, "reason": "todo_id 必填"})
            snap = consume_energy_fn(energy_cost_per_thought)
            result = get_task(task_id)
            if not result:
                return _j({"ok": False, "reason": f"待办 {task_id} 不存在"})
            result["energy"] = round(snap.energy, 1)
            return _j(result)

        if action == "update":
            if not task_id:
                return _j({"ok": False, "reason": "todo_id 必填"})
            snap = consume_energy_fn(energy_cost_per_thought)
            acceptance_criteria = (args.get("acceptance_criteria") or "").strip() or None
            # detail: 显式传 "detail" key 才覆盖;不传(None) → 不动现有值。
            # 注意 '' 也会覆盖(用户主动清空 → 不该 None 化)
            detail_raw = args.get("detail")
            detail = detail_raw if detail_raw is not None else None
            result = update_task(task_id, title=title or None, description=description or None,
                                 acceptance_criteria=acceptance_criteria,
                                 detail=detail,
                                 priority=priority if priority != "medium" else None,
                                 deadline=deadline, tags=tags if tags else None)
            result["energy"] = round(snap.energy, 1)
            return _j(result)

        if action == "start":
            if not task_id:
                return _j({"ok": False, "reason": "todo_id 必填"})
            snap = consume_energy_fn(energy_cost_per_thought)
            result = update_task(task_id, status="in_progress")
            if result["ok"]:
                detail = get_task(task_id)
                result["task_detail"] = detail
            result["energy"] = round(snap.energy, 1)
            return _j(result)

        if action == "pause":
            if not task_id:
                return _j({"ok": False, "reason": "todo_id 必填"})
            snap = consume_energy_fn(energy_cost_per_thought)
            result = update_task(task_id, status="paused")
            result["energy"] = round(snap.energy, 1)
            return _j(result)

        if action == "done":
            if not task_id:
                return _j({"ok": False, "reason": "todo_id 必填"})
            # 完成门禁：只剩"必须写过 task_note"一条（设计文档 6.6 重写）。
            # session_id 仍传，保留语义通道；目前 completion_ready 不用它。
            ready, reason = completion_ready(
                task_id,
                session_id=context.get("session_id"),
            )
            if not ready:
                schedule_task_wakeup(task_id, status="in_progress")
                return _j({"ok": False, "reason": reason})
            snap = consume_energy_fn(energy_cost_per_thought)
            result = update_task(task_id, status="done")
            result["energy"] = round(snap.energy, 1)
            result["hint"] = "待办完成了。如果过程中有值得记住的经验或踩过的坑，用 add_lesson 记下来。"
            return _j(result)

        if action == "cancel":
            if not task_id:
                return _j({"ok": False, "reason": "todo_id 必填"})
            snap = consume_energy_fn(energy_cost_per_thought)
            result = update_task(task_id, status="cancelled")
            result["energy"] = round(snap.energy, 1)
            return _j(result)

        if action == "search":
            if not query:
                return _j({"ok": False, "reason": "query 必填"})
            snap = consume_energy_fn(energy_cost_per_thought)
            results = search_tasks(query)
            # 搜索命中可能很多；默认只回前 20 条，保留 count 告知命中总数。
            limit = int(args.get("limit") or 20)
            total = len(results)
            sliced = results[:limit] if limit > 0 else results
            payload = {"ok": True, "count": total, "returned": len(sliced),
                       "tasks": sliced, "energy": round(snap.energy, 1)}
            if len(sliced) < total:
                payload["note"] = f"仅展示前 {len(sliced)} 条命中，共 {total} 条；如需更多请提高 limit"
            return _j(payload)

        return _j({"ok": False, "reason": f"未知 action: {action}"})

    registry.register(
        name="todo",
        toolset="actions",
        schema={
            "name": "todo",
            "description": (
                "待办/任务管理工具——创建、查看、推进、完成你要做的事。"
                "一切皆待办：小到回一条消息、大到搭建系统，都是这条工具管。"
                "action='create' 创建（title必填，强烈建议同时填 acceptance_criteria 写明完成标准）；"
                "action='list' 列出；"
                "action='get' 查看详情含笔记/计划/进度；"
                "action='update' 改属性（含 parent_id 拆解、has_workspace 挂空间、acceptance_criteria 补完成标准）；"
                "action='start' 开始执行；"
                "action='done' 完成（需先执行+回复用户）；"
                "action='cancel' 取消；"
                "action='search' 搜索。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "create | list | get | update | start | pause | done | cancel | search",
                        "enum": ["create", "list", "get", "update", "start", "pause", "done", "cancel", "search"],
                    },
                    "todo_id": {"type": "string", "description": "待办ID（get/update/start/pause/done/cancel必填）"},
                    "title": {"type": "string", "description": "任务标题（create必填）"},
                    "description": {"type": "string", "description": "任务详细描述——写下背景、上下文、为什么要做这件事；别只重复标题。"},
                    "acceptance_criteria": {
                        "type": "string",
                        "description": (
                            "完成标准（什么样算 done）。明文写下你判定这条 todo 可以关掉的具体条目，"
                            "例如「输出 SPAC 标的可投清单 5 条 + 每条带 1-2 句 why」、"
                            "「脚本跑通且输出无报错」。"
                            "create 时务必填，update 可补；没写完成标准的待办容易被反复重启。"
                        ),
                    },
                    "detail": {
                        "type": "string",
                        "description": (
                            "详情字段。每次写入**替换全文**(不是追加、不是部分编辑)——"
                            "想增删改某段,自己读旧版原文 + 在本地组装新全文再写入。"
                            "用法场景:①rest 前给未来自己留「下次醒来先看 X / 现在 Y 进展到 Z」;"
                            "②long-running 任务跨多轮记忆「上次卡在哪 / 谁负责什么」;"
                            "③把不准遗忘的上下文固化下来。"
                            "与 todo_note 的 append-only 笔记区别:detail 是「当前最新版」,"
                            "update 时整个字段全量替换——传 '' 清空,传新文本覆盖旧版本。"
                            "sense_todos 看板会渲染 detail 给模型看到。"
                        ),
                    },
                    "priority": {
                        "type": "string",
                        "description": "优先级：urgent | high | medium | low",
                        "enum": ["urgent", "high", "medium", "low"],
                    },
                    "deadline": {"type": "string", "description": "截止日期"},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "标签列表",
                    },
                    "source": {
                        "type": "string",
                        "description": "任务来源。'personal'(个人, 默认) / 'project:{project_id}'(项目派生) / 'project:personal_assistant'(替真人跟进的事项, 来源于社交接管 review)。建议从 social_feed review 出的消息落 todo 时填后者让面板有📬标识。",
                    },
                    "type": {
                        "type": "string",
                        "description": (
                            "任务类型——驱动对应 SKILL 方法论注入(task_types.yaml 映射), 模型拿到 todo 后看 type 决定走哪套方法论。"
                            "常用值: development / research / trading / communication / project_management / assistance(辅助人类类, 来源是 personal_assistant 项目的替真人跟进事项)。留空走默认 todo_execution 通用方法论。"
                        ),
                    },
                    "linked_deliverable_id": {
                        "type": "string",
                        "description": "对应的项目 deliverable ID（可选，用于反向引用）",
                    },
                    "query": {"type": "string", "description": "搜索关键词（search必填）"},
                    "limit": {
                        "type": "integer",
                        "description": "list/search 时最多返回多少条（list 默认 30，search 默认 20）。命中数仍在 count 字段。",
                    },
                },
                "required": ["action"],
            },
        },
        handler=_handle_task,
        check_fn=lambda: True,
        emoji="📋",
        max_result_size_chars=8000,
    )

    # ── task_plan ──
    def _handle_task_plan(args: Dict[str, Any], **_) -> str:
        action = (args.get("action") or "list").strip().lower()
        task_id = (args.get("todo_id") or args.get("task_id") or "").strip()
        content = (args.get("content") or "").strip()
        deadline = (args.get("deadline") or "").strip() or None
        plan_id = args.get("plan_id")

        if action == "create":
            if not task_id or not content:
                return _j({"ok": False, "reason": "task_id 和 content 必填"})
            snap = consume_energy_fn(energy_cost_per_thought)
            result = create_plan(task_id, content, deadline)
            result["energy"] = round(snap.energy, 1)
            return _j(result)

        if action == "list":
            if not task_id:
                return _j({"ok": False, "reason": "todo_id 必填"})
            snap = consume_energy_fn(energy_cost_per_thought)
            plans = list_plans(task_id)
            return _j({"ok": True, "plans": plans, "energy": round(snap.energy, 1)})

        if action == "complete":
            if not plan_id:
                return _j({"ok": False, "reason": "plan_id 必填"})
            snap = consume_energy_fn(energy_cost_per_thought)
            result = complete_plan(int(plan_id))
            result["energy"] = round(snap.energy, 1)
            return _j(result)

        if action == "skip":
            if not plan_id:
                return _j({"ok": False, "reason": "plan_id 必填"})
            snap = consume_energy_fn(energy_cost_per_thought)
            result = skip_plan(int(plan_id))
            result["energy"] = round(snap.energy, 1)
            return _j(result)

        if action == "update":
            if not plan_id:
                return _j({"ok": False, "reason": "plan_id 必填"})
            snap = consume_energy_fn(energy_cost_per_thought)
            result = update_plan(int(plan_id), content=content or None, deadline=deadline)
            result["energy"] = round(snap.energy, 1)
            return _j(result)

        return _j({"ok": False, "reason": f"未知 action: {action}"})

    registry.register(
        name="todo_plan",
        toolset="actions",
        schema={
            "name": "todo_plan",
            "description": (
                "管理待办的执行步骤/计划（拆解维度）。"
                "action='create' 添加步骤（todo_id+content必填）；"
                "action='list' 列出步骤（todo_id必填）；"
                "action='complete' 完成（plan_id必填）；"
                "action='skip' 跳过（plan_id必填）；"
                "action='update' 更新（plan_id必填）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "create | list | complete | skip | update",
                        "enum": ["create", "list", "complete", "skip", "update"],
                    },
                    "todo_id": {"type": "string", "description": "待办ID（create/list必填）"},
                    "plan_id": {"type": "integer", "description": "步骤ID（complete/skip/update必填）"},
                    "content": {"type": "string", "description": "步骤内容（create必填）"},
                    "deadline": {"type": "string", "description": "截止日期"},
                },
                "required": ["action"],
            },
        },
        handler=_handle_task_plan,
        check_fn=lambda: True,
        emoji="📝",
    )

    # ── task_note ──
    def _handle_task_note(args: Dict[str, Any], **context) -> str:
        action = (args.get("action") or "read").strip().lower()
        task_id = (args.get("todo_id") or args.get("task_id") or "").strip()
        content = (args.get("content") or "").strip()

        if not task_id:
            return _j({"ok": False, "reason": "todo_id 必填"})

        snap = consume_energy_fn(energy_cost_per_thought)

        if action == "add":
            if not content:
                return _j({"ok": False, "reason": "content 必填"})
            session_id = str(context.get("session_id") or "")
            if session_id.startswith("tx_task_reminder_") and not session_has_execution_attempt(session_id):
                return _j({
                    "ok": False,
                    "reason": "任务执行会话必须先调用 terminal / web_search / web_extract 等真实执行工具，再写入结果笔记",
                })
            result = add_note(task_id, content)
            result["energy"] = round(snap.energy, 1)
            return _j(result)

        if action == "read":
            notes = read_notes(task_id)
            return _j({"ok": True, "notes": notes, "energy": round(snap.energy, 1)})

        return _j({"ok": False, "reason": f"未知 action: {action}"})

    registry.register(
        name="todo_note",
        toolset="actions",
        schema={
            "name": "todo_note",
            "description": (
                "在待办工作空间中记录/读取笔记（记忆维度）。"
                "推进待办时用笔记记录进展和卡点，下次醒来直接接着走。"
                "action='add' 添加笔记；action='read' 读取笔记。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "add | read",
                        "enum": ["add", "read"],
                    },
                    "todo_id": {"type": "string", "description": "待办ID"},
                    "content": {"type": "string", "description": "笔记内容（add时必填）"},
                },
                "required": ["action", "todo_id"],
            },
        },
        handler=_handle_task_note,
        check_fn=lambda: True,
        emoji="📓",
    )

    # ── sense_todos ──
    def _handle_sense_tasks(args: Dict[str, Any], **_) -> str:
        snap = consume_energy_fn(0)
        source = (args.get("source") or "").strip().lower()
        project_id = (args.get("project_id") or "").strip()
        status_filter = (args.get("status") or "").strip().lower() or None

        # 无过滤 → 返回完整面板（含 description/acceptance_criteria/notes/plans）
        # 有过滤 → 返回过滤后的列表（明细可通过 todo get 拿更深的字段）
        if not source and not project_id and not status_filter:
            from domain.lifecycle import clock
            try:
                from infrastructure.config import get_app_instance_id
                iid = get_app_instance_id() or ""
            except Exception:
                iid = ""
            board_text = render_my_board(iid, clock.now_dt()) if iid else ""
            if board_text:
                return _j({"ok": True, "board": board_text, "energy": round(snap.energy, 1)})
            return _j({"ok": True, "message": "当前没有待办", "energy": round(snap.energy, 1)})

        # 带过滤：返回结构化列表
        active = list_tasks(status_filter=status_filter,
                            source=source or None,
                            project_id=project_id or None)
        if not active:
            return _j({"ok": True, "message": "当前没有符合条件的待办", "tasks": []})
        return _j({"ok": True, "tasks": active, "energy": round(snap.energy, 1)})

    registry.register(
        name="sense_todos",
        toolset="senses",
        schema={
            "name": "sense_todos",
            "description": (
                "查看当前待办概览。可选过滤："
                "source='personal' 看个人待办；source='project:{pid}' 看某项目派下的；"
                "project_id='<pid>' 等价于 source='project:<pid>'；status 过滤状态。"
                "不传任何过滤 = 全部待办。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "description": "planned / in_progress / done / cancelled / paused / idea"},
                    "source": {"type": "string", "description": "'personal' 或 'project:{pid}'"},
                    "project_id": {"type": "string", "description": "项目 id（自动转 source 维度）"},
                },
            },
        },
        handler=_handle_sense_tasks,
        check_fn=lambda: True,
        emoji="📌",
    )

    # ── todo ── 待办 ──
    def _handle_todo(args: Dict[str, Any], **_) -> str:
        """待办工具。三种触发：time / condition / ongoing。
        时间型 create 自动注册闹钟；update/delete/status 变化自动联动闹钟。
        """
        action = (args.get("action") or "list").strip().lower()
        snap = consume_energy_fn(0)

        if action == "create":
            owner_todo_id = (args.get("owner_todo_id") or args.get("task_id") or "").strip()
            if not owner_todo_id:
                return _j({"ok": False, "reason": "owner_todo_id 必填（指明 trigger 归属的待办 todo_id）"})
            assignee = (args.get("assignee") or "").strip()
            if not assignee:
                # 默认承担人 = 当前实例
                try:
                    from infrastructure.config import get_app_instance_id
                    assignee = get_app_instance_id() or ""
                except Exception:
                    assignee = ""
            content = (args.get("content") or "").strip()
            if not content:
                return _j({"ok": False, "reason": "content 必填"})
            trigger_type = (args.get("trigger_type") or "time").strip().lower()
            due_at = (args.get("due_at") or "").strip() or None
            condition = (args.get("condition") or "").strip() or None
            r = create_todo(owner_todo_id, assignee, content, trigger_type, due_at, condition)
            r["energy"] = round(snap.energy, 1)
            return _j(r)

        if action == "list":
            assignee = (args.get("assignee") or "").strip() or None
            status = (args.get("status") or "").strip() or None
            owner_todo_id = (args.get("owner_todo_id") or args.get("task_id") or "").strip() or None
            trigger_type = (args.get("trigger_type") or "").strip() or None
            items = list_todos(assignee=assignee, status=status, task_id=owner_todo_id,
                                trigger_type=trigger_type)
            return _j({"ok": True, "todos": items, "energy": round(snap.energy, 1)})

        if action == "get":
            try:
                todo_id = int(args.get("todo_id"))
            except Exception:
                return _j({"ok": False, "reason": "todo_id 必填且须是整数"})
            item = get_todo(todo_id)
            if not item:
                return _j({"ok": False, "reason": f"待办 {todo_id} 不存在"})
            return _j({"ok": True, "todo": item})

        if action == "update":
            raw_id = args.get("todo_id")
            try:
                todo_id = int(raw_id)
            except Exception:
                return _j({"ok": False, "reason": "todo_id 必填且须是整数"})
            kw = {}
            for k in ("content", "due_at", "trigger_condition", "status", "note"):
                if k in args and args[k] is not None:
                    v = args[k]
                    if k in ("content", "trigger_condition", "note"):
                        kw[k] = v
                    elif k == "due_at":
                        kw[k] = (str(v) or "").strip() or None
                    elif k == "status":
                        kw[k] = str(v).strip().lower()
            r = update_todo(todo_id, **kw)
            r["energy"] = round(snap.energy, 1)
            return _j(r)

        if action == "delete":
            try:
                todo_id = int(args.get("todo_id"))
            except Exception:
                return _j({"ok": False, "reason": "todo_id 必填且须是整数"})
            return _j(delete_todo(todo_id))

        return _j({"ok": False, "reason": f"未知 action: {action}"})

    registry.register(
        name="todo_trigger",
        toolset="actions",
        schema={
            "name": "todo_trigger",
            "description": (
                "待办触发管理——创建带触发机制的待办（alarm/space 维度）。三种触发：time（注册闹钟到点提醒）/ "
                "condition（等事件触发，由你自己判断）/ ongoing（常驻不完结）。"
                "可以为自己或他人创建（协作型，跨实例派发）。"
                "\n\n用法:\n"
                "  create: 必填 owner_todo_id, content；可选 assignee（默认自己）/ "
                "trigger_type（默认 time） / due_at（time 型必填） / condition（condition 型必填）\n"
                "  list: 可选过滤 assignee / status / owner_todo_id / trigger_type\n"
                "  get / update / delete: 需要 todo_id\n"
                "  update: status 切到 'cancelled' 或 'done' 时**建议附带 note 说明理由/结果**"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["create", "list", "get", "update", "delete"]},
                    "todo_id": {"type": "integer", "description": "todo_trigger id（get/update/delete 必填）"},
                    "owner_todo_id": {"type": "string", "description": "所属待办的todo_id（create/list 必填）"},
                    "assignee": {"type": "string", "description": "承担人 instance_id；为别人定 todo 时填对方 id"},
                    "content": {"type": "string", "description": "待办内容"},
                    "trigger_type": {"type": "string", "enum": ["time", "condition", "ongoing"]},
                    "due_at": {"type": "string", "description": "ISO8601 时间，time 型必填"},
                    "condition": {"type": "string", "description": "条件型待办的触发条件（自然语言描述）"},
                    "status": {"type": "string", "enum": ["pending", "active", "done", "cancelled"]},
                    "note": {"type": "string", "description": "状态变更理由或完成说明；update 到 cancelled/done 时推荐填，便于追溯为什么处理掉这条待办"},
                },
                "required": ["action"],
            },
        },
        handler=_handle_todo,
        check_fn=lambda: True,
        emoji="✅",
    )

    logger.info("Task tools registered: todo, todo_plan, todo_note, sense_todos, todo_trigger")


# 模块加载即注册到默认 registry —— _ensure_tools_loaded 通过 __import__ 触发本模块时
# 必须立刻完成注册,否则 agent 看不到 task/todo 工具(曾被遗漏,导致 alpha 无法 cancel 过期 todo)
try:
    register_task_tools()
except Exception:
    logger.warning("Auto-register task tools failed at import", exc_info=True)
