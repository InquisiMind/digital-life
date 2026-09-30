"""服务社交层 — 项目社交圈的建立与消息路由（刀 4b）。

设计（2026-09-29 与 zhp 收敛）：服务间协作不是专用工具，而是引擎社交机制
（联系人/群聊/私聊）在服务侧的自然延伸。建项目时一次性建立社交圈：

  - 每个成员服务的联系人里：其他成员（合作伙伴，kind=bot）+ 项目群
  - PM 额外有客户联系人（他的联系人有两条：内部群 + 客户）
  - 群 chat_id：svcgroup:{pid}；同伴私聊窗口按"对方 service_id"键；
    客户窗口：customer:{cid}

消息路由 send_chat_message(chat_id, text)：
  - 群 → 对其他成员 fan-out：各自 messages.db 落一条入站（source=svcgroup）
    + group_message 事件进对方队列（对方被唤醒）——语义与实例 broadcast 同构
  - 同伴私聊（chat_id=对方 service_id）→ 定向 message 事件 + 双方落库
  - 客户私聊（PM 的 customer:{cid} 窗口）→ 只落 PM 出站记录，真实投递
    归 web 通道（刀 3）——现在记录即审计

全部经 ContextVar 路由到各服务私有库，不引入第二套路径。
"""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from typing import Optional

logger = logging.getLogger("digital_life.domain.service.social")

GROUP_PLATFORM = "svcgroup"
PEER_PLATFORM = "service"
CUSTOMER_PLATFORM = "customer"


def group_chat_id(project_id: str) -> str:
    return f"svcgroup:{project_id}"


def customer_chat_id(customer_id: str) -> str:
    return f"customer:{customer_id}"


@contextmanager
def _service_context(service_id: str):
    """切到某服务的库上下文（联系人/消息都按 ContextVar 路由）。"""
    from domain.lifecycle.events import reset_instance_context, set_instance_context
    from infrastructure.config import (
        reset_current_instance_id,
        set_current_instance_id,
    )

    cfg = set_current_instance_id(service_id)
    evt = set_instance_context(service_id)
    try:
        yield
    finally:
        reset_instance_context(evt)
        reset_current_instance_id(cfg)


# ── 社交圈建立 ────────────────────────────────────────────────────────


def register_social_circle(
    project_id: str,
    members: list[dict],           # [{service_id, role}]
    pm_service_id: str,
    customer: Optional[dict] = None,  # {id, name}
) -> None:
    """建项目时为每个成员登记联系人 + 群窗口；PM 额外登记客户。

    幂等：stub 已存在则只补名字。
    """
    from domain.contacts import store as contacts

    gchat = group_chat_id(project_id)
    for me in members:
        sid = me["service_id"]
        with _service_context(sid):
            contacts.upsert_chat(gchat, name=f"项目群:{project_id[:12]}", chat_type="group")
            for peer in members:
                if peer["service_id"] == sid:
                    continue
                stub = contacts.get_or_create_stub(
                    PEER_PLATFORM, peer["service_id"], kind="bot"
                )
                if stub is not None and not stub.get("name"):
                    contacts.update_contact(
                        stub["id"], name=peer.get("role") or peer["service_id"][:12]
                    )
            if sid == pm_service_id and customer:
                contacts.upsert_chat(
                    customer_chat_id(customer["id"]),
                    name=customer.get("name") or "客户",
                    chat_type="dm",
                )
                stub = contacts.get_or_create_stub(
                    CUSTOMER_PLATFORM, customer["id"], kind="human"
                )
                if stub is not None and not stub.get("name"):
                    contacts.update_contact(
                        stub["id"], name=customer.get("name") or "客户"
                    )
    logger.info(
        "SOCIAL_CIRCLE_READY project=%s members=%d pm=%s customer=%s",
        project_id, len(members), pm_service_id[:12],
        bool(customer),
    )


# ── 消息路由 ──────────────────────────────────────────────────────────


def _member_role(service_id: str) -> str:
    from infrastructure.persistence import services_registry

    svc = services_registry.lookup_service(service_id) or {}
    return svc.get("display_name") or service_id[:12]


