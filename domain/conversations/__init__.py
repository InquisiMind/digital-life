"""Compatibility shim — 旧 conversations 模块的 API 入口已全部 forward 到
``domain.messages`` (去中心化消息总线,设计见
``docs/architecture/decentralized-message-bus.md``)。

Phase 4 删了整套旧机制:
- ``chats.db`` 共享聚合库(每实例独立 messages.db 取代)
- ``unified_contacts`` / ``unified_contact_ids`` 表(决策 3:不需要跨 app 同一个人)
- ``resolve_unified_id`` / ``register_chat_name`` / ``lookup_chat_name``
- ``_fan_out_to_other_instances`` (HTTP 广播 + urgency 走 broadcast endpoint 取代)
- ``chat_members`` / ``chat_groups`` (订阅配置 subscriptions.yaml 取代)

保留这三个函数名让上层(handler / action_tools / scheduler / communication)无感切换:
- ``record_inbound_message`` → ``domain.messages.record_inbound``
- ``publish_chat_message``   → ``domain.messages.record_outbound`` + HTTP 广播
- ``list_chat_messages``     → ``domain.messages.list_messages``
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("digital_life.domain.conversations")


def record_inbound_message(
    chat_id: str,
    *,
    sender_id: str,
    sender_name: str = "",
    text: str,
    msg_id: str = "",
    sender_kind: str = "human",
    attachments: list[str] | None = None,
) -> Optional[int]:
    """入站消息(实例自己收到平台消息时调一次)。

    旧签名保留兼容;实际写本实例 messages.db(direction='in',
    sender_role='human'/'other',per-app open_id 仅本库可见)。
    """
    from domain.messages import record_inbound
    # 平台前缀: feishu → lark, 其它用平台名自身
    try:
        from domain.lifecycle.runtime_context import get_current_event_platform
        _pf = get_current_event_platform()
    except Exception:
        _pf = ""
    _source = _pf or "feishu"  # fallback 默认 feishu（向后兼容，旧版记 lark）
    return record_inbound(
        chat_id=chat_id, sender_id=sender_id, sender_name=sender_name,
        text=text, msg_id=msg_id, source=_source, sender_kind=sender_kind,
        attachments=attachments,
    )


def publish_chat_message(
    chat_id: str,
    *,
    sender_id: str,
    sender_name: str = "",
    text: str,
    msg_id: str = "",
    sender_kind: str = "bot",
    broadcast: bool = True,
) -> Optional[int]:
    """出站消息(实例自己发群/私聊消息成功后调)。

    流程(决策 1-5):
    1. 写自己 messages.db(direction='out', sender_role='self')
    2. 仅当 sender_kind='bot' 且 broadcast=True 时,fire-and-forget HTTP 广播给同群 peer
       (peer 收到 broadcast endpoint 后,各自走 urgency 判断是否触发 wake)

    broadcast=False 的场景:本条消息正文 @ 到了本群某个机器人(peer),
    飞书会把消息单独推给被@的那个机器人——已送达,本侧再广播就是重复。
    调用方(action_tools)负责判定,这里只接收标志。
    """
    from domain.messages import record_outbound
    from infrastructure.config import get_app_instance_id, get_instance_display_name
    my_iid = get_app_instance_id() or sender_id
    my_name = get_instance_display_name() or sender_name or "Self"

    rid = record_outbound(
        chat_id=chat_id, self_display_name=my_name,
        self_instance_id=my_iid, text=text, msg_id=msg_id, source="feishu",
    )

    # HTTP 广播(决策 4:fire-and-forget,失败只 log 不阻塞,但 log 级别要够高
    # 才能定位问题——历史上是 debug 级,广播失败时 master 日志一片寂静,排查盲区)
    # broadcast=False:正文 @ 到了本群机器人,飞书已送达它,不再重复广播
    if sender_kind == "bot" and broadcast:
        try:
            from domain.messages.broadcast import broadcast_outbound
            delivered = broadcast_outbound(from_instance_id=my_iid, from_display_name=my_name,
                                           chat_id=chat_id, text=text, msg_ref=msg_id)
            if delivered == 0:
                logger.info("broadcast_outbound: 0 peers delivered (chat=%s)", chat_id[:16])
            else:
                logger.info("broadcast_outbound: delivered to %d peer(s) (chat=%s)",
                            delivered, chat_id[:16])
        except Exception as exc:
            logger.warning("broadcast_outbound failed (non-fatal): %s", exc)
    elif sender_kind == "bot" and not broadcast:
        logger.info("broadcast skipped: outbound text @-mentioned a bot peer (chat=%s)", chat_id[:16])

    return rid


def list_chat_messages(
    chat_id: str,
    limit: int = 30,
) -> list[dict]:
    """读 chat 最近消息(从本实例 messages.db 拉)。

    返回的 dict 含旧字段别名(msg_id/sender_id/sender_kind/created_at),
    让上层 scheduler chat_stream injection / communication._fmt 无感兼容。
    """
    from domain.messages import list_messages
    return list_messages(chat_id, limit=limit)

