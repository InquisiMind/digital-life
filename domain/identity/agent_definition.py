"""服务级覆盖层（overlay）：apps/{def}/services/{sid}/ 下的私有资产。

与框架 D1=B（定义层资产同 def 共享）兼容的纯增量设计：

- overlay 文件**存在则优先**，不存在完全走定义层——实例型/未覆盖
  服务的行为零变化；
- persona / 附加指令（EXTRA_PROMPT.md） / skills / 头像等元数据
  （agent.json）都住在服务私有目录，不触碰定义层共享资产。
"""

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def service_overlay_dir(service_id: str) -> Optional[Path]:
    """服务 ID → 其私有运行时目录；非服务（实例型/未知 ID）返回 None。"""
    if not service_id:
        return None
    from infrastructure.persistence import services_registry

    try:
        if services_registry.resolve_service_def(service_id):
            from infrastructure.config import resolve_runtime_dir

            return resolve_runtime_dir(service_id)
    except Exception:  # noqa: BLE001 — 注册表不可用时按无覆盖处理
        logger.debug("overlay dir lookup failed for %s", service_id[:12])
        return None
    return None


# ── persona（服务级人设覆盖） ───────────────────────────────────────────


def persona_path(service_id: str) -> Optional[Path]:
    d = service_overlay_dir(service_id)
    return (d / "persona" / "LIFE_PERSONA.md") if d else None


def read_persona(service_id: str) -> Optional[str]:
    p = persona_path(service_id)
    if p is None or not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


def write_persona(service_id: str, content: str) -> bool:
    p = persona_path(service_id)
    if p is None:
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return True


# ── L4 生命周期段落覆盖（默认=引擎常量，覆盖后整段替换） ────────────────


def l4_path(service_id: str) -> Optional[Path]:
    d = service_overlay_dir(service_id)
    return (d / "persona" / "L4_PROMPT.md") if d else None


def read_l4(service_id: str) -> Optional[str]:
    p = l4_path(service_id)
    if p is not None and p.exists():
        try:
            return p.read_text(encoding="utf-8")
        except OSError:
            return None
    dp = _def_persona_file(service_id, "L4_PROMPT.md")
    if dp is not None and dp.exists():
        try:
            return dp.read_text(encoding="utf-8")
        except OSError:
            return None
    return None


def write_l4(service_id: str, content: str) -> bool:
    p = l4_path(service_id)
    if p is None:
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    if not content.strip():
        p.unlink(missing_ok=True)
        return True
    p.write_text(content, encoding="utf-8")
    return True


# ── 附加指令（追加到 system prompt 尾部的服务级段落） ──────────────────


def extra_prompt_path(service_id: str) -> Optional[Path]:
    d = service_overlay_dir(service_id)
    return (d / "persona" / "EXTRA_PROMPT.md") if d else None


def read_extra_prompt(service_id: str) -> str:
    p = extra_prompt_path(service_id)
    if p is not None and p.exists():
        try:
            return p.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    # 定义层回退（配置作用域=agent 类型：同定义全部服务生效）
    dp = _def_persona_file(service_id, "EXTRA_PROMPT.md")
    if dp is not None and dp.exists():
        try:
            return dp.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return ""


def _def_persona_file(service_id: str, name: str) -> Optional[Path]:
    """服务 → 定义层 persona 目录下的文件；非服务返回 None。"""
    from infrastructure.persistence import services_registry

    def_id = services_registry.resolve_service_def(service_id) or ""
    if not def_id:
        return None
    from infrastructure.config import get_project_root

    return get_project_root() / "apps" / def_id / "persona" / name


def def_persona_dir(def_id: str) -> Optional[Path]:
    """定义层 persona 目录（agent 类型的配置作用域）。"""
    from infrastructure.config import get_project_root

    d = get_project_root() / "apps" / def_id / "persona"
    return d if d.parent.is_dir() else None


def write_extra_prompt(service_id: str, content: str) -> bool:
    p = extra_prompt_path(service_id)
    if p is None:
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    if not content.strip():
        p.unlink(missing_ok=True)  # 清空 = 删除覆盖，回到无附加态
        return True
    p.write_text(content, encoding="utf-8")
    return True


# ── agent 元数据（头像等展示性配置） ────────────────────────────────────


def _meta_path(service_id: str) -> Optional[Path]:
    d = service_overlay_dir(service_id)
    return (d / "persona" / "agent.json") if d else None


def read_agent_meta(service_id: str) -> dict:
    p = _meta_path(service_id)
    if p is not None and p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    dp = _def_persona_file(service_id, "agent.json")
    if dp is not None and dp.exists():
        try:
            return json.loads(dp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    return {}


def write_agent_meta(service_id: str, updates: dict) -> bool:
    p = _meta_path(service_id)
    if p is None:
        return False
    meta = read_agent_meta(service_id)
    meta.update({k: v for k, v in updates.items() if v is not None})
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


# ── skills（服务级技能目录：同名覆盖定义层） ────────────────────────────


def service_skills_dir(service_id: str) -> Optional[Path]:
    d = service_overlay_dir(service_id)
    return (d / "skills") if d else None


# ── 定义层直写（配置作用域=agent 类型，同定义全部服务生效） ─────────────


def write_def_file(def_id: str, name: str, content: str) -> bool:
    d = def_persona_dir(def_id)
    if d is None:
        return False
    d.mkdir(parents=True, exist_ok=True)
    if not content.strip():
        (d / name).unlink(missing_ok=True)
        return True
    (d / name).write_text(content, encoding="utf-8")
    return True


def read_def_file(def_id: str, name: str) -> Optional[str]:
    d = def_persona_dir(def_id)
    if d is None:
        return None
    p = d / name
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


def read_def_agent_meta(def_id: str) -> dict:
    raw = read_def_file(def_id, "agent.json")
    if raw is None:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def write_def_agent_meta(def_id: str, updates: dict) -> bool:
    meta = read_def_agent_meta(def_id)
    meta.update({k: v for k, v in updates.items() if v is not None})
    d = def_persona_dir(def_id)
    if d is None:
        return False
    d.mkdir(parents=True, exist_ok=True)
    (d / "agent.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def def_skills_dir(def_id: str) -> Optional[Path]:
    from infrastructure.config import get_project_root

    d = get_project_root() / "apps" / def_id / "skills"
    return d if d.parent.is_dir() else None
