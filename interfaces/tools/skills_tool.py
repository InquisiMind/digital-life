"""Skills 工具：渐进式披露的 tier 1（列表）+ tier 2/3（查看）。

Tier 1 — skills_list：返回注册 skill 的 name + description（极低 token）
Tier 2 — skill_view：返回完整 SKILL.md 正文
Tier 3 — skill_view(..., reference="xxx.md")：返回 references/ 下的文件
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from interfaces.tools.registry import registry, tool_error

logger = logging.getLogger(__name__)


def _j(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False)


def _burn(amount: float = 0.3):
    from domain.vital import consume_energy
    consume_energy(amount, reason="sense")


def _load_frontmatter(skill_file: Path) -> dict[str, Any]:
    from interfaces.skills import parse_frontmatter
    content = skill_file.read_text(encoding="utf-8")
    fm, _ = parse_frontmatter(content)
    return fm


def _find_skill(name: str) -> Path | None:
    """查找 skill SKILL.md 路径。

    查找顺序：实例 skills/ → 系统 skills/。
    跳过 platform 不兼容的 skill。
    """
    from interfaces.skills import (
        get_instance_skills_dir,
        get_overlay_skills_dir,
        get_system_skills_dir,
        skill_matches_platform,
    )
    from infrastructure.config import get_app_instance_id

    uuid = get_app_instance_id()
    bases = [b for b in [get_overlay_skills_dir(uuid), get_instance_skills_dir(uuid), get_system_skills_dir()] if b]
    for base in bases:
        skill_file = base / name / "SKILL.md"
        if skill_file.exists():
            fm = _load_frontmatter(skill_file)
            if not skill_matches_platform(fm):
                continue
            return skill_file
    return None


# ── Tool 1：skills_list ──────────────────────────────────────────────────


def _handle_skills_list(args: dict[str, Any], **kwargs) -> str:
    """返回已注册 skill 的 name + description（tier 1）。

    返回结构：
      {"skills": [{"name": "...", "description": "...", "version": "...", "source": "system"|"instance"}, ...]}
    """
    _burn()

    from interfaces.skills import (
        get_instance_skills_dir,
        get_overlay_skills_dir,
        get_system_skills_dir,
        iter_skill_files,
        skill_matches_platform,
        get_instance_registered_skills,
    )
    from infrastructure.config import get_app_instance_id

    uuid = get_app_instance_id()
    system_dir = get_system_skills_dir()
    instance_dir = get_instance_skills_dir(uuid)
    overlay_dir = get_overlay_skills_dir(uuid)
    registered = set(get_instance_registered_skills(uuid))

    # 收集：系统 skill（全部）+ 实例/定义层 skill + 服务覆盖 skill
    # 同名时靠列表顺序后写覆盖（overlay 最后写 = 最高优先）
    seen: dict[str, dict] = {}

    for skills_dir in [system_dir, instance_dir, overlay_dir]:
        if not skills_dir or not skills_dir.is_dir():
            continue
        for skill_file in iter_skill_files(skills_dir):
            name = skill_file.parent.name
            fm = _load_frontmatter(skill_file)
            if not skill_matches_platform(fm):
                continue
            if skills_dir == overlay_dir:
                source = "overlay"
            elif skills_dir == instance_dir:
                source = "instance"
            else:
                source = "system"
            seen[name] = {
                "name": fm.get("name", name),
                "description": fm.get("description", ""),
                "version": fm.get("version", ""),
                "source": source,
                # 标记是否在注册列表中
                "registered": name in registered,
            }

    # 只返回已注册的 skill，按 name 排序
    result = []
    for name in sorted(registered):
        if name in seen:
            entry = seen[name]
            # 去掉 registered 字段（内部用）
            entry = {k: v for k, v in entry.items() if k != "registered"}
            result.append(entry)

    return _j({"skills": result})


# ── Tool 2：skill_view ─────────────────────────────────────────────────


def _handle_skill_view(args: dict[str, Any], **kwargs) -> str:
    """加载 skill 完整内容（tier 2）。

    可选加载 references/ 下的关联文件（tier 3）。
    """
    _burn()

    name = args.get("name") or ""
    ref = args.get("reference") or ""

    if not name:
        return tool_error("name is required")

    skill_file = _find_skill(name)
    if not skill_file:
        return tool_error(f"Skill not found: {name}")

    if ref:
        # tier 3：加载 references/ 下的文件
        target = skill_file.parent / ref
        if not target.exists():
            return tool_error(f"File not found in skill '{name}': {ref}")
    else:
        target = skill_file

    try:
        content = target.read_text(encoding="utf-8")
    except Exception as e:
        return tool_error(f"Failed to read {target}: {e}")

    from interfaces.skills import parse_frontmatter
    fm, body = parse_frontmatter(content)

    return _j({
        "name": fm.get("name", name),
        "description": fm.get("description", ""),
        "reference": ref or "SKILL.md",
        "content": body.strip(),
        "path": str(target.relative_to(target.parents[1])),
    })


# ── Register ────────────────────────────────────────────────────────────


registry.register(
    name="skills_list",
    toolset="senses",
    schema={
        "name": "skills_list",
        "description": "列出所有已注册的 skill（name + description）。用于了解有哪些技能可用。需要用 skill 时调 skill_view(name) 看完整说明。",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
    handler=_handle_skills_list,
    check_fn=lambda: True,
    emoji="📖",
)


registry.register(
    name="skill_view",
    toolset="senses",
    schema={
        "name": "skill_view",
        "description": "查看某个 skill 的完整说明。name 是 skill 名称，reference 是可选的子文件路径（references/xxx.md）。",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "skill 名称",
                },
                "reference": {
                    "type": "string",
                    "description": "可选：references/ 目录下的文件名，如 'examples.md'",
                },
            },
            "required": ["name"],
        },
    },
    handler=_handle_skill_view,
    check_fn=lambda: True,
    emoji="📖",
)
