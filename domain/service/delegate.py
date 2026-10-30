"""后台子代理（delegated service）—— 把服务型 agent 当作可委派的后台员工。

设计（docs/design/background-subagent.md，2026-10-30 zhp 定稿）：
  - 产物 = 标准服务（domain/service/registry.create_service，agent_def_id 恒为
    通用定义 subagent-def），与任何服务无运行形态差异；唯一新增是 services 表
    的 owner_ref 委托关系列。
  - 异步：spawn 只立项（注册表 + 首条任务 message 事件 + TTL 闹钟），执行走
    ServiceWorkerLoop 事件驱动，不开线程。
  - 通信复用：下发/追问 = emit_to_service(message)；汇报按委托人形态两对端——
    service: 走 emit_to_service；instance: 切身份写对方 messages.db + 标准唤醒
    事件（执行同构 send_chat_message 的同伴分支，只补 instance: 对端寻址）。
  - key 继承：服务自有 app.yaml 写 model.profile = agent:{委托人}，运行时由
    infrastructure/ai/config.py 内置解析（指针不下沉密钥）。

委托关系 owner_ref 形态：instance:{uuid}（实例型委托人）| service:{sid}（服务
型委托人）。spawn 后不可变；汇报/追问/并发上限/复盘聚合都读它。
"""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from datetime import timedelta
from typing import Iterator, Optional

logger = logging.getLogger("digital_life.domain.service.delegate")

# 通用子代理定义（apps/subagent-def，runtime_kind: definition 本地资产）。
SUBAGENT_DEF_ID = "subagent-def"
# 子代理消息在委托人侧的窗口与来源标记（区别于 consulting 的 svcgroup/service）。
SUBAGENT_PLATFORM = "subagent"

DEFAULT_TTL_HOURS = 24.0
MAX_TTL_HOURS = 24 * 7
# 同一委托人同时活跃的子代理上限（防风暴；子代理自己是 active 才计入）。
DEFAULT_MAX_ACTIVE = 3


def current_owner_ref() -> str:
    """当前运行体作为委托人的 owner_ref（service:/instance: 前缀）。"""
    from infrastructure.config import get_app_instance_id
    from infrastructure.persistence import services_registry

    iid = (get_app_instance_id() or "").strip()
    if not iid:
        return ""
    if services_registry.lookup_service(iid) is not None:
        return f"service:{iid}"
    return f"instance:{iid}"


def split_owner_ref(owner_ref: str) -> tuple[str, str]:
    """instance:{uuid} / service:{sid} → (kind, bare_id)。非法返回 ("", "")。"""
    kind, _, bare = str(owner_ref or "").partition(":")
    if kind not in ("instance", "service") or not bare:
        return "", ""
    return kind, bare


@contextmanager
def _runtime_context(runtime_id: str) -> Iterator[None]:
    """切到某运行体的库上下文（联系人/消息/事件都按 ContextVar 路由）。

    与 business.consulting.social._service_context 同一姿势的基座版——
    委托人可能是实例，所以叫 runtime 而不叫 service。
    """
    from domain.lifecycle.events import reset_instance_context, set_instance_context
    from infrastructure.config import (
        reset_current_instance_id,
        set_current_instance_id,
    )

    cfg = set_current_instance_id(runtime_id)
    evt = set_instance_context(runtime_id)
    try:
        yield
    finally:
        reset_instance_context(evt)
        reset_current_instance_id(cfg)


# ── 委托人侧：spawn / 追问 ────────────────────────────────────────────


