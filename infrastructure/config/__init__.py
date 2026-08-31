"""配置路径工具 — L4 运行时的实例作用域路径解析。

两个 ContextVar 系统（多实例隔离的关键基础设施）：
  1. _instance_id_var — DB 路径隔离（apps/{uuid}/data/）
     用于 set_current_instance_id() / get_app_instance_id()
     所有路径函数（get_instance_state_db_path 等）都基于此解析

  2. _current_session_id_var — 当前唤醒会话 ID
     用于 consume_event() 记录 consumed_by_session_id（日历聚合展示用）

为什么用 ContextVar 而非环境变量：
  os.environ 是进程全局的，cron 循环切换实例时会覆盖。
  ContextVar 在线程间传播——agent 子线程继承正确的实例 ID，
  即使主线程已切换到下一个实例也不会受影响。

实例发现（动态）：
  扫 apps/*/config/app.yaml 里的 display_name 字段构建注册表。
  不再持久化到 apps/instances.yaml（2026-06-18 已废弃）。
  discover_active_instances() 返回 active=true 的实例列表。
"""

from __future__ import annotations

import contextvars
import os
import re
import uuid as _uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml

# Per-thread/coroutine instance ID context.
# os.environ is process-global and gets overwritten by the cron loop when it
# switches between instances. ContextVar propagates to child threads — so
# agent threads inherit the correct instance ID even when the main thread
# moves on to the next instance.
_instance_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "digital_life_instance_id", default=""
)

# Current session ID — set by scheduler when a wake session starts,
# consumed by events.consume_event() to record which session consumed the event.
_current_session_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "digital_life_session_id", default=""
)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def _looks_like_uuid(value: str) -> bool:
    return bool(_UUID_RE.match(value))


# ── Registry ──────────────────────────────────────────────────────────


def _load_registry() -> dict[str, dict]:
    """Scan apps/*/config/app.yaml to build {uuid: {display_name, ...}}.

    实例发现的唯一事实源是 apps/<uuid>/config/app.yaml 存在 + 含 display_name。
    每次调用都动态扫描，不写盘也不缓存到文件。
    """
    return _rebuild_registry_from_apps()


def _rebuild_registry_from_apps() -> dict[str, dict]:
    """Scan apps/ — entry is an instance iff it has config/app.yaml with display_name.

    透出供前端实例卡片使用：display_name + 以下展示元数据（app.yaml 里全部用
    `.get(...)` 读取，缺失则给空默认，对老 app.yaml 零破坏）：
      - avatar        — 头像/动图 URL（相对 apps/{id}/assets/ 或绝对 URL）
      - accent_color  — 主题色 #rrggbb
      - tagline       — 一句话个性/职责描述
      - sort_order    — 卡片排列序号（缺省 100）
    """
    apps_dir = get_project_root() / "apps"
    if not apps_dir.exists():
        return {}
    raw_entries: list[tuple[str, dict]] = []
    for entry in sorted(apps_dir.iterdir()):
        if not entry.is_dir():
            continue
        cfg_path = entry / "config" / "app.yaml"
        if not cfg_path.exists():
            continue
        uuid_str = entry.name
        display_name = uuid_str[:8]
        avatar = ""
        accent_color = ""
        tagline = ""
        sort_order = 100
        try:
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            if isinstance(cfg, dict):
                if cfg.get("display_name"):
                    display_name = cfg["display_name"]
                avatar = str(cfg.get("avatar") or "").strip()
                accent_color = str(cfg.get("accent_color") or "").strip()
                tagline = str(cfg.get("tagline") or "").strip()
                try:
                    sort_order = int(cfg.get("sort_order") or 100)
                except (TypeError, ValueError):
                    sort_order = 100
        except Exception:
            pass
        raw_entries.append(
            (
                uuid_str,
                {
                    "display_name": display_name,
                    "legacy_name": display_name,
                    "created_at": "",
                    "avatar": avatar,
                    "accent_color": accent_color,
                    "tagline": tagline,
                    "sort_order": sort_order,
                },
            )
        )
    # 按 sort_order 升序构建 dict（同序号时 fallback 到 UUID 字符序）
    raw_entries.sort(key=lambda kv: (kv[1]["sort_order"], kv[0]))
    return dict(raw_entries)


def _save_registry(registry: dict[str, dict]) -> None:
    """No-op: registry is derived from apps/*/config/app.yaml on each load.

    2026-06-18 起不再持久化 instances.yaml —— display_name 是 app.yaml 的
    单一字段，没有任何写盘的必要。本函数仅保留兼容已有的三个调用方。
    """
    return None


# ── Instance ID resolution ────────────────────────────────────────────


_default_fallback_warned = False


