"""团队视角的运行上下文。

团队是**项目层的成员身份**，与服务型/实例型运行体正交：服务挂了项目
就是团队成员；实例也可能被聘为项目经理（watchdog 早有"PM 是实例"的
分支）。凡是"团队成员才知道"的休息上下文——项目待办、挂起静默——都
从这里出；生命周期工具（rest）只消费接缝，不自己判断运行体类型，
更不该把"团队"焊死在"服务型"上。
"""

from __future__ import annotations

from typing import Optional


def team_project(runtime_id: str) -> Optional[dict]:
    """该运行体当前挂着的团队项目（无则 None）。

    服务：service 行的 project_id（active/suspended 都算在队）。
    实例：被聘为 pm 的 active 项目（一名实例 PM 也带队）。
    """
    if not runtime_id:
        return None
    from infrastructure.persistence import services_registry as sr

    svc = sr.lookup_service(runtime_id) or {}
    pid = svc.get("project_id") or ""
    if pid:
        proj = sr.lookup_project(pid)
        if proj and proj.get("status") in ("active", "suspended"):
            return proj
        return None
    for p in sr.list_projects(status="active"):
        if p.get("pm_id") == runtime_id:
            return p
    return None


def team_pre_rest_section(runtime_id: str) -> str:
    """睡前提示卡的团队段：项目待办盘点（分给我的优先）。

    不在团队里返回空串（提示卡组装方按空段略过）。个人 todo/灵感碎片
    那些段属于实例的个人世界，与本模块无关。
    """
    proj = team_project(runtime_id)
    if not proj:
        return ""
    pid = proj["project_id"]
    from infrastructure.persistence import services_registry as sr

    todos = [t for t in sr.list_project_todos(pid)
             if (t.get("status") or "") not in ("done", "cancelled")]
    if not todos:
        return ""
    counts: dict[str, int] = {}
    for t in todos:
        s = t.get("status") or "open"
        counts[s] = counts.get(s, 0) + 1
    my_name = (sr.lookup_service(runtime_id) or {}).get("display_name") or ""
    mine = [t for t in todos if my_name and t.get("assign_role") == my_name]
    lines = [
        f"📋 项目待办：{len(todos)} 项未完成"
        f"（进行中 {counts.get('in_progress', 0)} · 阻塞 {counts.get('blocked', 0)} · 待启动 {counts.get('open', 0)}）"
        + (f"，分给我的 {len(mine)} 项" if mine else "")
    ]
    for t in (mine or todos)[:3]:
        lock = " 🔒" if t.get("status") == "blocked" else ""
        lines.append(f"  - [{t.get('status')}] {(t.get('title') or '')[:40]}{lock}")
    return "\n".join(lines)


def silent_sleep_note(runtime_id: str) -> Optional[str]:
    """团队状态要求无闹钟静眠时的提示（项目挂起/项目待办全清），否则 None。

    项目挂起：闹钟已被收队取消，恢复由客户回来驱动，再设闹钟只会复活
    自转循环。待办全清：任务已闭环，新事件（客户消息/新任务指派）自然
    唤醒。只对挂项目的服务生效——suspend 收的是服务成员的闹钟，实例
    PM 的闹钟不在其列。
    """
    if not runtime_id:
        return None
    from infrastructure.persistence import services_registry as sr

    pid = (sr.lookup_service(runtime_id) or {}).get("project_id") or ""
    if not pid:
        return None
    proj = sr.lookup_project(pid) or {}
    if proj.get("status") == "suspended":
        return "项目已挂起，静眠等待客户（勿设闹钟）。"
    todos = sr.list_project_todos(pid)
    if todos and all(t.get("status") == "done" for t in todos):
        return "项目全部任务已完成，进入静眠（未设闹钟）。有新消息或新任务指派时会被唤醒。"
    return None