def spawn_subagent(
    task: str,
    *,
    name: str = "",
    ttl_hours: float = DEFAULT_TTL_HOURS,
    owner_ref: str = "",
) -> dict:
    """立项一个后台子代理，立即返回联系人 ID；执行完全异步。

    七步（设计 §2）：并发上限检查 → create_service → 写 key 继承指针 →
    委托人联系人簿登记 → 首条任务 message 事件 → TTL 闹钟 → 返回。
    返回 {ok, contact_id, ...}；失败 {ok: False, error}。
    """
    from domain.service.registry import create_service
    from infrastructure.persistence import services_registry

    task = (task or "").strip()
    if not task:
        return {"ok": False, "error": "task 不能为空"}
    owner_ref = (owner_ref or current_owner_ref()).strip()
    owner_kind, owner_core = split_owner_ref(owner_ref)
    if not owner_kind:
        return {"ok": False, "error": "无法解析委托人身份（owner_ref 为空或非法）"}
    try:
        ttl_hours = float(ttl_hours)
    except (TypeError, ValueError):
        return {"ok": False, "error": f"ttl_hours 非法: {ttl_hours!r}"}
    if not 0.1 <= ttl_hours <= MAX_TTL_HOURS:
        return {"ok": False, "error": f"ttl_hours 须在 0.1~{MAX_TTL_HOURS} 之间"}

    active = services_registry.list_services_by_owner(
        owner_ref, status="active", agent_def_id=SUBAGENT_DEF_ID
    )
    if len(active) >= DEFAULT_MAX_ACTIVE:
        return {
            "ok": False,
            "error": f"活跃子代理已达上限（{DEFAULT_MAX_ACTIVE}）——先等现有子代理完成",
            "active": [s["service_id"] for s in active],
        }

    display_name = (name or "").strip() or f"子代理·{task[:12]}"
    try:
        svc = create_service(
            SUBAGENT_DEF_ID, owner_ref=owner_ref, display_name=display_name
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    sid = svc["service_id"]

    # key 继承指针（不下沉密钥）：委托人换 key 下个 worker 自动跟随。
    _write_profile_pointer(sid, owner_core)

    expires_at = _set_ttl_alarm(sid, ttl_hours)
    _register_contact_stub(owner_core, sid, display_name)

    owner_svc = services_registry.lookup_service(owner_core) if owner_kind == "service" else None
    sender_name = (owner_svc or {}).get("display_name") or ""
    event_id = _emit_task(sid, task, owner_core=owner_core, sender_name=sender_name)
    if not event_id:
        # 首条任务事件被闸拒（理论上不可能：新建服务必 active 且订阅 message）
        logger.error("SUBAGENT_TASK_EVENT_DROPPED sid=%s", sid)

    logger.info(
        "SUBAGENT_SPAWNED sid=%s owner=%s ttl=%.1fh expires=%s",
        sid, owner_ref, ttl_hours, expires_at,
    )
    return {
        "ok": True,
        "contact_id": sid,
        "display_name": display_name,
        "expires_at": expires_at,
    }


def _write_profile_pointer(sid: str, owner_core: str) -> None:
    """服务自有 app.yaml 写 model.profile: agent:{委托人}（指针，不拷贝密钥）。"""
    from infrastructure.config import get_project_root

    cfg_dir = get_project_root() / "apps" / SUBAGENT_DEF_ID / "services" / sid / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "app.yaml").write_text(
        "# 后台子代理自有配置：模型与 key 指针继承委托人（改一处全局生效）。\n"
        f"model:\n  profile: agent:{owner_core}\n",
        encoding="utf-8",
    )


def _set_ttl_alarm(sid: str, ttl_hours: float) -> str:
    """在子代理自己的 state.db 设 TTL 闹钟（timer 恒拉起，硬截止防跑飞）。"""
    from domain.lifecycle.alarms import set_alarm
    from domain.lifecycle.clock import now_dt

    fire_at = (now_dt() + timedelta(hours=ttl_hours)).isoformat(timespec="seconds")
    with _runtime_context(sid):
        from domain.lifecycle.affairs.runtime import init_db

        init_db()
        set_alarm(
            "timer",
            fire_at,
            {
                "reason": "TTL 硬截止到期——立即收尾",
                "subagent_ttl": True,
                "subagent_id": sid,
            },
        )
    return fire_at


