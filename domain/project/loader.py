"""Project YAML loader."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("digital_life.domain.project")


@dataclass
class Position:
    id: str
    name: str
    description: str = ""
    assignees: list[str] = field(default_factory=list)
    responsibilities: list[str] = field(default_factory=list)


@dataclass
class ProjectConfig:
    id: str
    name: str
    description: str = ""
    status: str = "active"
    manager: str = ""  # instance_id of project manager
    group_chat_id: str = ""
    positions: list[Position] = field(default_factory=list)
    # v5 扩展：目标驱动
    goal: dict = field(default_factory=dict)        # {statement, started_at, deadline, start_capital, target_capital}
    kpis: list = field(default_factory=list)        # [{name, unit, target, snapshot_at, snapshot_value, ...}]
    thesis: list = field(default_factory=list)      # [{id, statement, confidence, evidence, last_reviewed}]
    review_schedule: dict = field(default_factory=dict)  # {daily, weekly, monthly}

    def get_position(self, position_id: str) -> Position | None:
        for p in self.positions:
            if p.id == position_id:
                return p
        return None

    def get_position_for_instance(self, instance_id: str) -> Position | None:
        for p in self.positions:
            if instance_id in p.assignees:
                return p
        return None

    def get_assignee_name(self, instance_id: str) -> str:
        """Resolve instance_id to position name, or return instance_id."""
        pos = self.get_position_for_instance(instance_id)
        return pos.name if pos else instance_id


def load_project(project_id: str) -> ProjectConfig | None:
    """Load project.yaml from projects/{project_id}/project.yaml."""
    yaml_path = Path(__file__).resolve().parents[2] / "projects" / project_id / "project.yaml"
    if not yaml_path.exists():
        logger.warning("Project config not found: %s", yaml_path)
        return None

    try:
        import yaml
    except ImportError:
        logger.warning("PyYAML not available, cannot load project config")
        return None

    with open(yaml_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    # ⚠ 2026-06-23 重构回归修复:之前把 `proj = raw.get("project", {})` 这行
    # 误删,但下面 11 处 `proj.get(...)` 引用留着,导致 NameError "name 'proj' is not defined"
    # → build_my_portfolio 失败 → sense_my_projects / sense_project_detail /
    # sense_project_todos 全部报错 → zero daily_planner Phase 0 拉不了 portfolio。
    # 已被 zero 6/24 报上来的 P0 系统 bug 报告指认。
    proj = raw.get("project", {}) or {}

    return ProjectConfig(
        id=proj.get("id", project_id),
        name=proj.get("name", project_id),
        description=proj.get("description", ""),
        status=proj.get("status", "active"),
        # 2026-06-23 防御:project.yaml 里写 "zero"/"alpha" 字面值常见错误。
        # 把可识别的实例名自动转 UUID,sense_projects.get_position_for_instance
        # 用 UUID 比对实例 id 就能正常匹配。
        manager=_normalize_instance_ref(proj.get("manager", "")),
        # env override：每个部署可独立配置 chat。新部署不需要改 project.yaml。
        # 形如 PROJECT_{PID}_GROUP_CHAT_ID=oc_xxx 会覆盖该 project 的 group_chat_id。
        group_chat_id=_resolve_chat_id_with_env(project_id, proj.get("group_chat_id", "")),
        positions=[
            Position(
                id=p.get("id", ""),
                name=p.get("name", ""),
                description=p.get("description", ""),
                assignees=[_normalize_instance_ref(a) for a in p.get("assignees", [])],
                responsibilities=p.get("responsibilities", []),
            )
            for p in raw.get("positions", [])
        ],
        goal=proj.get("goal", {}) or {},
        kpis=proj.get("kpis", []) or [],
        thesis=proj.get("thesis", []) or [],
        review_schedule=proj.get("review_schedule", {}) or {},
    )


# 实例名 → UUID 映射。新增实例时往这里加。
_INSTANCE_NAME_TO_IID = {
    "zero": "4b75b663-3f06-4547-9414-9dcbf5dbd943",
    "alpha": "c8b845cc-9009-4430-94e8-13836cf2f2f8",
}


def _normalize_instance_ref(ref: str) -> str:
    """把 project.yaml 里的 'zero'/'alpha' 之类的实例名字面值规范化为 UUID。

    历史问题:早期的 yaml 会用名字(zero/alpha),但 sense_projects 等工具用
    instance UUID 比对 Occupation.get_position_for_instance。名字和 UUID 不匹配 →
    实例永远看不到自己"参与了"哪个项目。
    """
    if not ref:
        return ""
    return _INSTANCE_NAME_TO_IID.get(ref, ref)


def _resolve_chat_id_with_env(project_id: str, default: str) -> str:
    """允许 env 覆盖 project.yaml 里的 group_chat_id。

    新部署/clone 直接在自己的 secrets.env 设 PROJECT_<UPPER_ID>_GROUP_CHAT_ID=oc_xxx
    就能覆盖默认值，不必改仓库里的 project.yaml。
    """
    import os as _os
    env_key = f"PROJECT_{project_id.upper().replace('-', '_')}_GROUP_CHAT_ID"
    return _os.environ.get(env_key, default or "").strip()


def list_project_ids() -> list[str]:
    """List all project IDs by scanning the projects/ directory."""
    projects_dir = Path(__file__).resolve().parents[2] / "projects"
    if not projects_dir.exists():
        return []
    return sorted(
        d.name for d in projects_dir.iterdir()
        if d.is_dir() and (d / "project.yaml").exists()
    )


def load_all_projects() -> dict[str, ProjectConfig]:
    """Load all project configs, keyed by project ID."""
    result = {}
    for pid in list_project_ids():
        cfg = load_project(pid)
        if cfg:
            result[pid] = cfg
    return result


def resolve_assignee_position(assignee_id: str) -> tuple[str, str] | None:
    """Resolve an assignee identifier to (project_name, position_name).

    Searches all active projects for a position whose assignees contains
    `assignee_id`. Returns None if not found.

    Use case: annotate group_message sender with their project position.
    `assignee_id` can be an instance uuid or a `human:xxx` identifier.
    """
    if not assignee_id:
        return None
    for pid, cfg in load_all_projects().items():
        if cfg.status != "active":
            continue
        pos = cfg.get_position_for_instance(assignee_id)
        if pos:
            return cfg.name, pos.name
    return None