def _default_instance_id() -> str:
    """Return the default instance UUID (first in registry, or legacy fallback).

    8/26 跨库写入 bug 修复（case_20260826）：静默 fallback 曾导致 env/ContextVar
    缺失的调用路径无声路由进 1 号实例库。保留 fallback 行为（master 启动路径
    依赖），但首次触发时打 WARNING 日志——变"静默"为"有声"，
    所有变体 bug 的触发瞬间都会留痕。
    """
    global _default_fallback_warned
    if not _default_fallback_warned:
        import inspect
        caller = ""
        for frame_info in inspect.stack()[1:]:
            fname = frame_info.filename
            if "/infrastructure/config/" not in fname:
                caller = f"{fname.split('/')[-1]}:{frame_info.lineno}"
                break
        import logging
        logging.getLogger("digital_life.config").warning(
            "instance fallback engaged: no explicit instance context "
            "(env/ContextVar), defaulting to registry-first. caller=%s "
            "[case_20260826 cross-db write bug] — if this fires during "
            "add_cognition/write paths, the write may land in the WRONG "
            "instance library", caller,
        )
        _default_fallback_warned = True
    registry = _load_registry()
    if registry:
        return next(iter(registry))
    # Legacy: scan apps/ for persona dirs
    apps_dir = get_project_root() / "apps"
    if apps_dir.exists():
        for entry in sorted(apps_dir.iterdir()):
            if entry.is_dir() and (entry / "persona").is_dir():
                return entry.name
    return "zero"


def resolve_instance_id(raw: str) -> str:
    """Resolve a display name or UUID to the canonical instance UUID.

    Resolution order:
    1. Already a UUID present in registry → return as-is
    2. Matches a display_name in registry → return that UUID
    3. Legacy: directory name under apps/ with persona/ → return as-is (transition)
    4. UUID not in registry but directory exists → return as-is
    5. Unrecognized → return as-is (caller handles)
    """
    if not raw:
        return _default_instance_id()

    raw = raw.strip("/")

    # 1. UUID already in registry
    if _looks_like_uuid(raw):
        registry = _load_registry()
        if raw in registry:
            return raw

    # 2. Look up by display_name
    registry = _load_registry()
    for uuid_key, meta in registry.items():
        if meta.get("display_name") == raw:
            return uuid_key

    # 3. Legacy fallback: directory exists under apps/
    apps_dir = get_project_root() / "apps"
    if (apps_dir / raw / "persona").is_dir():
        return raw

    # 4. UUID not in registry but directory exists
    if _looks_like_uuid(raw) and (apps_dir / raw).is_dir():
        return raw

    return raw


def get_app_instance_id(explicit: str | None = None) -> str:
    """Return the active digital employee instance UUID.

    Resolution order: explicit arg → ContextVar → env var → default.
    All inputs are resolved through resolve_instance_id() for UUID normalisation.
    """
    if explicit:
        return resolve_instance_id(explicit.strip("/") or "zero")
    ctx_val = _instance_id_var.get()
    if ctx_val:
        return resolve_instance_id(ctx_val)
    raw = (
        os.environ.get("DIGITAL_LIFE_INSTANCE_ID")
        or os.environ.get("L4_AGENT_ID")
        or os.environ.get("DIGITAL_LIFE_EMPLOYEE_ID")
        or ""
    ).strip("/")
    if raw:
        return resolve_instance_id(raw)
    return _default_instance_id()


def get_instance_display_name(instance_id: str | None = None) -> str:
    """Return the human-readable display_name for an instance UUID."""
    uuid_key = get_app_instance_id(instance_id)
    registry = _load_registry()
    meta = registry.get(uuid_key, {})
    return meta.get("display_name") or uuid_key


# ── Registry mutations ─────────────────────────────────────────────────


