"""服务 worker — 一次性进程：处理一个服务的到期事件后退出。

与实例进程的差异（设计文档 v1.1 特性 1 / 6.2）：
  - 实例进程长驻（通道 adapter + 60s cron 线程）；服务 worker 随消息拉起、
    处理完即释放，不留常驻。
  - 身份：进程内 ContextVar/env 全部指向 service_id；路径中枢把它解析到
    apps/{agent_def_id}/services/{service_id}/，事件/会话/记忆天然隔离。
  - 串行：DB lease（services 表库）保证同一服务同时只有一个 worker。
    调度循环 spawn 时把 lease holder 通过 L4_SERVICE_LEASE_HOLDER 传给
    worker；手工直跑时自取 holder，撞租即退。

生命周期不需要状态机：记忆在库、消息在队列，worker 挂了由调度循环重拉。
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("gateway.service_worker")


def run_service_worker(service_id: str) -> int:
    """处理该服务的到期事件。返回进程退出码。"""
    from infrastructure.config import (
        reset_current_instance_id,
        set_current_instance_id,
    )
    from infrastructure.persistence import services_registry

    svc = services_registry.lookup_service(service_id)
    if svc is None:
        logger.error("SERVICE_WORKER_EXIT service_id=%s reason=not_registered", service_id)
        return 1
    if svc.get("status") != services_registry.SERVICE_STATUS_ACTIVE:
        logger.info(
            "SERVICE_WORKER_EXIT service_id=%s reason=archived", service_id
        )
        return 0

    holder = (
        os.environ.get("L4_SERVICE_LEASE_HOLDER", "").strip()
        or f"worker:{os.getpid()}"
    )
    stale = services_registry.DEFAULT_LEASE_STALE_SECONDS
    # 单条原子 upsert：无租→插入；本人持有→续租；他人持有且未超时→False。
    if not services_registry.try_acquire_lease(service_id, holder, stale):
        existing = services_registry.get_lease(service_id)
        logger.warning(
            "SERVICE_WORKER_EXIT service_id=%s reason=lease_held_by=%s",
            service_id, existing["holder"] if existing else "?",
        )
        return 0

    # 身份切换：路径中枢（state.db/persona/配置）与事件层（channel 前缀）
    # 两套 ContextVar 都要指到服务——与 run_l4_tick 的 per-instance 姿势一致。
    os.environ["DIGITAL_LIFE_INSTANCE_ID"] = service_id
    cfg_token = set_current_instance_id(service_id)
    evt_token = None
    try:
        from domain.lifecycle.events import (
            pop_due_events,
            reset_instance_context,
            reset_wake_suppressed,
            set_instance_context,
            set_wake_suppressed,
        )

        evt_token = set_instance_context(service_id)

        from domain.lifecycle.affairs.runtime import init_db

        init_db()

        from domain.orchestration.lifecycle_orchestration.bootstrap.runtime import (
            ensure_life_affair,
        )

        # 引导期抑制进程内叫醒：首次运行建 life affair 会产生 birth 事件，
        # worker 自己就是唯一的叫醒者，不需要（也不该）再起后台 wake 线程。
        sup_token = set_wake_suppressed(True)
        try:
            affair_id = ensure_life_affair()
        finally:
            reset_wake_suppressed(sup_token)

        due = pop_due_events(limit=50)
        if not due:
            logger.info(
                "SERVICE_WORKER_EXIT service_id=%s reason=no_due_events", service_id
            )
            return 0

        from domain.lifecycle.scheduler import wake_digital_life
        from domain.lifecycle.wakeup_policy import choose_reason

        reason = choose_reason(due)
        logger.info(
            "SERVICE_WORKER_WAKE service_id=%s reason=%s events=%d",
            service_id, reason, len(due),
        )
        result = wake_digital_life(affair_id, reason=reason, pending_events=due)
        ok = not (isinstance(result, dict) and result.get("error"))
        logger.info(
            "SERVICE_WORKER_EXIT service_id=%s reason=done ok=%s", service_id, ok
        )
        return 0 if ok else 1
    except Exception:
        logger.exception(
            "SERVICE_WORKER_EXIT service_id=%s reason=crashed", service_id
        )
        return 1
    finally:
        if evt_token is not None:
            from domain.lifecycle.events import reset_instance_context

            reset_instance_context(evt_token)
        reset_current_instance_id(cfg_token)
        services_registry.release_lease(service_id, holder)