def _register_contact_stub(owner_core: str, sid: str, display_name: str) -> None:
    """委托人联系人簿登记子代理（sense_contacts 可见、窗口可查），软失败。"""
    try:
        from domain.contacts import store as contacts

        with _runtime_context(owner_core):
            contacts.upsert_chat(f"subagent:{sid}", name=display_name, chat_type="dm")
            stub = contacts.get_or_create_stub(SUBAGENT_PLATFORM, sid, kind="bot")
            if stub is not None and not stub.get("name"):
                contacts.update_contact(stub["id"], name=display_name)
    except Exception:
        logger.warning("SUBAGENT_CONTACT_STUB_FAILED sid=%s", sid, exc_info=True)


def _emit_task(sid: str, task: str, *, owner_core: str, sender_name: str) -> int:
    """首条任务 = 标准 message 事件（worker ≤10s 被调度循环拉起）。"""
    from domain.service.messaging import emit_to_service

    return emit_to_service(
        sid,
        "message",
        {
            "text": task,
            "chat_id": f"peer:{owner_core}",
            "chat_name": "委托任务",
            "sender_id": owner_core,
            "sender_name": sender_name,
            "sender_position": "",
            "subagent_task": True,
        },
    )


def message_subagent(contact_id: str, text: str) -> dict:
    """委托人追问/补充/取消自己的子代理。已归档则自动复活（挂起壳先例）。"""
    from domain.service.registry import activate_service
    from domain.service.messaging import emit_to_service
    from infrastructure.persistence import services_registry

    sid = (contact_id or "").strip()
    text = (text or "").strip()
    if not sid or not text:
        return {"ok": False, "error": "必须传 contact_id 和 text"}
    svc = services_registry.lookup_service(sid)
    if svc is None or svc.get("agent_def_id") != SUBAGENT_DEF_ID:
        return {"ok": False, "error": f"{sid} 不是子代理"}
    owner_ref = current_owner_ref()
    if svc.get("owner_ref") != owner_ref:
        return {"ok": False, "error": "只能指挥自己的子代理"}

    revived = False
    if svc.get("status") != services_registry.SERVICE_STATUS_ACTIVE:
        activate_service(sid)
        revived = True

    owner_kind, owner_core = split_owner_ref(owner_ref)
    sender_name = ""
    if owner_kind == "service":
        sender_name = (services_registry.lookup_service(owner_core) or {}).get("display_name") or ""
    _record_own_outbound(f"subagent:{sid}", text, sender_id=owner_core, sender_name=sender_name)
    event_id = emit_to_service(
        sid,
        "message",
        {
            "text": text,
            "chat_id": f"peer:{owner_core}",
            "chat_name": "委托人",
            "sender_id": owner_core,
            "sender_name": sender_name,
            "sender_position": "",
            "mentions_bot": True,
        },
    )
    logger.info(
        "SUBAGENT_MESSAGED sid=%s owner=%s revived=%s event=%s", sid, owner_ref, revived, event_id
    )
    return {"ok": True, "revived": revived, "recipients": [sid], "event_id": event_id}


# ── 子代理侧：汇报 ────────────────────────────────────────────────────


