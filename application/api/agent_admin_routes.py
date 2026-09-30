"""Agent 类型（定义层）配置 API + 工具 schema + 新建 agent + 项目模版。

配置作用域 = agent 类型（apps/{def}/）：人设 / 附加指令 / L4 覆盖 /
头像 / skills / 类型默认工具面，全部直接读写定义层，作用于同定义的
所有服务实例（新建的也生效）。服务级 overlay 保留为引擎兼容层
（存在则优先），但配置入口不再写它。

调试口子（内部），/api/admin/ 前缀（并入 services_admin 的 _ROUTER）。
"""

import json
import logging
import re
from pathlib import Path

import yaml
from aiohttp import web

logger = logging.getLogger(__name__)

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _json(data, status: int = 200) -> web.Response:
    resp = web.json_response(data, status=status)
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _def_dir(def_id: str) -> Path:
    from infrastructure.config import get_project_root

    return get_project_root() / "apps" / def_id


def _def_app_yaml(def_id: str) -> Path:
    return _def_dir(def_id) / "config" / "app.yaml"


def _def_exists(def_id: str) -> bool:
    return _SAFE_NAME.match(def_id) and _def_app_yaml(def_id).exists()


def _load_def_cfg(def_id: str) -> dict:
    try:
        return yaml.safe_load(_def_app_yaml(def_id).read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}


