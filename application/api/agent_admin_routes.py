"""Agent 管理后台 API：profile（人设/附加指令/头像）、skills CRUD、工具面 schema、新建 agent、项目模版。

调试口子（内部）：给 /customer 项目配置页与 /services 服务台提供
agent 深度配置能力。读写一律走服务级 overlay（domain.service.overlay），
定义层共享资产只读不写——见 D1=B 兼容说明。
"""

import json
import logging
import re
from pathlib import Path

from aiohttp import web

logger = logging.getLogger(__name__)

_ROUTER = web.Application()

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _json(data, status: int = 200) -> web.Response:
    resp = web.json_response(data, status=status)
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _svc(sid: str) -> dict | None:
    from infrastructure.persistence import services_registry

    return services_registry.lookup_service(sid)


# ── profile：基本信息（名称 / 头像 / 人设 / 附加指令） ───────────────────


async def handle_profile_get(request: web.Request) -> web.Response:
    sid = request.match_info["service_id"]
    svc = _svc(sid)
    if svc is None:
        return _json({"ok": False, "error": "服务不存在"}, 404)
    from domain.service import overlay

    persona_overlay = overlay.read_persona(sid)
    # 人设展示：覆盖优先，否则定义层共享原文（只读参考）
    persona_content = persona_overlay
    persona_source = "overlay" if persona_overlay is not None else "def"
    if persona_overlay is None:
        from domain.memory.context.selectors.persona import (
            get_life_persona_path,
            MISSING_LIFE_PERSONA,
        )

        try:
            persona_content = get_life_persona_path(sid).read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            persona_content = ""
        if not persona_content:
            persona_source = "none"
            persona_content = ""

    meta = overlay.read_agent_meta(sid)
    return _json({
        "ok": True,
        "name": svc.get("display_name") or "",
        "avatar": meta.get("avatar") or "",
        "def_id": svc.get("agent_def_id") or "",
        "status": svc.get("status") or "",
        "persona": {"content": persona_content, "source": persona_source},
        "extra_prompt": overlay.read_extra_prompt(sid),
    })


async def handle_profile_put(request: web.Request) -> web.Response:
    sid = request.match_info["service_id"]
    if _svc(sid) is None:
        return _json({"ok": False, "error": "服务不存在"}, 404)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    from domain.service import overlay
    from infrastructure.persistence import services_registry

    touched: list[str] = []
    if "name" in body:
        name = str(body["name"] or "").strip()[:30]
        if name:
            services_registry.update_service_fields(sid, display_name=name)
            touched.append("name")
    if "avatar" in body:
        overlay.write_agent_meta(sid, {"avatar": str(body["avatar"] or "").strip()[:16]})
        touched.append("avatar")
    if "persona" in body:
        content = str(body["persona"] or "").strip()
        if content:
            overlay.write_persona(sid, content)
        else:
            p = overlay.persona_path(sid)
            if p is not None:
                p.unlink(missing_ok=True)  # 清空 = 回到定义层共享人设
        touched.append("persona")
    if "extra_prompt" in body:
        overlay.write_extra_prompt(sid, str(body["extra_prompt"] or ""))
        touched.append("extra_prompt")
    if not touched:
        return _json({"ok": False, "error": "无可更新字段"}, 400)
    logger.info("AGENT_PROFILE_UPDATED %s fields=%s", sid[:12], touched)
    return _json({"ok": True, "fields": touched})


# ── skills：列表 / 读取 / 覆盖写入 / 删除（全部落在服务 overlay） ─────────