def report_to_delegator(text: str, *, done: bool = True, sender_id: str = "") -> dict:
    """子代理把结果（或进度 done=False）发给委托人；done=True 汇报后归档。

    委托人地址从注册表 owner_ref 读（反向寻址零参数）。两对端路由：
    service: → emit_to_service（同构同伴私聊）；instance: → 切身份写对方
    messages.db + message 事件（唤醒决策归实例侧 cron 兜底——本进程是
    ephemeral worker，绝不能跑实例的 in-process wake）。
    """
    from domain.lifecycle.alarms import cancel_alarms_by_kind
    from domain.service.registry import archive_service
    from infrastructure.config import get_app_instance_id
    from infrastructure.persistence import services_registry

    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "text 不能为空"}
    sid = (sender_id or (get_app_instance_id() or "")).strip()
    svc = services_registry.lookup_service(sid) if sid else None
    if svc is None:
        return {"ok": False, "error": "本运行体不是已注册服务，无从汇报"}
    owner_ref = svc.get("owner_ref") or ""
    owner_kind, owner_core = split_owner_ref(owner_ref)
    if not owner_kind:
        return {"ok": False, "error": "本服务不是子代理（注册表无委托关系）"}

    display_name = svc.get("display_name") or ""
    mid = f"subreport-{uuid.uuid4().hex[:12]}"

    inserted = True
    if owner_kind == "service":
        from domain.messages import record_inbound, record_outbound
        from domain.service.messaging import emit_to_service

        with _runtime_context(owner_core):
            _, inserted = record_inbound(
                chat_id=f"peer:{sid}",
                sender_id=sid,
                sender_name=display_name,
                text=text,
                msg_id=mid,
                source=SUBAGENT_PLATFORM,
                sender_kind="bot",
            )
        if inserted:
            emit_to_service(
                owner_core,
                "message",
                {
                    "text": text,
                    "chat_id": f"peer:{sid}",
                    "chat_name": display_name,
                    "sender_id": sid,
                    "sender_name": display_name,
                    "sender_position": "",
                    "collab": True,
                    "subagent_report": True,
                },
            )
    else:
        from domain.lifecycle.events import (
            emit_event,
            reset_wake_suppressed,
            set_wake_suppressed,
        )
        from domain.messages import record_inbound

        with _runtime_context(owner_core):
            _, inserted = record_inbound(
                chat_id=f"subagent:{sid}",
                sender_id=sid,
                sender_name=display_name,
                text=text,
                msg_id=mid,
                source=SUBAGENT_PLATFORM,
                sender_kind="bot",
            )
            if inserted:
                # 抑制进程内叫醒：本进程是子代理 worker（ephemeral），实例的
                # wake 不属于这里；事件落库后由实例自己的 cron 兜底拉起。
                sup = set_wake_suppressed(True)
                try:
                    emit_event(
                        kind="message",
                        payload={
                            "text": text,
                            "chat_id": f"subagent:{sid}",
                            "chat_name": display_name,
                            "sender_id": sid,
                            "sender_name": display_name,
                            "sender_position": "",
                            "mentions_bot": True,
                            "subagent_report": True,
                        },
                    )
                finally:
                    reset_wake_suppressed(sup)
    if not inserted:
        return {"ok": False, "error": "汇报落库失败（消息未投出）"}

    _record_own_outbound(f"peer:{owner_core}", text, sender_id=sid, sender_name=display_name)

    archived = False
    if done:
        # 清自己的 timer（TTL 闹钟已完成使命；rest 自设闹钟随归档一并作废）
        with _runtime_context(sid):
            try:
                cancel_alarms_by_kind("timer")
            except Exception:
                logger.warning("SUBAGENT_TTL_CANCEL_FAILED sid=%s", sid, exc_info=True)
        archive_service(sid)
        archived = True

    logger.info(
        "SUBAGENT_REPORTED sid=%s owner=%s done=%s chars=%d", sid, owner_ref, done, len(text)
    )
    return {"ok": True, "archived": archived, "delegator": owner_ref}


def _record_own_outbound(
    chat_id: str, text: str, *, sender_id: str, sender_name: str
) -> None:
    """发送方侧出站记录（当前身份即发送方）。软失败不阻塞投递。"""
    try:
        from domain.messages import record_outbound

        record_outbound(
            chat_id=chat_id,
            self_display_name=sender_name,
            self_instance_id=sender_id,
            text=text,
            source=SUBAGENT_PLATFORM,
        )
    except Exception:
        logger.warning("SUBAGENT_OUTBOUND_RECORD_FAILED", exc_info=True)


def list_subagents_for(owner_ref: str, *, status: Optional[str] = None) -> list[dict]:
    """委托人名下的子代理列表（工具 sense/管理面聚合用）。"""
    from infrastructure.persistence import services_registry

    return [
        {
            "service_id": s["service_id"],
            "display_name": s.get("display_name") or "",
            "status": s.get("status") or "",
            "created_at": s.get("created_at") or "",
        }
        for s in services_registry.list_services_by_owner(
            owner_ref, status=status, agent_def_id=SUBAGENT_DEF_ID
        )
    ]