def send_chat_message(sender_service_id: str, chat_id: str, text: str,
                      msg_id: str = "") -> dict:
    """服务侧统一发消息入口。返回 {ok, kind, recipients}。

    路由：群 → fan-out；同伴 service_id → 定向；customer:* → 出站记录。
    """
    from domain.messages import record_inbound, record_outbound
    from domain.service import emit_to_service
    from infrastructure.persistence import services_registry

    sender_role = _member_role(sender_service_id)
    mid = msg_id or f"svcmsg-{uuid.uuid4().hex[:12]}"

    # 1) 项目群：fan-out 给其他成员
    if chat_id.startswith("svcgroup:"):
        pid = chat_id.split(":", 1)[1]
        members = services_registry.list_services_by_project(pid, status="active")
        recipients = []
        for m in members:
            peer = m["service_id"]
            if peer == sender_service_id:
                continue
            with _service_context(peer):
                _, inserted = record_inbound(
                    chat_id=chat_id,
                    sender_id=sender_service_id,
                    sender_name=sender_role,
                    text=text,
                    msg_id=mid,
                    source=GROUP_PLATFORM,
                    sender_kind="bot",
                )
            # 唤醒过滤：消息落库全员可见，但只唤醒被提及的成员
            # （文本含其角色名，或"全体/所有人/大家"）——其余成员下次醒来
            # 从群历史上下文补看，避免每条群话全员唤醒
            peer_role = (m.get("display_name") or "").strip()
            mentioned = (
                peer_role and peer_role in text
            ) or any(kw in text for kw in ("全体", "所有人", "大家"))
            if inserted and mentioned:
                emit_to_service(
                    peer,
                    "group_message",
                    {
                        "text": text,
                        "chat_id": chat_id,
                        "chat_name": f"项目群:{pid[:12]}",
                        "sender_id": sender_service_id,
                        "sender_name": sender_role,
                        "sender_position": sender_role,
                        "mentions_bot": True,
                    },
                )
                recipients.append(peer)
            elif inserted:
                recipients.append(f"{peer}(saved,silent)")
            else:
                recipients.append(f"{peer}(dup)")
        with _service_context(sender_service_id):
            record_outbound(
                chat_id=chat_id,
                self_display_name=sender_role,
                self_instance_id=sender_service_id,
                text=text,
                msg_id=mid,
                source=GROUP_PLATFORM,
            )
        return {"ok": True, "kind": "group", "recipients": recipients}

    # 2) 同伴私聊：chat_id = 对方 service_id
    peer = services_registry.lookup_service(chat_id)
    if peer is not None:
        with _service_context(chat_id):
            _, inserted = record_inbound(
                chat_id=f"peer:{sender_service_id}",
                sender_id=sender_service_id,
                sender_name=sender_role,
                text=text,
                msg_id=mid,
                source=PEER_PLATFORM,
                sender_kind="bot",
            )
        if inserted:
            emit_to_service(
                chat_id,
                "message",
                {"text": text, "chat_id": f"peer:{sender_service_id}",
                 "sender_id": sender_service_id, "sender_name": sender_role,
                 "from_role": sender_role, "collab": True},
            )
        with _service_context(sender_service_id):
            record_outbound(
                chat_id=chat_id,
                self_display_name=sender_role,
                self_instance_id=sender_service_id,
                text=text,
                msg_id=mid,
                source=PEER_PLATFORM,
            )
        return {"ok": True, "kind": "peer", "recipients": [chat_id]}

    # 3) 客户私聊（PM → 客户）：落出站记录；真实投递归 web 通道（刀 3）
    if chat_id.startswith("customer:"):
        with _service_context(sender_service_id):
            record_outbound(
                chat_id=chat_id,
                self_display_name=sender_role,
                self_instance_id=sender_service_id,
                text=text,
                msg_id=mid,
                source=CUSTOMER_PLATFORM,
            )
        return {"ok": True, "kind": "customer", "recipients": [chat_id],
                "delivery": "web(刀3前只记录)"}

    return {"ok": False, "kind": "unknown", "recipients": [],
            "error": f"无法路由 chat_id={chat_id!r}（项目群 svcgroup:* / 同伴 service_id / 客户 customer:*）"}


def customer_message_to_pm(pm_service_id: str, customer: dict, text: str) -> int:
    """客户 → PM 的入站（S00：web 入口/脚本用）。落 PM 库 + message 事件唤醒。"""
    from domain.messages import record_inbound
    from domain.service import emit_to_service

    cid = customer.get("id") or "anon"
    chat = customer_chat_id(cid)
    with _service_context(pm_service_id):
        _, inserted = record_inbound(
            chat_id=chat,
            sender_id=cid,
            sender_name=customer.get("name") or "客户",
            text=text,
            msg_id=f"cust-{uuid.uuid4().hex[:12]}",
            source=CUSTOMER_PLATFORM,
            sender_kind="human",
        )
    if not inserted:
        return 0
    return emit_to_service(
        pm_service_id,
        "message",
        {
            "text": text,
            "chat_id": chat,
            "sender_id": cid,
            "sender_name": customer.get("name") or "客户",
            "customer_message": True,
        },
    )
