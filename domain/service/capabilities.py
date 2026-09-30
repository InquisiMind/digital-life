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
    # 内部项目 + 全局待办是实例型的（设计文档 D2/6.3）：服务型项目是
    # CustomerProject（刀 4 引入，届时按项目打开）。未闸时服务的记忆索引
    # 会吸进全部内部项目与跨实例待办（9/29 双服务审计现场抓获）。
    "projects": False,
}

# 工具名 → 所属 capability：该能力关闭时，工具不出现在 tools 数组
# （dispatch 仍可到达——与 schema_visible 退役机制不同，这是"躯体没这个器官"）
CAPABILITY_GATED_TOOLS: dict[str, str] = {
    "sense_vitals": "vitals",
    "sense_nurture_log": "vitals",
    "sense_schedule": "routines",
    # todo/project 全家（domain/todos/tools.py + domain/project/tools.py）
    "todo": "projects",
    "todo_plan": "projects",
    "todo_note": "projects",
    "todo_trigger": "projects",
    "sense_todos": "projects",
    "task_from_deliverable": "projects",
    "sense_projects": "projects",
    "sense_project_detail": "projects",
    "sense_project_todos": "projects",
    "project_todo": "projects",
    "project_deliver": "projects",
    "project_info": "projects",
    "project_bootstrap": "projects",
}

# 服务型专属工具（刀 4b 项目协作原语）：实例默认不装载（工程可放行）——
# 实例间协作走 broadcast 对等链，语义不同。
SERVICE_PROJECT_TOOLS: frozenset[str] = frozenset({
    "sense_project_peers",
    "send_chat_message",
    "project_file_list",
    "project_file_read",
    "project_file_write",
    "deliver_to_shared",
    "project_todo_create",
    "project_todo_list",
    "project_todo_update",
})

SERVICE_ONLY_TOOLS = SERVICE_PROJECT_TOOLS


def service_only_tools_hidden(runtime_id: str | None = None) -> bool:
    """当前运行体是否应隐藏服务专属工具（= 是实例）。"""
    from infrastructure.config import get_app_instance_id
    from infrastructure.persistence.services_registry import resolve_service_def

    rid = runtime_id or get_app_instance_id()
    if not rid:
        return True
    return resolve_service_def(rid) is None


def project_tools_allowed(runtime_id: str | None = None) -> set[str] | None:
    """当前服务的岗位工具面（模版角色 → services.tools_json）。

    None = 未配置（默认全量项目工具集）；集合 = 只留交集内的项目工具。
    实例返回 None（配合 service_only_tools_hidden 整组隐藏）。
    """
    from infrastructure.config import get_app_instance_id
    from infrastructure.persistence.services_registry import (
        lookup_service,
        resolve_service_def,
    )

    rid = runtime_id or get_app_instance_id()
    if not rid or resolve_service_def(rid) is None:
        return None
    svc = lookup_service(rid) or {}
    tools = svc.get("tools")
    if isinstance(tools, list) and tools:
        return set(tools)
    # 类型级默认：定义层 app.yaml 的 tools（模版角色未显式收紧时按 agent 类型默认面）
    try:
        import yaml as _yaml

        from infrastructure.config import get_project_root
        from infrastructure.persistence.services_registry import (
            resolve_service_def,
        )

        def_id = resolve_service_def(rid) or ""
        if def_id:
            cfg = get_project_root() / "apps" / def_id / "config" / "app.yaml"
            data = _yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
            dtools = data.get("tools")
            if isinstance(dtools, list) and dtools:
                return set(str(x) for x in dtools)
    except Exception:  # noqa: BLE001 — 定义层默认读取失败按全量
        pass
    return None


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