def _collect_skills(sid: str) -> list[dict]:
    from domain.service import overlay
    from interfaces.skills import (
        get_instance_registered_skills,
        iter_skill_files,
        parse_frontmatter,
    )
    from infrastructure.config import get_instance_skills_dir
    from infrastructure.persistence import services_registry

    def_id = services_registry.resolve_service_def(sid) or ""
    registered = set(get_instance_registered_skills(sid))
    from interfaces.skills import get_system_skills_dir

    out: dict[str, dict] = {}
    # 顺序：系统 → 定义层 → overlay（后写覆盖）
    layers = [
        ("system", get_system_skills_dir()),
        ("def", get_instance_skills_dir(sid)),
        ("overlay", overlay.service_skills_dir(sid)),
    ]
    for source, base in layers:
        if base is None or not Path(base).is_dir():
            continue
        for f in iter_skill_files(Path(base)):
            name = f.parent.name
            try:
                fm, _ = parse_frontmatter(f.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                fm = {}
            out[name] = {
                "name": name,
                "description": fm.get("description", ""),
                "version": fm.get("version", ""),
                "source": source,
                "registered": name in registered,
            }
    return sorted(out.values(), key=lambda x: x["name"])


async def handle_skills_list(request: web.Request) -> web.Response:
    sid = request.match_info["service_id"]
    if _svc(sid) is None:
        return _json({"ok": False, "error": "服务不存在"}, 404)
    return _json({"ok": True, "skills": _collect_skills(sid)})


def _skill_search_paths(sid: str, name: str) -> list[tuple[str, Path]]:
    from domain.service import overlay
    from interfaces.skills import get_system_skills_dir
    from infrastructure.config import get_instance_skills_dir

    return [
        ("overlay", overlay.service_skills_dir(sid) / name / "SKILL.md"),
        ("def", get_instance_skills_dir(sid) / name / "SKILL.md"),
        ("system", get_system_skills_dir() / name / "SKILL.md"),
    ]


async def handle_skill_get(request: web.Request) -> web.Response:
    sid = request.match_info["service_id"]
    name = request.match_info["name"]
    if _svc(sid) is None:
        return _json({"ok": False, "error": "服务不存在"}, 404)
    for source, p in _skill_search_paths(sid, name):
        if p.exists():
            return _json({"ok": True, "name": name, "source": source,
                          "content": p.read_text(encoding="utf-8")})
    return _json({"ok": False, "error": "skill 不存在"}, 404)


async def handle_skill_put(request: web.Request) -> web.Response:
    sid = request.match_info["service_id"]
    name = request.match_info["name"]
    if _svc(sid) is None:
        return _json({"ok": False, "error": "服务不存在"}, 404)
    if not _SAFE_NAME.match(name):
        return _json({"ok": False, "error": "skill 名仅限字母数字与 _ -"}, 400)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    content = str(body.get("content") or "").strip()
    if not content:
        return _json({"ok": False, "error": "content 不能为空"}, 400)
    from domain.service import overlay

    sdir = overlay.service_skills_dir(sid)
    if sdir is None:
        return _json({"ok": False, "error": "非服务实例"}, 400)
    target = sdir / name / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    # overlay 注册（agent.json skills 数组，去重追加）
    meta = overlay.read_agent_meta(sid)
    skills = [s for s in meta.get("skills", []) if isinstance(s, str)]
    if name not in skills:
        skills.append(name)
    overlay.write_agent_meta(sid, {"skills": skills})
    logger.info("AGENT_SKILL_WRITTEN %s skill=%s", sid[:12], name)
    return _json({"ok": True, "name": name, "source": "overlay"})


async def handle_skill_delete(request: web.Request) -> web.Response:
    sid = request.match_info["service_id"]
    name = request.match_info["name"]
    if _svc(sid) is None:
        return _json({"ok": False, "error": "服务不存在"}, 404)
    from domain.service import overlay

    sdir = overlay.service_skills_dir(sid)
    target = (sdir / name) if sdir else None
    if target is None or not target.exists():
        return _json({"ok": False, "error": "该 skill 无服务级覆盖可删"}, 404)
    import shutil

    shutil.rmtree(target)
    meta = overlay.read_agent_meta(sid)
    skills = [s for s in meta.get("skills", []) if isinstance(s, str) and s != name]
    overlay.write_agent_meta(sid, {"skills": skills})
    logger.info("AGENT_SKILL_DELETED %s skill=%s", sid[:12], name)
    return _json({"ok": True})


# ── tools：全量工具清单 + schema（订阅开关沿用 services/{sid}/config） ───


async def handle_tools_list(_request: web.Request) -> web.Response:
    from interfaces.tools.registry import registry

    tools = []
    for name in sorted(registry.get_all_tool_names()):
        schema = registry.get_schema(name) or {}
        tools.append({
            "name": name,
            "toolset": registry.get_toolset_for_tool(name) or "",
            "description": (schema.get("description") or "")[:160],
            "schema": schema,
        })
    return _json({"ok": True, "tools": tools})


# ── 新建独立 agent（不挂项目；加入项目走 projects/{pid}/members） ────────


async def handle_agent_create(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    def_id = str(body.get("def") or "").strip()
    name = str(body.get("name") or "").strip()[:30]
    if not def_id or not name:
        return _json({"ok": False, "error": "def 和名称必填"}, 400)
    from domain.service.registry import create_service
    from infrastructure.config import get_project_root

    def_dir = get_project_root() / "apps" / def_id
    if not def_dir.is_dir():
        return _json({"ok": False, "error": f"定义不存在: {def_id}"}, 404)
    try:
        sid = create_service(def_id, project_id="", display_name=name)
    except Exception as exc:  # noqa: BLE001
        return _json({"ok": False, "error": str(exc)}, 400)
    logger.info("AGENT_CREATED def=%s service=%s name=%s", def_id, sid[:12], name)
    return _json({"ok": True, "service_id": sid})


# ── 项目模版：列表 / 读取 / 保存（config/project_templates/*.yaml） ───────


def _templates_dir() -> Path:
    from infrastructure.config import get_project_root

    return get_project_root() / "config" / "project_templates"


async def handle_templates_list(_request: web.Request) -> web.Response:
    items = []
    for f in sorted(_templates_dir().glob("*.yaml")):
        raw = f.read_text(encoding="utf-8")
        name = ""
        m = re.search(r"^name:\s*(.+)$", raw, re.M)
        if m:
            name = m.group(1).strip()
        items.append({"id": f.stem, "name": name or f.stem, "size": len(raw)})
    return _json({"ok": True, "templates": items})


async def handle_template_get(request: web.Request) -> web.Response:
    tid = request.match_info["template_id"]
    f = _templates_dir() / f"{tid}.yaml"
    if not _SAFE_NAME.match(tid) or not f.exists():
        return _json({"ok": False, "error": "模版不存在"}, 404)
    return _json({"ok": True, "id": tid, "raw": f.read_text(encoding="utf-8")})


async def handle_template_put(request: web.Request) -> web.Response:
    tid = request.match_info["template_id"]
    if not _SAFE_NAME.match(tid):
        return _json({"ok": False, "error": "模版 id 非法"}, 400)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    raw = str(body.get("raw") or "")
    if not raw.strip():
        return _json({"ok": False, "error": "raw 不能为空"}, 400)
    # 语法闸：保存前先过 YAML 解析，防止写坏模版砸了建项目链路
    try:
        import yaml

        yaml.safe_load(raw)
    except Exception as exc:  # noqa: BLE001
        return _json({"ok": False, "error": f"YAML 语法错误: {exc}"}, 400)
    f = _templates_dir() / f"{tid}.yaml"
    existed = f.exists()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(raw, encoding="utf-8")
    logger.info("TEMPLATE_SAVED id=%s existed=%s", tid, existed)
    return _json({"ok": True})


def register_into(router: web.Application) -> None:
    """把 agent 深度配置路由并进 services_admin 的 /api/admin/ 子应用。

    必须在 add_subapp（freeze）之前调用；aiohttp 不支持同前缀两个 subapp。
    """
    router.router.add_get("/agents/{service_id}/profile", handle_profile_get)
    router.router.add_put("/agents/{service_id}/profile", handle_profile_put)
    router.router.add_get("/agents/{service_id}/skills", handle_skills_list)
    router.router.add_get("/agents/{service_id}/skills/{name}", handle_skill_get)
    router.router.add_put("/agents/{service_id}/skills/{name}", handle_skill_put)
    router.router.add_delete("/agents/{service_id}/skills/{name}", handle_skill_delete)
    router.router.add_get("/tools", handle_tools_list)
    router.router.add_post("/agents", handle_agent_create)
    router.router.add_get("/templates", handle_templates_list)
    router.router.add_get("/templates/{template_id}", handle_template_get)
    router.router.add_put("/templates/{template_id}", handle_template_put)
    logger.info("Agent admin routes registered into /api/admin/ (shared router)")
