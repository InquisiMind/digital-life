"""CustomerProject — 客户交付项目（设计文档 v1.1 D2 / 6.3 / 刀 4）。

与现有 project.yaml（实例型 InternalProject，ProjectConfig）并存、互不迁就：
  - 实例型项目：projects/<pid>/project.yaml，positions/assignees 挂实例 UUID，
    个人助理这类；继续由 domain/project/loader.py 管。
  - 客户项目：services.db 的 projects 表，持有一组服务型 agent + 一个共享
    项目工作区 projects/<pid>/workspace/；客户资料/中间产物放工作区，
    不放任何服务的私有记忆（特性 2 记忆三层去向）。

项目模版（config/project_templates/*.yaml）定义角色清单：建项目时按模版
批量创建服务，全部挂同一 project_id，共享同一工作区。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("digital_life.domain.project.customer")


# ── 模版 ──────────────────────────────────────────────────────────────


def templates_dir() -> Path:
    from infrastructure.config import get_project_root

    return get_project_root() / "config" / "project_templates"


def load_template(template_id: str) -> dict:
    """读项目模版。返回 {id, name, roles: [{def, name, capabilities?, subscriptions?}]}。"""
    import yaml

    path = templates_dir() / f"{template_id}.yaml"
    if not path.exists():
        raise ValueError(
            f"项目模版不存在: {path}（现有: "
            f"{[p.stem for p in templates_dir().glob('*.yaml')]}）"
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    roles = data.get("roles") or []
    if not isinstance(roles, list) or not roles:
        raise ValueError(f"项目模版 {template_id} 缺 roles 角色清单")
    for r in roles:
        if not isinstance(r, dict) or not r.get("def"):
            raise ValueError(f"项目模版 {template_id} 角色缺 def 字段: {r!r}")
    return {"id": template_id, "name": data.get("name") or template_id, "roles": roles}


# ── 建项目 ────────────────────────────────────────────────────────────


def create_customer_project(name: str, template_id: str) -> dict:
    """建客户项目：注册表落行 + 工作区目录 + 按模版批量创建服务。

    返回 {project: {...}, services: [svc 行...]}。
    """
    from infrastructure.config import get_project_root
    from infrastructure.persistence import services_registry

    template = load_template(template_id)
    project = services_registry.create_project(
        name=name, template_id=template_id
    )
    pid = project["project_id"]
    ws = get_project_root() / "projects" / pid / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    services_registry.update_project_fields(pid)  # touch updated_at
    # workspace_path 存相对路径（repo 根可迁移）
    with services_registry._connect() as conn:
        conn.execute(
            "UPDATE projects SET workspace_path = ? WHERE project_id = ?",
            (f"projects/{pid}/workspace", pid),
        )

    from domain.service import create_service

    created = []
    for role in template["roles"]:
        svc = create_service(
            role["def"],
            project_id=pid,
            display_name=role.get("name") or role["def"],
            capabilities=role.get("capabilities"),
            subscriptions=role.get("subscriptions"),
        )
        created.append(svc)
        logger.info(
            "PROJECT_ROLE_CREATED project=%s role=%r service=%s def=%s",
            pid, role.get("name"), svc["service_id"], role["def"],
        )
    return {"project": services_registry.lookup_project(pid), "services": created}


def get_project_of_service(service_id: str) -> Optional[dict]:
    """服务挂的项目（无/项目已归档返回 None）。"""
    from infrastructure.persistence import services_registry

    svc = services_registry.lookup_service(service_id)
    if not svc or not svc.get("project_id"):
        return None
    project = services_registry.lookup_project(svc["project_id"])
    if not project or project.get("status") != "active":
        return None
    return project


def project_workspace_dir(project_id: str) -> Optional[Path]:
    """项目共享工作区目录。"""
    from infrastructure.config import get_project_root
    from infrastructure.persistence import services_registry

    project = services_registry.lookup_project(project_id)
    if not project:
        return None
    return get_project_root() / "projects" / project_id / "workspace"


# ── 工作区锚定 + 越界拦截（特性 6：引擎层路径白名单，非提示词约束）────


class WorkspaceEscapeError(PermissionError):
    """请求路径越出项目工作区（含 symlink 逃逸）——直接拒绝。"""


def resolve_workspace_path(service_id: str, relative: str) -> Path:
    """把服务请求的相对路径锚定到其项目工作区，返回绝对路径。

    越界拦截两层：
      1. 词法层：拼完后要求真实路径（realpath）仍位于工作区内；
      2. symlink 层：realpath 解析符号链接——工作区内的 symlink 指向
         区外文件同样拒（"symlink 逃逸也拦"）。
    无项目的服务没有共享工作区——锚定到其私有 workspace（同样拦截越界）。
    """
    from infrastructure.config import get_workspace_dir

    base = get_workspace_dir(service_id).resolve()
    requested = (base / relative).resolve()
    if requested != base and base not in requested.parents:
        raise WorkspaceEscapeError(
            f"路径越界: {relative!r} 解析为 {requested}，不在工作区 {base} 内"
        )
    return requested
