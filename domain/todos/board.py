"""Board rendering for wake prompt — 单一渲染入口。

设计原则（2026-06-14 重构）：
  - todos 实体是面板的唯一真相。todo_triggers 降级为属性徽章
  - 只在这里渲染面板。scheduler 注入、sense_todos 工具都调 render_my_board
  - 完整展开但分层：每条 todo 最多 5 行（标题/描述/完成标准/笔记/步骤）

为什么单独成模块：scheduler.py 之前有 130 行硬编码渲染逻辑，
wake_context.py 又有另一套等价渲染——两套都在改字段时会漏。
收敛到这一处以后改面板只动这个文件。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .core.constants import CN_PRIORITIES
from ._infra import get_db

logger = logging.getLogger("digital_life.domain.todos.board")

# 面板会显示的状态（done / cancelled 不上面板——做完了就没必要每轮提醒）
ACTIVE_STATUSES = ("in_progress", "planned", "paused", "idea")

# 状态徽章顺序（用于同项目内排序）
_STATUS_ORDER = {
    "in_progress": 0,  # 进行中最高
    "planned": 1,
    "paused": 2,
    "idea": 3,
}


def _truncate(text: str, n: int) -> str:
    """单行截断 + 去换行（面板里不要折行）。"""
    if not text:
        return ""
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= n else one_line[:n] + "…"


def _relative_days(due_iso: str, now_dt: Any) -> Optional[float]:
    """返回 due_at 距 now 的天数（负数=已过期）。解析失败返回 None。"""
    if not due_iso:
        return None
    try:
        from domain.lifecycle import clock
        due_dt = clock.parse_iso(due_iso)
        return (due_dt - now_dt).total_seconds() / 86400
    except Exception:
        return None


def _load_board_data(iid: str):
    """一次拉所有面板需要的数据：活跃 todos + 每个 todo 的 trigger 徽章/最近笔记/待执行步数。

    返回结构：list[dict]，每条含 task + triggers(list) + latest_note(dict|None) + pending_plan(dict|None) + plan_counts(dict)
    """
    db = get_db()
    try:
        # 活跃 todos（assignee 是我，状态非终态）
        placeholders = ",".join("?" * len(ACTIVE_STATUSES))
        task_rows = db.execute(
            f"SELECT * FROM todos WHERE assignee_instance=? AND status IN ({placeholders}) "
            f"ORDER BY updated_at DESC",
            (iid, *ACTIVE_STATUSES),
        ).fetchall()

        if not task_rows:
            return []

        from .core.models import row_to_task
        tasks = [row_to_task(r) for r in task_rows]
        task_ids = [t.id for t in tasks]
        id_placeholders = ",".join("?" * len(task_ids))

        # trigger 徽章：每个 todo 关联到的非终态 triggers（pending/active）
        trigger_rows = db.execute(
            f"SELECT task_id, trigger_type, due_at, trigger_condition FROM todo_triggers "
            f"WHERE task_id IN ({id_placeholders}) AND status IN ('pending', 'active')",
            tuple(task_ids),
        ).fetchall()
        triggers_by_task: dict[str, list[dict]] = {}
        for r in trigger_rows:
            triggers_by_task.setdefault(r["task_id"], []).append({
                "trigger_type": r["trigger_type"],
                "due_at": r["due_at"] or "",
                "trigger_condition": r["trigger_condition"] or "",
            })

        # 最近笔记（每条 todo 取最新一条）
        note_rows = db.execute(
            f"SELECT n.task_id, n.content, n.created_at FROM todo_notes n "
            f"INNER JOIN (SELECT task_id, MAX(id) as max_id FROM todo_notes "
            f"WHERE task_id IN ({id_placeholders}) GROUP BY task_id) m ON n.id = m.max_id",
            tuple(task_ids),
        ).fetchall()
        latest_note_by_task: dict[str, dict] = {
            r["task_id"]: {"content": r["content"], "created_at": r["created_at"]}
            for r in note_rows
        }

        # plan 计数：每条 todo 的 pending / total + 最早一条 pending 的 content
        plan_rows = db.execute(
            f"SELECT task_id, "
            f"SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) as pending, "
            f"COUNT(*) as total FROM todo_plans "
            f"WHERE task_id IN ({id_placeholders}) GROUP BY task_id",
            tuple(task_ids),
        ).fetchall()
        plan_counts: dict[str, dict] = {
            r["task_id"]: {"pending": r["pending"] or 0, "total": r["total"] or 0}
            for r in plan_rows
        }

        # 每条 todo 的下一个待执行步骤（最早的 pending plan）
        next_plan_rows = db.execute(
            f"SELECT task_id, content FROM todo_plans "
            f"WHERE status='pending' AND task_id IN ({id_placeholders}) "
            f"ORDER BY task_id, order_num, id",
            tuple(task_ids),
        ).fetchall()
        next_plan_by_task: dict[str, str] = {}
        for r in next_plan_rows:
            if r["task_id"] not in next_plan_by_task:
                next_plan_by_task[r["task_id"]] = r["content"]
    finally:
        db.close()

    result = []
    for t in tasks:
        result.append({
            "task": t,
            "triggers": triggers_by_task.get(t.id, []),
            "latest_note": latest_note_by_task.get(t.id),
            "plan_counts": plan_counts.get(t.id, {"pending": 0, "total": 0}),
            "next_plan": next_plan_by_task.get(t.id, ""),
        })
    return result


def _project_label(project_id: str) -> str:
    """把 project_id 翻译成项目名（找不到就显示 id）。"""
    if not project_id:
        return "个人"
    try:
        from domain.project.loader import load_project
        cfg = load_project(project_id)
        if cfg and cfg.name:
            return cfg.name
    except Exception:
        pass
    return f"项目:{project_id}"


def _project_meta_line(project_id: str, iid: str) -> str:
    """生成项目分组头下方的"目标 + 我的岗位 + 协作者"一行元信息。

    设计：把原来 SYS prompt 里的「💼 项目 - 岗位 - 职责 - 协作者」段并入待办面板。
    项目存在 ⟺ 有待办；没有待办的项目整体从这里消失，prompt 自然清理。
    返回空串表示该项目无附加信息（个人项目 / loader 找不到 / 无 iid 等）。
    """
    if not project_id or not iid:
        return ""
    try:
        from domain.project.loader import load_project
        from infrastructure.config import get_instance_display_name
        cfg = load_project(project_id)
        if not cfg:
            return ""
        parts: list[str] = []
        # 我的岗位
        my_pos = cfg.get_position_for_instance(iid)
        if my_pos and my_pos.name:
            parts.append(f"岗位 {my_pos.name}")
        # 协作者(同项目其它岗位的承担者)
        partner_labels: list[str] = []
        for other_pos in cfg.positions:
            if my_pos and other_pos.id == my_pos.id:
                continue
            for asg in other_pos.assignees:
                if asg.startswith("human:"):
                    partner_labels.append(f"zhp/{other_pos.name}")
                else:
                    name = get_instance_display_name(asg) or asg[:8]
                    partner_labels.append(f"{name}/{other_pos.name}")
        if partner_labels:
            parts.append("协作者 " + "、".join(partner_labels))
        # 目标一句话
        goal = cfg.goal or {}
        if isinstance(goal, dict):
            gs = goal.get("statement", "")
            if gs:
                parts.append(f"目标 {gs}")
        if not parts:
            return ""
        return "  · " + " | ".join(parts)
    except Exception:
        return ""


def _render_badges(item: dict, now_dt: Any) -> str:
    """生成徽章列表（紧凑的一行）。"""
    task = item["task"]
    badges = []

    # 状态（中文短词）
    status_cn = {
        "in_progress": "进行中",
        "planned": "计划中",
        "paused": "暂停",
        "idea": "想法",
    }.get(task.status, task.status)
    badges.append({"emoji": "·", "text": status_cn})

    # 优先级（只有 urgent / high 才标）
    if task.priority in ("urgent", "high"):
        badges.append({"emoji": "⚡", "text": CN_PRIORITIES.get(task.priority, task.priority)})

    # deadline
    if task.deadline:
        days = _relative_days(task.deadline, now_dt)
        if days is not None:
            if days < 0:
                badges.append({"emoji": "⚠️", "text": f"已过期 {int(-days)} 天"})
            elif days < 1:
                badges.append({"emoji": "⏰", "text": "今天到期"})
            elif days < 7:
                badges.append({"emoji": "⏰", "text": f"{int(days)+1} 天后"})

    # trigger 徽章（取最早的 due_at 作为时间紧迫度信号）
    for tr in item["triggers"]:
        ttype = tr.get("trigger_type", "")
        if ttype == "ongoing":
            badges.append({"emoji": "🔄", "text": "持续"})
        elif ttype == "condition":
            badges.append({"emoji": "❓", "text": "等条件"})
        elif ttype == "time":
            dd = tr.get("due_at", "")
            days = _relative_days(dd, now_dt)
            if days is not None and days < 0:
                badges.append({"emoji": "🚨", "text": "提醒已逾期"})

    # has_workspace
    if task.has_workspace:
        badges.append({"emoji": "🗂️", "text": "工作空间"})

    # 来源徽章(source 字段)
    # - 'personal' 或空 → 不显示(默认情况, 避免噪音)
    # - 'project:personal_assistant' → 📬 社交接管来源(项目化设计)
    # - 'project:{其它}' → 📁 项目标识
    src = (task.source or "").strip()
    if src and src != "personal":
        if src == "project:personal_assistant":
            badges.append({"emoji": "📬", "text": "社交接管"})
        elif src.startswith("project:"):
            proj_short = src.split(":", 1)[1][:8]
            badges.append({"emoji": "📁", "text": f"项目:{proj_short}"})

    # type=assistance → 🤝 辅助人类类任务(替真人跟进的事项)
    task_type = (getattr(task, "type", "") or "").strip().lower()
    if task_type == "assistance":
        badges.append({"emoji": "🤝", "text": "替真人事"})

    # tags 含 social 前缀 → 退化标签(老 social_review 路径已废, 历史数据兼容)
    try:
        tags_list = task.tags if isinstance(task.tags, list) else []
    except Exception:
        tags_list = []
    for tag in tags_list:
        if isinstance(tag, str) and tag.lower().startswith("social"):
            # 已通过 source='project:personal_assistant' 显示了 📬, 不重复
            if src != "project:personal_assistant":
                badges.append({"emoji": "📬", "text": "社交接管"})
            break

    return " ".join(f"{b['emoji']} {b['text']}" for b in badges)


def _render_todo_lines(item: dict, now_dt: Any) -> list[str]:
    """渲染单条 todo（最多 5 行）。"""
    task = item["task"]
    lines: list[str] = []

    # 行 1：标题 + id + 徽章
    badges = _render_badges(item, now_dt)
    lines.append(f"- **{task.title}** (id={task.id})  {badges}")

    # 行 2：描述摘要（不重复标题）
    if task.description:
        lines.append(f"  · {_truncate(task.description, 100)}")

    # 行 2.5：详情记忆(如有)——模型每次 sense_todos 都能看到的"上下文记忆"。
    # 这是 rest 前可以编辑的整段字段(增删改,非 append-only),区别于 todo_note。
    # 列空 / 纯空白不渲染。truncate 200 字防 board 超长。
    if getattr(task, "detail", "") and task.detail.strip():
        lines.append(f"  📝 {_truncate(task.detail, 200)}")

    # 行 3：完成标准（缺失要 ⚠️）
    if task.acceptance_criteria:
        lines.append(f"  ✅ {_truncate(task.acceptance_criteria, 100)}")
    else:
        lines.append(f"  ⚠️ 完成标准未写 —— 用 `todo(action=\"update\", todo_id=\"{task.id}\", acceptance_criteria=\"...\")` 先补")

    # 行 4：最近笔记
    note = item.get("latest_note")
    if note and note.get("content"):
        note_text = _truncate(note["content"], 80)
        try:
            from domain.lifecycle import clock
            note_dt = clock.parse_iso(note["created_at"])
            ndays = (now_dt - note_dt).total_seconds() / 86400
            age = "今天" if ndays < 1 else (f"{int(ndays)} 天前" if ndays < 30 else f"{int(ndays/30)} 月前")
        except Exception:
            age = ""
        lines.append(f"  💭 {note_text} ({age})")

    # 行 5：待执行步骤
    pc = item.get("plan_counts") or {"pending": 0, "total": 0}
    if pc["total"] > 0:
        next_step = item.get("next_plan", "")
        next_str = f" → 下一步: {_truncate(next_step, 60)}" if next_step else ""
        lines.append(f"  📋 {pc['pending']}/{pc['total']} 待执行{next_str}")

    return lines


def _sort_key(item: dict, now_dt: Any) -> tuple:
    """同项目内排序：进行中 → 已逾期 trigger → 今天/本周 → future → 无 trigger。"""
    task = item["task"]
    status_pri = _STATUS_ORDER.get(task.status, 99)

    # 看最紧迫的 due（task.deadline 或 trigger due_at）
    candidates = []
    if task.deadline:
        d = _relative_days(task.deadline, now_dt)
        if d is not None:
            candidates.append(d)
    for tr in item["triggers"]:
        dd = tr.get("due_at", "")
        if dd:
            d = _relative_days(dd, now_dt)
            if d is not None:
                candidates.append(d)
    earliest_due = min(candidates) if candidates else None

    # 已逾期 (earliest_due < 0) → 在前面；今天/本周内 (0-7) → 次之；其余最后
    due_pri = 0 if earliest_due is None else (
        1 if earliest_due < 0 else (2 if earliest_due < 7 else 3)
    )

    return (status_pri, due_pri, earliest_due if earliest_due is not None else 1e9)


def _render_stats(items: list[dict]) -> str:
    """末尾统计行。简洁强提示，让模型自觉补齐/推进。"""
    total = len(items)
    if total == 0:
        return ""

    in_progress = sum(1 for i in items if i["task"].status == "in_progress")
    no_criteria = sum(1 for i in items if not i["task"].acceptance_criteria)
    pending_plans = sum((i["plan_counts"] or {}).get("pending", 0) for i in items)

    parts = [f"共 {total} 条（进行中 {in_progress}）"]
    if no_criteria > 0:
        parts.append(f"⚠️ {no_criteria} 条缺完成标准")
    if pending_plans > 0:
        parts.append(f"📋 {pending_plans} 个待执行步骤")
    return "\n".join(["", "> **面板状态**：" + " · ".join(parts)])


def render_my_board(iid: str, now_dt: Any) -> str:
    """渲染「我的待办」面板。

    返回完整 markdown 字符串（含 ## 标题段）。空字符串=没有任何活跃 todo。

    iid: 当前实例 id
    now_dt: 用 domain.lifecycle.clock.now_dt() 得到的 tz-aware datetime
    """
    if not iid:
        return ""

    items = _load_board_data(iid)
    if not items:
        return ""

    # 按项目分组
    by_project: dict[str, list[dict]] = {}
    for item in items:
        pid = item["task"].project_id or ""
        by_project.setdefault(pid, []).append(item)

    # 项目顺序：个人在前，其它按字母序
    pids_sorted = sorted(
        by_project.keys(),
        key=lambda p: (0 if p == "" else 1, _project_label(p))
    )

    lines = ["## ── 我的待办 ──"]
    for pid in pids_sorted:
        proj_items = by_project[pid]
        # 同项目内按紧迫度排序
        proj_items.sort(key=lambda i: _sort_key(i, now_dt))
        lines.append(f"\n### {_project_label(pid)}（{len(proj_items)} 条）")
        # 项目元信息(岗位/协作者/目标) — 替代原 SYS 里的职责段; 无 todo = 项目消失, 信息同步清理
        meta_line = _project_meta_line(pid, iid) if pid else ""
        if meta_line:
            lines.append(meta_line)
        for item in proj_items:
            lines.extend(_render_todo_lines(item, now_dt))

    lines.append(_render_stats(items))
    lines.append(_board_guidance())
    lines.append("## ── /待办 ──")

    return "\n".join(lines)


def _board_guidance() -> str:
    """待办面板末尾的「行为指引」——告诉模型面板的归属、生命周期与操作语义。

    面板本身只是数据陈列，没有它模型会"看了不知道该做什么"。指引补三件事：
      1. 归属：这些是你自己设的待办（不是被人派单），自主权在你
      2. 生命周期：任务不主动关闭就一直留存，需要你判断推进/收尾
      3. 操作语义：执行 / 更新 / 关闭三类判断，以及状态含义
    """
    return (
        "\n> **面板用法**：这是你自主维护的待办（不是被指派的任务）。"
        "只要不显式关闭，任务会一直留存。每次看到时自行判断要否动它：\n"
        "> · **执行**：切到 进行中（开始动手）/ **暂停**（权衡后再启）——尤其 ⚡ 高优 且 0/N 待执行的\n"
        "> · **更新**：推进后写 note / 调状态 / 修订验收标准\n"
        "> · **关闭**：完成（验收达标）/ 作废（不再需要）——别让已完成的任务堆着\n"
        "> 不需要每个都动；本次没动的原样留存待下次。"
    )


__all__ = ["render_my_board"]
