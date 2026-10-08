"""L4 事件注册表 — 以 config/event_types.yaml 为唯一数据源。

所有 12 种事件类型定义在 YAML 文件中，无 Python 硬编码定义或 heartbeat.py 回退。
实例级 prompt 覆写通过热加载在运行时应用到注册表。

事件类型结构（EventTypeDefinition）：
  type_id: 事件标识（如 message、timer、vital_threshold）
  display_name: 显示名称
  description: 描述
  priority: 优先级（越高越优先唤醒）
  prompt_template: 唤醒时注入的 prompt 模板
  trigger_type: 触发类型（external/cron/system）
  debounce_window_s: 防抖窗口（秒）
  merge_policy: 合并策略（latest/accumulate/count）
"""

from __future__ import annotations

import logging
from pathlib import Path

from domain.core.models import EventTriggerType, EventTypeDefinition
from infrastructure.persistence.repositories.events import InMemoryEventRegistry

logger = logging.getLogger(__name__)

_registry = InMemoryEventRegistry()


def _parse_debounce_window(value) -> tuple[int, int]:
    """解析 debounce_window_s 配置，统一返回 (min, max) 的 tuple。

    支持两种格式：
      int:    30 → (30, 30)  固定窗口
      [a, b]: [20, 40] → (20, 40)  范围内随机窗口

    范围窗口的意义：让多实例的 wake 时点自然错峰，避免同时响应
    群消息导致并行 LLM call 抢资源。
    """
    if isinstance(value, (list, tuple)):
        if len(value) >= 2:
            lo, hi = int(value[0]), int(value[1])
            if lo > hi:
                lo, hi = hi, lo
            return (max(0, lo), max(0, hi))
        if len(value) == 1:
            v = max(0, int(value[0]))
            return (v, v)
        return (0, 0)
    try:
        v = int(value)
        return (v, v)
    except (TypeError, ValueError):
        return (0, 0)


def _load_yaml_config() -> dict[str, dict]:
    """从 config/event_types.yaml 加载所有事件类型定义。"""
    try:
        config_path = Path(__file__).resolve().parents[2] / "config" / "event_types.yaml"
        if not config_path.exists():
            logger.warning("event_types.yaml not found at %s", config_path)
            return {}
        import yaml
        with open(config_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return raw.get("event_types", {})
    except Exception as exc:
        logger.warning("failed to load event_types.yaml: %s", exc)
        return {}


def _entry_from_cfg(type_id: str, cfg: dict):
    trigger_type_str = cfg.get("trigger_type", "external")
    try:
        trigger_type = EventTriggerType(trigger_type_str)
    except ValueError:
        trigger_type = EventTriggerType.EXTERNAL
    return EventTypeDefinition(
        type_id=type_id,
        display_name=cfg.get("display_name", type_id),
        trigger_type=trigger_type,
        payload_schema=cfg.get("payload_schema", {}),
        prompt_template=cfg.get("wake_prompt", ""),
        allowed_tools=tuple(cfg.get("allowed_tools", [])),
        context_policy=cfg.get("context_policy", {}),
        auth_policy={},
        description=cfg.get("description", ""),
        priority=int(cfg.get("priority", 5)),
        debounce_window_s=_parse_debounce_window(cfg.get("debounce_window_s", 0)),
        merge_policy=cfg.get("merge_policy", "latest"),
        consumption_policy=cfg.get("consumption_policy", "on_detail"),
        trigger_description=cfg.get("trigger_description", ""),
    )


def register_extension_types() -> int:
    """并入业务扩展事件类型（extension 注册的额外 yaml）。

    可重入：已注册的 type 以扩展定义整体替换（业务类型归业务定义）。
    在 load_extensions() 完成后调用——业务 activate 比本模块首次 import
    晚时，靠这里补齐。
    """
    from infrastructure import extension

    n = 0
    for path in extension.event_type_files():
        try:
            import yaml
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            logger.warning("failed to load extension event types %s: %s", path, exc)
            continue
        for type_id, cfg in (raw.get("event_types") or {}).items():
            if not isinstance(cfg, dict):
                continue
            entry = _entry_from_cfg(type_id, cfg)
            if _registry.get(type_id) is not None:
                _registry._definitions[type_id] = entry
            else:
                try:
                    _registry.register(entry)
                except ValueError:
                    pass
            n += 1
    return n


def _register_all() -> None:
    """从 event_types.yaml 构建注册表——唯一数据源（+ 扩展点业务类型）。"""
    yaml_configs = _load_yaml_config()

    for type_id, cfg in yaml_configs.items():
        try:
            _registry.register(_entry_from_cfg(type_id, cfg))
        except ValueError:
            pass  # idempotent on re-import


_register_all()


def _load_instance_overrides() -> None:
    """从实例的 config.yaml 加载 prompt 覆写并应用到注册表。

    在模块导入时调用，确保实例级 prompt 定制在启动时就生效，
    不需要等到 console 热重载。
    """
    try:
        from infrastructure.config import get_instance_config_path
        cfg_path = get_instance_config_path()
        if not cfg_path.exists():
            return
        import yaml
        with open(cfg_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        overrides = raw.get("prompts_override", {})
        if not isinstance(overrides, dict):
            return
        for type_id, prompt_text in overrides.items():
            definition = _registry.get(type_id)
            if definition and prompt_text:
                from dataclasses import replace
                _registry._definitions[type_id] = replace(definition, prompt_template=str(prompt_text))
    except Exception:
        pass  # best-effort: don't block startup if config is malformed


_load_instance_overrides()


# ─────────────────────────── Registry accessors ────────────────────────────


def get_event_type(type_id: str) -> EventTypeDefinition | None:
    """返回 type_id 的注册定义，未注册返回 None。"""
    return _registry.get(type_id)


def list_event_types() -> tuple[EventTypeDefinition, ...]:
    """返回所有已注册的事件类型定义。"""
    return _registry.list()


def resolve_prompt_template(type_id: str) -> str | None:
    """从注册表中解析 type_id 的 prompt 模板。"""
    definition = _registry.get(type_id)
    if definition and definition.prompt_template:
        return definition.prompt_template
    return None


def resolve_allowed_tools(type_id: str) -> tuple[str, ...]:
    """返回 type_id 的允许工具列表，未注册返回空元组。"""
    definition = _registry.get(type_id)
    if definition:
        return definition.allowed_tools
    return ()


def resolve_event_config(type_id: str) -> dict:
    """返回 type_id 的完整事件配置（priority, debounce_window_s, merge_policy, consumption_policy）。"""
    definition = _registry.get(type_id)
    if not definition:
        return {}
    return {
        "priority": definition.priority,
        "debounce_window_s": definition.debounce_window_s,
        "merge_policy": definition.merge_policy,
        "consumption_policy": definition.consumption_policy,
    }


def validate_event_type(type_id: str, *, raise_on_unknown: bool = False) -> bool:
    """校验 type_id 是否已注册。

    Args:
        type_id: 事件类型字符串。
        raise_on_unknown: True 时未知类型抛 ValueError；False 时仅 warning。
    Returns:
        True 已注册，False 未注册。
    """
    if _registry.get(type_id) is not None:
        return True
    if raise_on_unknown:
        raise ValueError(f"unregistered event type: {type_id!r}")
    logger.warning("unregistered event type emitted: %r — consider registering it", type_id)
    return False


__all__ = [
    "get_event_type",
    "list_event_types",
    "resolve_prompt_template",
    "resolve_allowed_tools",
    "resolve_event_config",
    "validate_event_type",
]
