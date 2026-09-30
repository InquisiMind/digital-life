"""系统级 Skill 工具函数。

Skill 是系统级定义（interfaces/skills/），实例通过 app.yaml 注册使用。
参考 Anthropic progressive disclosure 模型。

核心工具（可直接复用 Hermes 代码）：
- parse_frontmatter()：解析 YAML frontmatter
- skill_matches_platform()：平台过滤

注册与渲染：
- get_instance_registered_skills()：读 app.yaml skills 字段
- render_skill_index()：渲染 skill 索引供 system prompt 注入
"""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# ── 目录路径 ──────────────────────────────────────────────────────────────

SKILLS_DIR = Path(__file__).parent.resolve()


def get_system_skills_dir() -> Path:
    """返回系统级 skill 目录：interfaces/skills/"""
    return SKILLS_DIR


def get_instance_skills_dir(instance_id: str | None = None) -> Path:
    """返回实例级 skill 目录：apps/{uuid}/skills/"""
    from infrastructure.config import get_instance_skills_dir as _cfg
    return _cfg(instance_id)


# ── Frontmatter 解析（复用 Hermes） ──────────────────────────────────────


def parse_frontmatter(content: str):
    """解析 YAML frontmatter + body。

    返回：(frontmatter_dict, body_str)
    frontmatter 解析失败时返回空 dict，body 为原文。
    """
    frontmatter: dict[str, Any] = {}
    body = content

    if not content.startswith("---"):
        return frontmatter, body

    end_match = re.search(r"\n---\s*\n", content[3:])
    if not end_match:
        return frontmatter, body

    yaml_content = content[3 : end_match.start() + 3]
    body = content[end_match.end() + 3 :]

    try:
        parsed = yaml.safe_load(yaml_content)
        if isinstance(parsed, dict):
            frontmatter = parsed
    except Exception:
        for line in yaml_content.strip().split("\n"):
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            frontmatter[key.strip()] = value.strip()

    return frontmatter, body


# ── 平台过滤 ──────────────────────────────────────────────────────────────


_PLATFORM_MAP = {
    "macos": "darwin",
    "linux": "linux",
    "windows": "win32",
}


def skill_matches_platform(frontmatter: dict[str, Any]) -> bool:
    """skill 的 platforms 声明是否与当前 OS 兼容。

    platforms 为空或缺失 → 全平台兼容。
    """
    platforms = frontmatter.get("platforms")
    if not platforms:
        return True
    if not isinstance(platforms, list):
        platforms = [platforms]
    current = sys.platform
    for platform in platforms:
        normalized = str(platform).lower().strip()
        mapped = _PLATFORM_MAP.get(normalized, normalized)
        if current.startswith(mapped):
            return True
    return False


# ── Skill 文件遍历 ────────────────────────────────────────────────────────


EXCLUDED_SKILL_DIRS = frozenset((".git", ".github", ".hub"))


def iter_skill_files(skills_dir: Path, filename: str = "SKILL.md"):
    """遍历 skills_dir，返回所有匹配 filename 的路径（递归，排除特定目录）。"""
    matches = []
    for root, dirs, files in os.walk(skills_dir):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_SKILL_DIRS]
        if filename in files:
            matches.append(Path(root) / filename)
    return sorted(matches, key=lambda p: str(p.relative_to(skills_dir)))


# ── 实例注册 ──────────────────────────────────────────────────────────────


