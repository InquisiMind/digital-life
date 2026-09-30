"""CustomerProject — 轻量项目模块（刀 4b，2026-09-29 与 zhp 收敛的分层）。

项目 = 一个轻量管理工具：基本信息 + 成员（服务 ID，N:M 不设约束）+ 待办树
+ 目录（成员个人区 + shared 共享区）+ pm_id + watchdog 开关。

分层（核心是解耦）：
  - 领域原语（本模块）：无业务假设——引擎不认识"阶段/方法论/咨询"
  - 项目模版（config/project_templates/*.yaml）：业务怎么做的出厂定义
    （角色清单→服务生成、每岗位工具面、PM 标记、初始待办骨架）
  - agent def（app.yaml）：agent 是什么（人格/模型/基础技能）——与项目无关

进度/阶段管理不进引擎：阶段 = 待办分组，方法论 = PM 的 skill 与模版初始
待办。唯一特殊事件 project_stall（watchdog，项目级开关默认关）：未完成
待办存在但全员不活跃 → 催 PM。

目录布局：
  projects/{pid}/shared/               ← 共享交付面：成员可读，写入走 deliver 转正
  projects/{pid}/members/{member_id}/  ← 成员个人区（挂项目的服务 workspace 指向这里）
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Optional

logger = logging.getLogger("digital_life.domain.project.customer")


class WorkspaceEscapeError(PermissionError):
    """请求路径越出允许范围（含 symlink 逃逸）——直接拒绝。"""


# ── 模版实体 ──────────────────────────────────────────────────────────


def templates_dir() -> Path:
    from infrastructure.config import get_project_root

    return get_project_root() / "config" / "project_templates"


def load_template(template_id: str) -> dict:
    """读项目模版。

    返回 {id, name, watchdog, roles: [{def, name, pm?, tools?, capabilities?,
    subscriptions?}], initial_todos: [{title, assign_role?, detail?}]}。
    """
    import yaml

    path = templates_dir() / f"{template_id}.yaml"
    if not path.exists():
        raise ValueError(
            f"项目模版不存在: {path}（现有: {[p.stem for p in templates_dir().glob('*.yaml')]}）"
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    roles = data.get("roles") or []
    if not isinstance(roles, list) or not roles:
        raise ValueError(f"项目模版 {template_id} 缺 roles 角色清单")
    for r in roles:
        if not isinstance(r, dict) or not r.get("def"):
            raise ValueError(f"项目模版 {template_id} 角色缺 def 字段: {r!r}")
    return {
        "id": template_id,
        "name": data.get("name") or template_id,
        "watchdog": bool(data.get("watchdog", False)),
        "roles": roles,
        "initial_todos": data.get("initial_todos") or [],
    }


# ── 建项目（= 一套社交圈的建立） ─────────────────────────────────────


def create_project_from_template(
    name: str,
    template_id: str,
    *,
    customer: Optional[dict] = None,   # {id, name}：S00 客户信息（PM 的客户联系人）
    pm_override: str = "",             # 创建者是 agent 且自任 PM 时覆盖
    description: str = "",
) -> dict:
    """按模版建项目：项目行 + 成员服务（含工具面）+ 社交圈 + 初始待办 + 目录。

    返回 {project, services: [...], pm_service_id, todos: [...]}。
    """
    from infrastructure.config import get_project_root
    from infrastructure.persistence import services_registry

    template = load_template(template_id)

    # 1) 成员服务（角色 → 服务，工具面抄进行）
    from domain.service import create_service

    created: list[dict] = []
    pm_service_id = ""
    for role in template["roles"]:
        svc = create_service(
            role["def"],
            display_name=role.get("name") or role["def"],
            capabilities=role.get("capabilities"),
            subscriptions=role.get("subscriptions"),
            tools=role.get("tools"),
        )
        created.append(svc)
        if role.get("pm"):
            pm_service_id = svc["service_id"]

    # 2) 项目行 + 成员表
    pid_row = services_registry.create_project(
        name=name,
        template_id=template_id,
        description=description,
        pm_id="",  # 服务创建完才能定 PM，下面补
        watchdog_enabled=template["watchdog"],
        customer_id=(customer or {}).get("id", ""),
    )
    pid = pid_row["project_id"]
    for svc, role in zip(created, template["roles"]):
        services_registry.update_service_fields(svc["service_id"], project_id=pid)
        services_registry.add_project_member(pid, svc["service_id"], role.get("name") or "")

    # 3) PM：模版标记的角色；pm_override（创建者自任）优先
    final_pm = pm_override or pm_service_id
    if not final_pm:
        raise ValueError(f"项目模版 {template_id} 缺 pm: true 的角色，也未传 pm_override")
    services_registry.update_project_fields(pid, pm_id=final_pm)

    # 4) 目录骨架
    root = get_project_root() / "projects" / pid
    (root / "shared").mkdir(parents=True, exist_ok=True)
    for svc in created:
        (root / "members" / svc["service_id"]).mkdir(parents=True, exist_ok=True)

    # 5) 社交圈：群 + 伙伴联系人 + PM 的客户联系人
    from domain.service.social import register_social_circle

    register_social_circle(
        pid,
        members=[{"service_id": s["service_id"], "role": s["display_name"]} for s in created],
        pm_service_id=final_pm,
        customer=customer,
    )

    # 6) 初始待办（方法论骨架；assign_role 按角色名解析到服务）
    role_to_sid = {s["display_name"]: s["service_id"] for s in created}
    todos = []
    for t in template["initial_todos"]:
        assignee = role_to_sid.get(t.get("assign_role") or "", "")
        todos.append(
            services_registry.create_project_todo(
                pid, t["title"], detail=t.get("detail", ""),
                assignee_id=assignee, created_by=final_pm,
                kind=t.get("kind") or "task",
            )
        )

    logger.info(
        "PROJECT_BOOTSTRAPPED project=%s name=%r members=%d pm=%s todos=%d customer=%s",
        pid, name, len(created), final_pm[:12], len(todos), bool(customer),
    )
    return {
        "project": services_registry.lookup_project(pid),
        "services": created,
        "pm_service_id": final_pm,
        "todos": todos,
    }


# ── 查询 ──────────────────────────────────────────────────────────────


def get_project_of_service(service_id: str) -> Optional[dict]:
    """服务挂的第一个 active 项目（N:M 下取最近加入的；无则 None）。"""
    from infrastructure.persistence import services_registry

    for m in reversed(services_registry.list_project_members_of(service_id)):
        project = services_registry.lookup_project(m["project_id"])
        if project and project.get("status") == "active":
            return project
    return None


def project_root_dir(project_id: str) -> Path:
    from infrastructure.config import get_project_root

    return get_project_root() / "projects" / project_id


def member_personal_dir(project_id: str, member_id: str) -> Path:
    return project_root_dir(project_id) / "members" / member_id


def shared_dir(project_id: str) -> Path:
    return project_root_dir(project_id) / "shared"


# ── 工作区锚定 + 越界拦截（特性 6：引擎层路径白名单） ─────────────────


def resolve_workspace_path(service_id: str, relative: str, *, for_write: bool = False) -> Path:
    """把服务请求的相对路径锚定到项目目录，返回绝对路径。

    读允许 members/{自己}/** 与 shared/**；写只允许 members/{自己}/**（进
    shared 走 deliver 转正）。realpath 双层拦截：词法越界（../）与 symlink
    逃逸（区内链接指向区外）都拒。未挂项目的服务锚定其私有 workspace。
    """
    from infrastructure.config import get_workspace_dir

    project = get_project_of_service(service_id)
    if project is None:
        base = get_workspace_dir(service_id).resolve()
        requested = (base / relative).resolve()
        if requested != base and base not in requested.parents:
            raise WorkspaceEscapeError(
                f"路径越界: {relative!r} → {requested}，不在工作区 {base} 内"
            )
        return requested

    pid = project["project_id"]
    base = project_root_dir(pid).resolve()
    requested = (base / relative).resolve()
    if requested != base and base not in requested.parents:
        raise WorkspaceEscapeError(
            f"路径越界: {relative!r} → {requested}，不在项目目录 {base} 内"
        )
    personal = member_personal_dir(pid, service_id).resolve()
    in_personal = requested == personal or personal in requested.parents
    if for_write and not in_personal:
        raise WorkspaceEscapeError(
            f"写路径受限: {relative!r} 只能写个人区 members/{service_id[:12]}/…，"
            "进共享区用 deliver_to_shared 转正"
        )
    if not in_personal:
        shared = shared_dir(pid).resolve()
        if requested != shared and shared not in requested.parents:
            raise WorkspaceEscapeError(
                f"读路径受限: {relative!r} 只能读个人区与 shared/，"
                f"不能读其他成员的个人区（{relative} 越出了）"
            )
    return requested


def deliver_file(service_id: str, relative: str) -> dict:
    """个人区 → shared/ 转正（显式 deliver：让过程更郑重）。

    shared/ 存最新，.versions/{时间戳}/ 留历史。
    """
    from datetime import datetime

    project = get_project_of_service(service_id)
    if project is None:
        raise ValueError("本服务未挂项目（无共享区可转正）")
    pid = project["project_id"]
    # relative 是个人区内相对路径——换算成项目根相对再锚定校验
    src = resolve_workspace_path(service_id, f"members/{service_id}/{relative}", for_write=True)
    personal = member_personal_dir(pid, service_id).resolve()
    if personal not in src.parents:
        raise WorkspaceEscapeError(
            f"deliver 源必须在个人区: {relative!r}"
        )
    if not src.is_file():
        raise ValueError(f"转正源不存在: {relative!r}")
    rel_in_personal = src.relative_to(personal)
    dst = shared_dir(pid) / rel_in_personal
    version_dir = shared_dir(pid) / ".versions" / datetime.now().strftime("%Y%m%d_%H%M%S")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        version_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(dst, version_dir / rel_in_personal)
    shutil.copy2(src, dst)
    logger.info(
        "PROJECT_DELIVER project=%s member=%s file=%s", pid, service_id[:12], rel_in_personal
    )
    return {"delivered": str(rel_in_personal), "shared_path": str(dst),
            "versioned": dst.exists()}
