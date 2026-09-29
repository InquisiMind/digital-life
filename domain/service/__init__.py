"""服务型运行体（service）领域模块。

一个服务 = 基于 agent 定义的独立上下文运行体。概念三层：
agent 定义（模版）→ 服务（运行体）→ 项目（任务容器）。
详见《元理AI战略咨询产品-总体设计文档》v1.1 第四章/第五章。
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
