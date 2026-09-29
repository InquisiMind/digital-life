"""capability 闸 — 运行体能力解析（设计文档 v1.1 特性 4）。

语义：
  - 实例型：全部能力恒开（行为零变化，存量路径不过闸）。
  - 服务型：按 services 注册表 capabilities 字段；未声明的能力取服务默认
    （vitals/routines/perception 默认关）。显式 {"vitals": true} 可再打开
    （将来"拟人化节奏"只改配置）。

消费方：
  - domain/vital/state.py：精力变更（consume/nurture/touch/tick 持久化）跳过
  - infrastructure/ai/agent.py：capability 门控工具不出现在 tools 数组
  - 未来：感知、交易、语音等任何实例型能力按同一开关关闭
"""

from __future__ import annotations

# 服务型默认关闭的能力（实例型不受此表影响——恒开）
SERVICE_DEFAULT_CAPABILITIES: dict[str, bool] = {
    "vitals": False,
    "routines": False,
    "perception": False,
}

# 工具名 → 所属 capability：该能力关闭时，工具不出现在 tools 数组
# （dispatch 仍可到达——与 schema_visible 退役机制不同，这是"躯体没这个器官"）
CAPABILITY_GATED_TOOLS: dict[str, str] = {
    "sense_vitals": "vitals",
    "sense_nurture_log": "vitals",
    "sense_schedule": "routines",
}


def capability_enabled(cap: str, runtime_id: str | None = None) -> bool:
    """该运行体是否启用某能力。实例恒 True；服务按注册表（默认关）。"""
    from infrastructure.config import get_app_instance_id
    from infrastructure.persistence.services_registry import (
        lookup_service,
        resolve_service_def,
    )

    rid = runtime_id or get_app_instance_id()
    if not rid or resolve_service_def(rid) is None:
        return True  # 实例型：全部能力在（零变化）
    svc = lookup_service(rid) or {}
    caps = svc.get("capabilities")
    caps = caps if isinstance(caps, dict) else {}
    if cap in caps:
        return bool(caps[cap])
    return SERVICE_DEFAULT_CAPABILITIES.get(cap, False)


def gated_tool_names(runtime_id: str | None = None) -> set[str]:
    """当前运行体应隐藏的 capability 门控工具名集合。"""
    blocked_caps = {
        cap
        for cap in set(CAPABILITY_GATED_TOOLS.values())
        if not capability_enabled(cap, runtime_id)
    }
    if not blocked_caps:
        return set()
    return {
        tool
        for tool, cap in CAPABILITY_GATED_TOOLS.items()
        if cap in blocked_caps
    }