def register_instance(uuid_str: str, display_name: str) -> None:
    """Add a new instance to the registry."""
    registry = _load_registry()
    registry[uuid_str] = {
        "display_name": display_name,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_registry(registry)


def update_instance_display_name(uuid_str: str, display_name: str) -> None:
    """Update the display_name for an existing instance."""
    registry = _load_registry()
    if uuid_str not in registry:
        raise KeyError(f"Instance not found in registry: {uuid_str}")
    registry[uuid_str]["display_name"] = display_name
    _save_registry(registry)


def remove_instance_from_registry(uuid_str: str) -> None:
    """Remove an instance from the registry (does not delete files)."""
    registry = _load_registry()
    registry.pop(uuid_str, None)
    _save_registry(registry)


# ── ContextVar helpers ─────────────────────────────────────────────────


def set_current_instance_id(instance_id: str) -> contextvars.Token:
    """Set the current instance ID for the calling thread/coroutine subtree."""
    return _instance_id_var.set(instance_id)


def reset_current_instance_id(token: contextvars.Token) -> None:
    """Reset the instance ID to the previous value."""
    _instance_id_var.reset(token)


def set_current_session_id(session_id: str) -> contextvars.Token:
    """Set the current session ID for the calling thread/coroutine subtree."""
    return _current_session_id_var.set(session_id)


def reset_current_session_id(token: contextvars.Token) -> None:
    """Reset the session ID to the previous value."""
    _current_session_id_var.reset(token)


def get_current_session_id() -> str:
    """Return the current session ID, or empty string if not set."""
    return _current_session_id_var.get()


# ── Project root ──────────────────────────────────────────────────────


def get_project_root() -> Path:
    """Return the repository root."""
    return Path(__file__).resolve().parents[2]


# ── Instance-scoped paths (canonical) ──────────────────────────────────


def get_instance_dir(instance_id: str | None = None) -> Path:
    """Return the app directory for a digital employee instance: apps/{uuid}/"""
    return get_project_root() / "apps" / get_app_instance_id(instance_id)


def get_instance_data_dir(instance_id: str | None = None) -> Path:
    """Return the runtime data directory for an instance: apps/{uuid}/data/"""
    return get_instance_dir(instance_id) / "data"


def get_instance_config_dir(instance_id: str | None = None) -> Path:
    """Return the instance config override directory: apps/{uuid}/config/"""
    return get_instance_dir(instance_id) / "config"


def get_instance_state_db_path(instance_id: str | None = None) -> Path:
    """Return the state DB path: apps/{uuid}/data/state.db"""
    return get_instance_data_dir(instance_id) / "state.db"


def get_instance_sessions_db_path(instance_id: str | None = None) -> Path:
    """Return the sessions DB path: apps/{uuid}/data/sessions.db"""
    return get_instance_data_dir(instance_id) / "sessions.db"


def get_instance_memories_dir(instance_id: str | None = None) -> Path:
    """Return the memories directory: apps/{uuid}/data/memories/"""
    return get_instance_data_dir(instance_id) / "memories"


def get_instance_persona_path(instance_id: str | None = None) -> Path:
    """Return the runtime persona prompt path: apps/{uuid}/persona/LIFE_PERSONA.md"""
    return get_instance_dir(instance_id) / "persona" / "LIFE_PERSONA.md"


def get_instance_skills_dir(instance_id: str | None = None) -> Path:
    """Return the skills directory: apps/{uuid}/skills/"""
    return get_instance_dir(instance_id) / "skills"


def get_instance_config_path(instance_id: str | None = None) -> Path:
    """Return the canonical instance config path: apps/{uuid}/config/app.yaml.

    2026-06-17 重构后实例配置统一在 config/app.yaml（旧的 data/config.yaml 已废弃并删除）。
    读写都指向这里——prompts_override / set_instance_active / config_center 写到这里，
    load_runtime_config(path) / 日志读这里。维持单一信息源，避免再分裂。
    """
    return get_instance_dir(instance_id) / "config" / "app.yaml"


def get_instance_app_config_path(instance_id: str | None = None) -> Path:
    """Return the app config path: apps/{uuid}/config/app.yaml (飞书路由等实例配置)。"""
    return get_instance_dir(instance_id) / "config" / "app.yaml"


def get_instance_env_path(instance_id: str | None = None) -> Path:
    """Return the instance secrets env file: apps/{uuid}/config/secrets.env

    实际文件在 apps/<id>/config/secrets.env（不在 data/.env）——这是
    infrastructure/ai/config.py:60 / digital-life init / 各处 worker loader 共同
    认定的事实源。之前这里误返 data/.env，导致 ConfigCenter 写 GLM_API_KEY /
    FEISHU_APP_SECRET 等凭证时落到不存在的路径 → 改了不生效。
    """
    iid = instance_id or get_app_instance_id()
    apps_root = get_project_root() / "apps"
    if iid:
        return apps_root / iid / "config" / "secrets.env"
    return apps_root / "default" / "config" / "secrets.env"


# ── Global config paths ───────────────────────────────────────────────


def get_global_config_dir() -> Path:
    """Return the global config directory: config/"""
    return get_project_root() / "config"


def get_global_event_types_path() -> Path:
    """Return the global event types YAML: config/event_types.yaml"""
    return get_global_config_dir() / "event_types.yaml"


def get_global_routines_path() -> Path:
    """Return the global routines YAML: config/routines.yaml"""
    return get_global_config_dir() / "routines.yaml"


def get_instance_routines_path(instance_id: str | None = None) -> Path:
    """Return per-instance routines YAML: apps/{iid}/config/routines.yaml.

    作息 2026-06-29 下沉到实例级后的路径。实例存在该文件时，routine_scheduler
    用实例版作息（完全覆盖全局）；不存在时回退全局 get_global_routines_path()
    （由 routine_scheduler.load_routines 决定回退逻辑，本函数只返路径）。

    与 get_instance_config_path 同级风格——作息是一个独立的有结构清单，
    不挤在 app.yaml 的扁平配置里。
    """
    # get_instance_config_path 返 app.yaml 文件，.parent 得到 config/ 目录。
    return get_instance_config_path(instance_id).parent / "routines.yaml"


def get_global_default_config_path() -> Path:
    """Return the global default config: config/default.yaml"""
    return get_global_config_dir() / "default.yaml"


# ── Legacy aliases (delegate to instance-scoped with default id) ──────


def get_runtime_home() -> Path:
    """Legacy: delegates to get_instance_data_dir()."""
    return get_instance_data_dir()


def get_runtime_config_path() -> Path:
    """Legacy: delegates to get_instance_config_path()."""
    return get_instance_config_path()


def get_runtime_env_path() -> Path:
    """Legacy: delegates to get_instance_env_path()."""
    return get_instance_env_path()


def get_runtime_memories_dir() -> Path:
    """Legacy: delegates to get_instance_memories_dir()."""
    return get_instance_memories_dir()


def get_runtime_state_db_path() -> Path:
    """Legacy: delegates to get_instance_state_db_path()."""
    return get_instance_state_db_path()


def get_app_instance_dir(instance_id: str | None = None) -> Path:
    """Legacy: delegates to get_instance_dir()."""
    return get_instance_dir(instance_id)


def get_app_persona_path(instance_id: str | None = None) -> Path:
    """Legacy: delegates to get_instance_persona_path()."""
    return get_instance_persona_path(instance_id)


def get_app_skills_dir(instance_id: str | None = None) -> Path:
    """Legacy: delegates to get_instance_skills_dir()."""
    return get_instance_skills_dir(instance_id)


# ── Instance discovery ────────────────────────────────────────────────


def discover_instances() -> list[str]:
    """Return UUIDs of all registered instances (from registry or legacy scan)."""
    registry = _load_registry()
    if registry:
        return list(registry.keys())
    # Legacy fallback: scan apps/ for persona/ subdirectories
    apps_dir = get_project_root() / "apps"
    if not apps_dir.exists():
        return []
    instances = []
    for entry in sorted(apps_dir.iterdir()):
        if entry.is_dir() and (entry / "persona").is_dir():
            instances.append(entry.name)
    return instances


def is_instance_active(instance_id: str) -> bool:
    """Check whether an instance is active (should be started on boot).

    Single source of truth: apps/<id>/config/app.yaml → active field.
    Default True if instance dir exists (未显式置 active 的老实例回退为启用)。
    """
    app_yaml = get_instance_config_path(instance_id)
    if app_yaml.exists():
        try:
            cfg = yaml.safe_load(app_yaml.read_text(encoding="utf-8")) or {}
            if isinstance(cfg, dict) and "active" in cfg:
                return bool(cfg["active"])
        except Exception:
            pass

    instance_dir = get_project_root() / "apps" / instance_id
    return instance_dir.is_dir()


def set_instance_active(instance_id: str, active: bool) -> None:
    """Set the active flag for an instance. True = start on boot, False = paused."""
    cfg_path = get_instance_config_path(instance_id)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg = {}
    if cfg_path.exists():
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    cfg["active"] = active
    cfg_path.write_text(
        yaml.dump(cfg, default_flow_style=False, allow_unicode=True), encoding="utf-8"
    )


def discover_active_instances() -> list[str]:
    """Return only instances whose active flag is True."""
    return [i for i in discover_instances() if is_instance_active(i)]


__all__ = [
    # Canonical (prefer these)
    "discover_active_instances",
    "discover_instances",
    "get_app_instance_id",
    "get_current_session_id",
    "get_global_config_dir",
    "get_global_default_config_path",
    "get_global_event_types_path",
    "get_global_routines_path",
    "get_instance_config_dir",
    "get_instance_config_path",
    "get_instance_app_config_path",
    "get_instance_data_dir",
    "get_instance_dir",
    "get_instance_display_name",
    "get_instance_env_path",
    "get_instance_memories_dir",
    "get_instance_persona_path",
    "get_instance_sessions_db_path",
    "get_instance_skills_dir",
    "get_instance_state_db_path",
    "get_project_root",
    "register_instance",
    "remove_instance_from_registry",
    "reset_current_instance_id",
    "reset_current_session_id",
    "resolve_instance_id",
    "set_current_instance_id",
    "set_current_session_id",
    "update_instance_display_name",
    # Legacy (delegate to instance-scoped)
    "get_app_instance_dir",
    "get_app_persona_path",
    "get_app_skills_dir",
    "get_runtime_config_path",
    "get_runtime_env_path",
    "get_runtime_home",
    "get_runtime_memories_dir",
    "get_runtime_state_db_path",
]
