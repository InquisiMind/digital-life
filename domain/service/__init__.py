"""服务型运行体（service）领域模块。

服务型是与实例型并列的一等 agent 运行形态（基座能力，不认识具体业务）：
事件信箱 → 唤醒 → 工具面/事件闸，运行体按 agent 定义实例化。
概念三层：agent 定义（模版）→ 服务（运行体）→ 项目（任务容器，基座只认
目录与注册表契约；业务语义——如咨询的项目管理——由 business/{app} 经
扩展点挂载。首个租户：元理 AI 战略咨询平台（business/consulting）。
"""

from domain.service.registry import (
    SERVICE_PASSTHROUGH_KINDS,
    activate_service,
    archive_service,
    create_service,
    get_service,
    list_services,
    service_allows_kind,
)
from domain.service.messaging import emit_to_service
from domain.service.capabilities import (
    CAPABILITY_GATED_TOOLS,
    SERVICE_DEFAULT_CAPABILITIES,
    capability_enabled,
    gated_tool_names,
)

__all__ = [
    "CAPABILITY_GATED_TOOLS",
    "SERVICE_DEFAULT_CAPABILITIES",
    "SERVICE_PASSTHROUGH_KINDS",
    "activate_service",
    "archive_service",
    "capability_enabled",
    "create_service",
    "emit_to_service",
    "gated_tool_names",
    "get_service",
    "list_services",
    "service_allows_kind",
]
