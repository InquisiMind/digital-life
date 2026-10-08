"""基座扩展点 — 业务包的注册入口。

基座只认注册，不认业务：工具模块、HTTP 路由、调度任务、事件类型文件、
服务事件闸、工具面策略、服务工作区、休息上下文——全部由业务包在
activate() 里声明式登记，基座在组合根（agent 装配 / HTTP server /
worker loop / 事件注册表 / emit 闸 / rest）统一消费。

发现约定：``business/*/activate.py`` 存在即 import 并调用其中的
``activate()``（幂等，加载失败只 WARNING——业务挂了基座照跑）。
基座对"业务是什么"的全部知识就是这条约定。
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("digital_life.extension")

# ── 注册表（业务 activate() 写入）──────────────────────────────────────
_TOOL_MODULES: list[str] = []
_API_ROUTERS: list[str] = []          # "pkg.mod:add_routes"
_SCHEDULER_JOBS: list[str] = []       # "pkg.mod:job"（worker loop 每轮调用，业务自节流）
_EVENT_TYPE_FILES: list[str] = []     # 额外事件类型 yaml（并入事件注册表）
_EVENT_GATES: list[str] = []          # "pkg.mod:gate(svc, kind) -> (ok, reason)"
_TOOL_FACE_FILTERS: list[str] = []    # "pkg.mod:filter(names) -> names"
_WORKSPACE_PROVIDERS: list[str] = []  # "pkg.mod:dir(service_id) -> Path|None"
_REST_CARD_SECTIONS: list[str] = []   # "pkg.mod:section(runtime_id) -> str"
_REST_SILENT_NOTES: list[str] = []    # "pkg.mod:note(runtime_id) -> str|None"
_ADMIN_ROUTERS: list[str] = []        # "pkg.mod:register_admin_routes(router)"
_SERVICE_ENRICHERS: list[str] = []    # "pkg.mod:enrich(rows)"

_loaded = False


def register_tool_module(module_name: str) -> None:
    _TOOL_MODULES.append(module_name)


def register_api_router(ref: str) -> None:
    """ref = "pkg.mod:function"，function(app) 挂路由。"""
    _API_ROUTERS.append(ref)


def register_scheduler_job(ref: str) -> None:
    _SCHEDULER_JOBS.append(ref)


def register_event_type_file(path: str) -> None:
    _EVENT_TYPE_FILES.append(path)


def register_event_gate(ref: str) -> None:
    _EVENT_GATES.append(ref)


def register_tool_face_filter(ref: str) -> None:
    _TOOL_FACE_FILTERS.append(ref)


def register_workspace_provider(ref: str) -> None:
    _WORKSPACE_PROVIDERS.append(ref)


def register_rest_card_section(ref: str) -> None:
    _REST_CARD_SECTIONS.append(ref)


def register_rest_silent_note(ref: str) -> None:
    _REST_SILENT_NOTES.append(ref)


def register_admin_router(ref: str) -> None:
    """ref = "pkg.mod:register_admin_routes(router)"——业务管理端点注入
    基座 /api/admin 子应用（基座在 add_subapp 冻结前调用）。"""
    _ADMIN_ROUTERS.append(ref)


def register_service_enricher(ref: str) -> None:
    """ref = "pkg.mod:enrich(rows)"——给基座服务总览行补业务字段（项目名等）。"""
    _SERVICE_ENRICHERS.append(ref)


def _resolve(ref: str) -> Callable:
    mod_name, _, attr = ref.partition(":")
    module = importlib.import_module(mod_name)
    return getattr(module, attr)


def _business_root() -> Path:
    # 以 infrastructure 包自身定位仓库根——不受实例上下文/测试对
    # get_project_root 的重定向影响（业务包是仓库资产，不是实例数据）。
    return Path(__file__).resolve().parents[1] / "business"


def load_extensions() -> None:
    """发现并激活业务包（幂等）。"""
    global _loaded
    if _loaded:
        return
    _loaded = True
    root = _business_root()
    if not root.is_dir():
        return
    for activate_py in sorted(root.glob("*/activate.py")):
        try:
            mod = importlib.import_module(
                f"business.{activate_py.parent.name}.activate"
            )
            if hasattr(mod, "activate"):
                mod.activate()
                logger.info("extension activated: business/%s", activate_py.parent.name)
        except Exception:
            logger.warning(
                "extension activate failed: business/%s（基座继续运行）",
                activate_py.parent.name, exc_info=True,
            )
    # 业务事件类型并入注册表（activate 比注册表首次 import 晚的场合靠这里补齐）
    try:
        from domain.lifecycle.event_registry import register_extension_types

        n = register_extension_types()
        if n:
            logger.info("extension event types merged: %d", n)
    except Exception:
        logger.warning("extension event types merge failed", exc_info=True)


# ── 组合根消费口 ───────────────────────────────────────────────────────


def tool_modules() -> list[str]:
    load_extensions()
    return list(_TOOL_MODULES)


def mount_api(app) -> None:
    load_extensions()
    for ref in _API_ROUTERS:
        try:
            _resolve(ref)(app)
        except Exception:
            logger.warning("extension api mount failed: %s（跳过）", ref, exc_info=True)


def run_scheduler_jobs() -> None:
    load_extensions()
    for ref in _SCHEDULER_JOBS:
        try:
            _resolve(ref)()
        except Exception:
            logger.warning("extension scheduler job failed: %s", ref, exc_info=True)


def event_type_files() -> list[Path]:
    load_extensions()
    return [Path(p) for p in _EVENT_TYPE_FILES if Path(p).exists()]


def check_event_gates(svc: dict, kind: str) -> tuple[bool, str]:
    """依次跑业务事件闸：首个拒绝即拒（False, reason）。全过=(True, "")。"""
    load_extensions()
    for ref in _EVENT_GATES:
        try:
            ok, reason = _resolve(ref)(svc, kind)
            if not ok:
                return False, reason or "event_gate_denied"
        except Exception:
            logger.warning("extension event gate failed: %s（放行）", ref, exc_info=True)
    return True, ""


def apply_tool_face_filters(names: list[str]) -> list[str]:
    load_extensions()
    out = names
    for ref in _TOOL_FACE_FILTERS:
        try:
            out = _resolve(ref)(out)
        except Exception:
            logger.warning("extension tool face filter failed: %s（跳过）", ref, exc_info=True)
    return out


def service_workspace_dir(service_id: str) -> Optional[Path]:
    """业务提供的挂项目服务工作区（None=基座默认私有 workspace）。"""
    load_extensions()
    for ref in _WORKSPACE_PROVIDERS:
        try:
            d = _resolve(ref)(service_id)
            if d is not None:
                return d
        except Exception:
            logger.warning("extension workspace provider failed: %s", ref, exc_info=True)
    return None


def rest_card_sections(runtime_id: str) -> list[str]:
    load_extensions()
    sections = []
    for ref in _REST_CARD_SECTIONS:
        try:
            s = _resolve(ref)(runtime_id)
            if s:
                sections.append(s)
        except Exception:
            logger.warning("extension rest card section failed: %s", ref, exc_info=True)
    return sections


def rest_silent_notes(runtime_id: str) -> Optional[str]:
    """业务判定该运行体应无闹钟静眠时的提示（首个非空即中）。"""
    load_extensions()
    for ref in _REST_SILENT_NOTES:
        try:
            note = _resolve(ref)(runtime_id)
            if note:
                return note
        except Exception:
            logger.warning("extension rest silent note failed: %s", ref, exc_info=True)
    return None


def mount_admin_routers(router) -> None:
    """业务管理端点注入基座 /api/admin 子应用（冻结前由基座调用）。"""
    load_extensions()
    for ref in _ADMIN_ROUTERS:
        try:
            _resolve(ref)(router)
        except Exception:
            logger.warning("extension admin router failed: %s（跳过）", ref, exc_info=True)


def enrich_service_rows(rows: list[dict]) -> None:
    """业务 enricher 就地补全服务总览行（project_name 等）。"""
    load_extensions()
    for ref in _SERVICE_ENRICHERS:
        try:
            _resolve(ref)(rows)
        except Exception:
            logger.warning("extension service enricher failed: %s", ref, exc_info=True)