def _save_def_cfg(def_id: str, cfg: dict) -> None:
    p = _def_app_yaml(def_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")


# ── profile（名称 / 头像 / 人设 / 附加指令 / L4）—— 作用域：定义层 ──────


async def handle_def_profile_get(request: web.Request) -> web.Response:
    def_id = request.match_info["def_id"]
    if not _def_exists(def_id):
        return _json({"ok": False, "error": "定义不存在"}, 404)
    from domain.service import overlay

    cfg = _load_def_cfg(def_id)
    persona_path = _def_dir(def_id) / "persona" / "LIFE_PERSONA.md"
    try:
        persona = persona_path.read_text(encoding="utf-8")
    except OSError:
        persona = ""
    try:
        from domain.identity.system_prompts import L4_LIFECYCLE_PROMPT as _L4
    except Exception:  # noqa: BLE001
        _L4 = ""
    l4 = overlay.read_def_file(def_id, "L4_PROMPT.md")
    return _json({
        "ok": True,
        "def_id": def_id,
        "name": cfg.get("display_name") or def_id,
        "avatar": overlay.read_def_agent_meta(def_id).get("avatar") or "",
        "persona": persona,
        "extra_prompt": (overlay.read_def_file(def_id, "EXTRA_PROMPT.md") or "").strip(),
        "l4": {"content": l4 if l4 is not None else _L4,
               "source": "overlay" if l4 is not None else "engine"},
    })


async def handle_def_profile_put(request: web.Request) -> web.Response:
    def_id = request.match_info["def_id"]
    if not _def_exists(def_id):
        return _json({"ok": False, "error": "定义不存在"}, 404)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    from domain.service import overlay

    touched: list[str] = []
    cfg = _load_def_cfg(def_id)
    if "name" in body:
        name = str(body["name"] or "").strip()[:30]
        if name:
            cfg["display_name"] = name
            touched.append("name")
    if "avatar" in body:
        overlay.write_def_agent_meta(def_id, {"avatar": str(body["avatar"] or "").strip()[:16]})
        touched.append("avatar")
    if "persona" in body:
        content = str(body["persona"] or "").strip()
        p = _def_dir(def_id) / "persona" / "LIFE_PERSONA.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        touched.append("persona")
    if "extra_prompt" in body:
        overlay.write_def_file(def_id, "EXTRA_PROMPT.md", str(body["extra_prompt"] or ""))
        touched.append("extra_prompt")
    if "l4" in body:
        overlay.write_def_file(def_id, "L4_PROMPT.md", str(body["l4"] or ""))
        touched.append("l4")
    if "name" in body:
        _save_def_cfg(def_id, cfg)
    if not touched:
        return _json({"ok": False, "error": "无可更新字段"}, 400)
    logger.info("AGENT_DEF_PROFILE_UPDATED def=%s fields=%s", def_id, touched)
    return _json({"ok": True, "fields": touched})


# ── skills（定义层 apps/{def}/skills/ + app.yaml 注册） ─────────────────


def _collect_def_skills(def_id: str) -> list[dict]:
    from interfaces.skills import (
        get_system_skills_dir,
        iter_skill_files,
        parse_frontmatter,
    )
    from domain.service.overlay import def_skills_dir

    cfg = _load_def_cfg(def_id)
    registered = set(cfg.get("skills") or [])
    out: dict[str, dict] = {}
    layers = [
        ("system", get_system_skills_dir()),
        ("def", def_skills_dir(def_id)),
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


async def handle_def_skills_list(request: web.Request) -> web.Response:
    def_id = request.match_info["def_id"]
    if not _def_exists(def_id):
        return _json({"ok": False, "error": "定义不存在"}, 404)
    return _json({"ok": True, "skills": _collect_def_skills(def_id)})


async def handle_def_skill_get(request: web.Request) -> web.Response:
    def_id = request.match_info["def_id"]
    name = request.match_info["name"]
    if not _def_exists(def_id):
        return _json({"ok": False, "error": "定义不存在"}, 404)
    from domain.service.overlay import def_skills_dir

    p = (def_skills_dir(def_id) or _def_dir(def_id) / "skills") / name / "SKILL.md"
    if p.exists():
        return _json({"ok": True, "name": name, "source": "def",
                      "content": p.read_text(encoding="utf-8")})
    from interfaces.skills import get_system_skills_dir

    sp = get_system_skills_dir() / name / "SKILL.md"
    if sp.exists():
        return _json({"ok": True, "name": name, "source": "system",
                      "content": sp.read_text(encoding="utf-8")})
    return _json({"ok": False, "error": "skill 不存在"}, 404)


def _set_def_registered(def_id: str, name: str, on: bool) -> None:
    cfg = _load_def_cfg(def_id)
    skills = [str(x) for x in (cfg.get("skills") or [])]
    if on and name not in skills:
        skills.append(name)
    elif not on and name in skills:
        skills = [x for x in skills if x != name]
    cfg["skills"] = skills
    _save_def_cfg(def_id, cfg)


async def handle_def_skill_put(request: web.Request) -> web.Response:
    def_id = request.match_info["def_id"]
    name = request.match_info["name"]
    if not _def_exists(def_id):
        return _json({"ok": False, "error": "定义不存在"}, 404)
    if not _SAFE_NAME.match(name):
        return _json({"ok": False, "error": "skill 名仅限字母数字与 _ -"}, 400)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    content = str(body.get("content") or "").strip()
    if not content:
        return _json({"ok": False, "error": "content 不能为空"}, 400)
    from domain.service.overlay import def_skills_dir

    sdir = def_skills_dir(def_id) or (_def_dir(def_id) / "skills")
    target = sdir / name / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _set_def_registered(def_id, name, True)
    logger.info("AGENT_DEF_SKILL_WRITTEN def=%s skill=%s", def_id, name)
    return _json({"ok": True, "name": name, "source": "def"})


async def handle_def_skill_delete(request: web.Request) -> web.Response:
    def_id = request.match_info["def_id"]
    name = request.match_info["name"]
    if not _def_exists(def_id):
        return _json({"ok": False, "error": "定义不存在"}, 404)
    from domain.service.overlay import def_skills_dir

    sdir = def_skills_dir(def_id)
    target = (sdir / name) if sdir else None
    if target is None or not target.exists():
        return _json({"ok": False, "error": "定义层无此 skill"}, 404)
    import shutil

    shutil.rmtree(target)
    _set_def_registered(def_id, name, False)
    logger.info("AGENT_DEF_SKILL_DELETED def=%s skill=%s", def_id, name)
    return _json({"ok": True})


# ── 类型默认工具面（app.yaml tools；实例工具面仍由模版角色决定） ─────────


async def handle_def_tools_get(request: web.Request) -> web.Response:
    def_id = request.match_info["def_id"]
    if not _def_exists(def_id):
        return _json({"ok": False, "error": "定义不存在"}, 404)
    cfg = _load_def_cfg(def_id)
    return _json({"ok": True, "tools": cfg.get("tools") if isinstance(cfg.get("tools"), list) else None})


async def handle_def_tools_put(request: web.Request) -> web.Response:
    def_id = request.match_info["def_id"]
    if not _def_exists(def_id):
        return _json({"ok": False, "error": "定义不存在"}, 404)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    cfg = _load_def_cfg(def_id)
    if "tools" in body:
        tools = body["tools"]
        if tools is None:
            cfg.pop("tools", None)
        elif isinstance(tools, list):
            cfg["tools"] = [str(x)[:60] for x in tools][:40]
        else:
            return _json({"ok": False, "error": "tools 需数组或 null"}, 400)
        _save_def_cfg(def_id, cfg)
        logger.info("AGENT_DEF_TOOLS_UPDATED def=%s n=%s", def_id,
                    len(cfg.get("tools") or []) if cfg.get("tools") is not None else "clear")
        return _json({"ok": True, "tools": cfg.get("tools")})
    return _json({"ok": False, "error": "缺少 tools 字段"}, 400)


# ── 工具 schema（registry 全量） ─────────────────────────────────────────


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


# ── 新建独立 agent（不挂项目；加入项目走模版角色或项目成员端点） ────────


async def handle_def_create(request: web.Request) -> web.Response:
    """POST /api/admin/agent-defs — 新建 agent 类型（定义层）。

    配置管理是类型域：新建的应是"类型"（可被项目模版引用为角色），
    服务实例只由项目实例化产生。目录骨架：config/app.yaml（标记
    runtime_kind: definition + 默认模型段抄自现有定义）+ persona 模板
    初稿 + 空 skills/。密钥不入 app.yaml（沿用环境/secrets 机制）。
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    name = str(body.get("name") or "").strip()[:30]
    def_id = str(body.get("def_id") or "").strip()
    if not name:
        return _json({"ok": False, "error": "名称必填"}, 400)
    if not def_id:
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24]
        import uuid as _uuid

        def_id = slug or f"agent-{_uuid.uuid4().hex[:6]}"  # 中文名 slug 为空时随机短 id
    if not _SAFE_NAME.match(def_id):
        return _json({"ok": False, "error": "ID 仅限字母数字与 _ -"}, 400)
    d = _def_dir(def_id)
    if d.exists():
        return _json({"ok": False, "error": f"类型已存在: {def_id}"}, 400)

    # 默认模型段抄自任一现有定义（同机同端点，密钥仍在环境层）
    model_cfg = {
        "name": "glm-5.3", "provider": "glm",
        "base_url": "https://open.bigmodel.cn/api/coding/paas/v4",
    }
    from infrastructure.config import get_project_root

    _apps = get_project_root() / "apps"
    for existing in sorted(_apps.iterdir()) if _apps.is_dir() else []:
        try:
            cfg = yaml.safe_load((existing / "config" / "app.yaml").read_text(encoding="utf-8")) or {}
            if str(cfg.get("runtime_kind") or "") == "definition" and isinstance(cfg.get("model"), dict):
                model_cfg = cfg["model"]
                break
        except Exception:  # noqa: BLE001
            continue

    (d / "config").mkdir(parents=True, exist_ok=True)
    (d / "config" / "app.yaml").write_text(yaml.safe_dump({
        "runtime_kind": "definition",
        "active": False,
        "display_name": name,
        "model": model_cfg,
        "channels": {},
        "skills": [],
    }, allow_unicode=True, sort_keys=False), encoding="utf-8")
    (d / "persona").mkdir(parents=True, exist_ok=True)
    (d / "persona" / "LIFE_PERSONA.md").write_text(
        f"# {name}\n\n"
        "在这里写这一类 agent 的人设：身份、职责、行为习惯、输出风格。\n"
        "保存即对同类型全部实例生效（实例由项目模版实例化产生）。\n",
        encoding="utf-8")
    (d / "skills").mkdir(parents=True, exist_ok=True)
    logger.info("AGENT_DEF_CREATED def=%s name=%s", def_id, name)
    return _json({"ok": True, "def_id": def_id})


# ── 项目模版：列表 / 读取 / 保存（raw + structured） ─────────────────────


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
    raw = f.read_text(encoding="utf-8")
    try:
        parsed = yaml.safe_load(raw) or {}
    except Exception:  # noqa: BLE001
        parsed = {}
    return _json({"ok": True, "id": tid, "raw": raw, "parsed": parsed})


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
    try:
        yaml.safe_load(raw)
    except Exception as exc:  # noqa: BLE001
        return _json({"ok": False, "error": f"YAML 语法错误: {exc}"}, 400)
    f = _templates_dir() / f"{tid}.yaml"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(raw, encoding="utf-8")
    logger.info("TEMPLATE_SAVED id=%s existed=%s", tid, f.exists())
    return _json({"ok": True})


async def handle_template_put_structured(request: web.Request) -> web.Response:
    # PUT /api/admin/templates/{tid}/structured — 结构化保存（表单编辑器）。
    tid = request.match_info["template_id"]
    if not _SAFE_NAME.match(tid):
        return _json({"ok": False, "error": "模版 id 非法"}, 400)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    tpl = body.get("template")
    if not isinstance(tpl, dict):
        return _json({"ok": False, "error": "缺少 template 对象"}, 400)

    roles = tpl.get("roles")
    if not isinstance(roles, list) or not roles:
        return _json({"ok": False, "error": "roles 必须非空"}, 400)
    pm_seen = False
    for r in roles:
        if not isinstance(r, dict) or not str(r.get("def") or "").strip():
            return _json({"ok": False, "error": "每个成员的 def 必填"}, 400)
        if r.get("pm"):
            if pm_seen:
                return _json({"ok": False, "error": "pm 角色只能有一个"}, 400)
            pm_seen = True
    todos = tpl.get("initial_todos")
    if not isinstance(todos, list):
        todos = []
    role_names = {str(r.get("name") or r.get("def") or "") for r in roles}
    for t in todos:
        if not isinstance(t, dict) or not str(t.get("title") or "").strip():
            return _json({"ok": False, "error": "预设任务每项需 title"}, 400)
        ar = str(t.get("assign_role") or "")
        if ar and ar not in role_names:
            return _json({"ok": False, "error": f"预设任务的执行角色不存在: {ar}"}, 400)
        if t.get("kind") not in (None, "task", "milestone"):
            return _json({"ok": False, "error": "kind 仅 task / milestone"}, 400)

    out = {
        "name": str(tpl.get("name") or tid)[:60],
        "watchdog": bool(tpl.get("watchdog")),
        "description": str(tpl.get("description") or "")[:500],
        "roles": [
            {
                "def": str(r.get("def")),
                "name": str(r.get("name") or r.get("def"))[:30],
                **({"pm": True} if r.get("pm") else {}),
                **({"tools": [str(x) for x in r.get("tools")]}
                   if isinstance(r.get("tools"), list) else {}),
                **({"subscriptions": [str(x) for x in r.get("subscriptions")]}
                   if isinstance(r.get("subscriptions"), list) else {}),
                **({"capabilities": r.get("capabilities")}
                   if isinstance(r.get("capabilities"), dict) else {}),
            }
            for r in roles
        ],
        "initial_todos": [
            {
                "title": str(t.get("title"))[:200],
                **({"detail": str(t.get("detail"))[:1000]} if t.get("detail") else {}),
                **({"assign_role": str(t.get("assign_role"))} if t.get("assign_role") else {}),
                **({"kind": t.get("kind")} if t.get("kind") else {}),
            }
            for t in todos
        ],
    }
    raw = yaml.safe_dump(out, allow_unicode=True, sort_keys=False)
    f = _templates_dir() / f"{tid}.yaml"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(raw, encoding="utf-8")
    logger.info("TEMPLATE_SAVED_STRUCTURED id=%s", tid)
    return _json({"ok": True, "raw": raw})


def register_into(router: web.Application) -> None:
    """并进 services_admin 的 /api/admin/ 子应用（须在 add_subapp freeze 之前）。"""
    router.router.add_get("/agent-defs/{def_id}/profile", handle_def_profile_get)
    router.router.add_put("/agent-defs/{def_id}/profile", handle_def_profile_put)
    router.router.add_get("/agent-defs/{def_id}/skills", handle_def_skills_list)
    router.router.add_get("/agent-defs/{def_id}/skills/{name}", handle_def_skill_get)
    router.router.add_put("/agent-defs/{def_id}/skills/{name}", handle_def_skill_put)
    router.router.add_delete("/agent-defs/{def_id}/skills/{name}", handle_def_skill_delete)
    router.router.add_get("/agent-defs/{def_id}/tools", handle_def_tools_get)
    router.router.add_put("/agent-defs/{def_id}/tools", handle_def_tools_put)
    router.router.add_get("/tools", handle_tools_list)
    router.router.add_post("/agent-defs", handle_def_create)
    router.router.add_get("/templates", handle_templates_list)
    router.router.add_get("/templates/{template_id}", handle_template_get)
    router.router.add_put("/templates/{template_id}", handle_template_put)
    router.router.add_put("/templates/{template_id}/structured", handle_template_put_structured)
    logger.info("Agent-def admin routes registered into /api/admin/")
