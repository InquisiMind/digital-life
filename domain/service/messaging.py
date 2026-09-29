"""向服务投递事件：服务级闸 + 身份切换 + 抑制进程内叫醒。

为什么不用普通 emit_event 直发：
  1. 服务级闸（状态/订阅）在注册表里，不在实例级 event_subscriptions.yaml；
  2. 事件要落进服务私有的 state.db（apps/{def}/services/{sid}/data/），
     需要把 instance/事件两层 ContextVar 切到服务身份；
  3. 服务没有常驻进程承载 wake 线程——emit 后的 _wake_or_inject 必须抑制，
     叫醒由服务调度循环负责（扫队列 → 抢 lease → spawn worker）。
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("digital_life.domain.service")


def emit_to_service(
    service_id: str,
    kind: str,
    payload: Optional[dict] = None,
    fire_at: Optional[str] = None,
    channel: Optional[str] = None,
) -> int:
    """向服务投递一个事件，返回 event_id（0 = 被闸拒绝，不入队）。

    payload 约定与实例 message 事件一致（text/chat_id/sender_name...），
    worker 侧的 wake prompt 模板按 kind 复用现有渲染。
    """
    from domain.service.registry import service_allows_kind
    from infrastructure.persistence import services_registry

    svc = services_registry.lookup_service(service_id)
    if svc is None:
        logger.warning(
            "SERVICE_EVENT_DROPPED service_id=%s kind=%s reason=not_found", service_id, kind
        )
        return 0
    if svc.get("status") != services_registry.SERVICE_STATUS_ACTIVE:
        logger.info(
            "SERVICE_EVENT_DROPPED service_id=%s kind=%s reason=archived", service_id, kind
        )
        return 0
    if not service_allows_kind(svc, kind):
        logger.info(
            "SERVICE_EVENT_DROPPED service_id=%s kind=%s reason=unsubscribed subs=%r",
            service_id, kind, svc.get("subscriptions"),
        )
        return 0

    from domain.lifecycle.events import (
        emit_event,
        reset_instance_context,
        reset_wake_suppressed,
        set_instance_context,
        set_wake_suppressed,
    )
    from infrastructure.config import (
        reset_current_instance_id,
        set_current_instance_id,
    )

    cfg_token = set_current_instance_id(service_id)
    evt_token = set_instance_context(service_id)
    sup_token = set_wake_suppressed(True)
    try:
        return emit_event(kind=kind, payload=payload, fire_at=fire_at, channel=channel)
    finally:
        reset_wake_suppressed(sup_token)
        reset_instance_context(evt_token)
        reset_current_instance_id(cfg_token)