def get_instance_registered_skills(instance_id: str | None = None) -> list[str]:
    """读取注册 skill 名称列表：定义层 app.yaml skills 字段 + 服务 overlay 注册。

    服务级注册写在服务私有 agent.json 的 skills 数组（同名去重、
    overlay 追加）——定义层 app.yaml 不动，保持同 def 共享语义。
    """
    from infrastructure.config import (
        get_instance_app_config_path,
        get_app_instance_id,
        get_global_config_dir,
    )

    uuid = get_app_instance_id(instance_id)
    app_yaml = get_instance_app_config_path(uuid)

    names: list[str] = []
    if app_yaml.exists():
        try:
            cfg = yaml.safe_load(app_yaml.read_text(encoding="utf-8")) or {}
            skills = cfg.get("skills")
            if isinstance(skills, list):
                names = [str(s).strip() for s in skills if str(s).strip()]
        except Exception as e:
            logger.warning("Failed to load skills from %s: %s", app_yaml, e)

    # 服务 overlay 注册（仅服务实例存在；异常时忽略，不影响原列表）
    try:
        from domain.service.overlay import read_agent_meta

        extra = read_agent_meta(uuid).get("skills")
        if isinstance(extra, list):
            seen = set(names)
            for s in extra:
                s = str(s).strip()
                if s and s not in seen:
                    names.append(s)
                    seen.add(s)
    except Exception:  # noqa: BLE001
        pass

    return names


# ── Skill 索引渲染（供 system prompt） ────────────────────────────────────


def get_overlay_skills_dir(instance_id: str | None = None) -> Path | None:
    """服务级 skill 覆盖目录（apps/{def}/services/{sid}/skills/）。

    仅服务实例存在；实例型/未建覆盖目录返回 None——各发现点把它
    插到查找链最前（同名覆盖定义层），不存在则行为不变。
    """
    from infrastructure.config import get_app_instance_id

    uuid = get_app_instance_id(instance_id)
    try:
        from domain.service.overlay import service_skills_dir

        d = service_skills_dir(uuid)
        return d if (d is not None and d.is_dir()) else None
    except Exception:  # noqa: BLE001 — overlay 层异常按无覆盖处理
        return None


def load_skill_metadata(skill_name: str, instance_id: str | None = None) -> dict[str, Any] | None:
    """加载指定 skill 的 frontmatter。

    查找顺序：服务覆盖 skills/ → 实例(定义层) skills/ → 系统 skills/。
    找不到或 platform 不兼容返回 None。
    """
    from infrastructure.config import get_app_instance_id

    uuid = get_app_instance_id(instance_id)
    instance_dir = get_instance_skills_dir(uuid)
    system_dir = get_system_skills_dir()

    # 覆盖层优先，其次实例，最后系统
    bases = [b for b in [get_overlay_skills_dir(uuid), instance_dir, system_dir] if b]
    for base in bases:
        skill_dir = base / skill_name
        skill_file = skill_dir / "SKILL.md"
        if skill_file.exists():
            try:
                content = skill_file.read_text(encoding="utf-8")
                fm, _ = parse_frontmatter(content)
                if not skill_matches_platform(fm):
                    return None
                return fm
            except Exception as e:
                logger.debug("Failed to load skill %s from %s: %s", skill_name, skill_file, e)
    return None


def render_skill_index(instance_id: str | None = None) -> str:
    """渲染已注册 skill 的索引段落（供 system prompt 注入）。

    返回 markdown 格式的技能列表，或空字符串（无注册 skill）。

    精简约束：每条 description 截到 60 字符。详情模型需要时调 ``skill_view(name)``
    拿全文（progressive disclosure）。description 长度超过 60 时按整词截断 + 省略号。
    """
    registered = get_instance_registered_skills(instance_id)
    if not registered:
        return ""

    items = []
    for name in sorted(registered):
        meta = load_skill_metadata(name, instance_id)
        if meta is None:
            # skill 不存在或 platform 不兼容，跳过
            continue
        desc = (meta.get("description", "") or "").strip()
        if not desc:
            continue
        if len(desc) > 60:
            # 按整词截断（避免半个中文/英文词），加省略号
            cut = desc[:60]
            # 找最后一个空格或中文边界
            for sep in (" ", "，", "、", "——"):
                idx = cut.rfind(sep)
                if idx > 30:
                    cut = cut[:idx]
                    break
            desc = cut + "…"
        items.append(f"- `{name}`：{desc}")

    if not items:
        return ""

    skill_index = "\n".join([
        "**技能**（详情调 `skill_view(name)`）：",
        *items,
    ])
    return skill_index
