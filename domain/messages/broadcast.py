"""去中心化消息总线 Phase 3:HTTP 广播 + 订阅配置。

设计参见 ``docs/architecture/decentralized-message-bus.md``。

责任:
- 加载实例 ``apps/<id>/config/subscriptions.yaml``(本实例订阅哪些群 + 同群有哪些
  peer 实例的 endpoint)
- ``broadcast_message``:出站成功后,fire-and-forget HTTP POST 给所有 peer
- ``receive_broadcast``:peer HTTP endpoint 收到时调用,写入实例 messages.db 并
  走 urgency 分类(走和飞书入站完全一样的路径)

订阅配置 master 启动时自动维护:对每个实例,与所有其他实例比较 app.yaml 的 chat_ids
重合的部分,生成 subscriptions.yaml 默认条目。实例间物理隔离,peer 的 endpoint 是
http://<host>:<api_port>/internal/message-broadcast(目前同机部署)。
"""
from __future__ import annotations

import logging
import os
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from xmlrpc.client import boolean

import httpx
import yaml

logger = logging.getLogger(__name__)

MASTER_HOST_DEFAULT = "127.0.0.1"
MASTER_PORT_DEFAULT = 8642


# ─────────────────────────────────────────────────────────────────────────────
# 订阅配置(subscriptions.yaml)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Peer:
    uuid: str
    endpoint: str   # http://host:port/internal/message-broadcast


@dataclass
class Subscription:
    chat_id: str
    platform: str = "feishu"
    peers: list[Peer] = field(default_factory=list)
    name: str = ""  # 群名(可选)


def _subscriptions_path(instance_id: str) -> Path:
    from infrastructure.config import get_instance_dir
    return get_instance_dir(instance_id) / "config" / "subscriptions.yaml"


def load_subscriptions(instance_id: str) -> dict[str, Subscription]:
    """读实例的 subscriptions.yaml。文件不存在 → 空字典(允许实例不订阅任何群)。"""
    p = _subscriptions_path(instance_id)
    if not p.is_file():
        return {}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.warning("subscriptions.yaml load failed for %s: %s", instance_id[:8], exc)
        return {}
    out: dict[str, Subscription] = {}
    for chat_id, cfg in (data.get("subscriptions") or {}).items():
        if not isinstance(cfg, dict):
            continue
        peers = [
            Peer(uuid=pp.get("uuid") or "", endpoint=pp.get("endpoint") or "")
            for pp in (cfg.get("peers") or [])
            if isinstance(pp, dict) and pp.get("uuid")
        ]
        out[chat_id] = Subscription(
            chat_id=chat_id,
            platform=cfg.get("platform") or "feishu",
            peers=peers,
            name=cfg.get("name") or "",
        )
    return out


def save_subscriptions(instance_id: str, subs: dict[str, Subscription]) -> None:
    """幂等写实例 subscriptions.yaml。"""
    p = _subscriptions_path(instance_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "subscriptions": {
            chat_id: {
                "name": s.name,
                "platform": s.platform,
                "peers": [{"uuid": pe.uuid, "endpoint": pe.endpoint} for pe in s.peers],
            }
            for chat_id, s in subs.items()
        }
    }
    p.write_text(yaml.safe_dump(data, allow_unicode=True, default_flow_style=False,
                                sort_keys=False), encoding="utf-8")


def sync_subscriptions_from_registry() -> None:
    """master 启动时调用:对每个实例,与所有其他实例比较可见 chat 列表重合部分,
    生成默认条目。已存在条目不覆盖(尊重人手编辑)。

    逻辑:
      - 实例 A 自己 chats = app.yaml chat_ids + messages.db 实际见过的 chat
      - 实例 B,C 也有这些 chat
      - 给 A 的 subscriptions.yaml[<chat>].peers = [B.endpoint, C.endpoint]

    追加 messages.db 反推的 chat,是因为 app.yaml.chat_ids 是可选的——很多场景
    (像当前 zero/alpha 都没填 chat_ids)实例实际订阅了群但 yaml 里没声明。
    用 messages.db 已记录的 chat_id 作权威订阅源。
    """
    try:
        from infrastructure.config import discover_active_instances, get_instance_dir
    except Exception as exc:
        logger.warning("sync_subscriptions: cannot import config: %s", exc)
        return

    instances = discover_active_instances()
    if not instances:
        return
    # 先拉所有实例的 chat 集合 + endpoint
    chat_to_instances: dict[str, list[tuple[str, str]]] = {}  # chat -> [(iid, endpoint)]
    instance_chats: dict[str, set[str]] = {}
    for iid in instances:
        chats = _read_instance_chat_ids(iid) | _read_chats_from_messages_db(iid)
        instance_chats[iid] = chats
        endpoint = _peer_endpoint_for(iid)
        for chat in chats:
            chat_to_instances.setdefault(chat, []).append((iid, endpoint))

    for iid in instances:
        my_chats = instance_chats.get(iid, set())
        cur = load_subscriptions(iid)
        changed = False
        for chat_id in my_chats:
            peers = [
                Peer(uuid=oiid, endpoint=oep)
                for (oiid, oep) in chat_to_instances.get(chat_id, [])
                if oiid != iid  # 排除自己
            ]
            if chat_id in cur:
                # 已存在,保留(name 允许覆盖以维持参考价值,peers 也同步):
                # 但若用户已经手填且 peers 非空,避免抹除,跳过。
                if cur[chat_id].peers:
                    continue
                cur[chat_id].peers = peers
                changed = True
            else:
                cur[chat_id] = Subscription(chat_id=chat_id, platform="feishu",
                                            peers=peers, name="")
                changed = True
        if changed:
            try:
                save_subscriptions(iid, cur)
                logger.info("subscriptions.yaml synced for %s (%d chats)",
                            iid[:8], len(cur))
            except Exception as exc:
                logger.warning("save subscriptions.yaml failed for %s: %s",
                               iid[:8], exc)


def _read_chats_from_messages_db(instance_id: str) -> set[str]:
    """从实例的 messages.db 反推所有见过的 chat_id。

    比 app.yaml 的 chat_ids 更权威——这是实例实际处理过的所有聊天的全集。
    不读 chats.db(那个 Phase 4 删)。
    """
    from infrastructure.config import get_instance_data_dir
    try:
        db = get_instance_data_dir(instance_id) / "messages.db"
        if not db.is_file():
            return set()
        import sqlite3
        conn = sqlite3.connect(str(db))
        try:
            rows = conn.execute("SELECT DISTINCT chat_id FROM messages").fetchall()
            return {r[0] for r in rows if r[0]}
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("read chats from messages.db failed for %s: %s",
                     instance_id[:8], exc)
        return set()


def _peer_endpoint_for(instance_id: str) -> str:
    """peer 实例的 broadcast endpoint URL。"""
    host = os.getenv("DIGITAL_LIFE_PEER_HOST") or MASTER_HOST_DEFAULT
    port = int(os.getenv("API_SERVER_PORT") or MASTER_PORT_DEFAULT)
    return f"http://{host}:{port}/internal/message-broadcast"


def _read_instance_chat_ids(instance_id: str) -> set[str]:
    """读实例 app.yaml 的 channels.feishu.chat_ids。"""
    from infrastructure.config import get_instance_dir
    cfg = get_instance_dir(instance_id) / "config" / "app.yaml"
    if not cfg.is_file():
        return set()
    try:
        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        channels = data.get("channels") or {}
        feishu = channels.get("feishu") or {} if isinstance(channels, dict) else {}
        ids = feishu.get("chat_ids") or [] if isinstance(feishu, dict) else []
        return {str(c).strip() for c in ids if c}
    except Exception:
        return set()


# ─────────────────────────────────────────────────────────────────────────────
# 广播出站
# ─────────────────────────────────────────────────────────────────────────────

def broadcast_outbound(
    *,
    from_instance_id: str,
    from_display_name: str,
    chat_id: str,
    text: str,
    msg_ref: str,
    timeout: float = 2.0,
) -> int:
    """出站成功后调一次,fire-and-forget HTTP 广播给同群 peer。

    返回成功送达的 peer 数(失败只 log,不抛 — 决策 4:可接受偶发丢失)。

    msg_ref:
      优先用平台返回的真实 msg_id 做去重(接收方 UNIQUE(source, msg_ref));
      如果调用方传空(如 express_to_human 快速路径没等飞书回 msg_id),
      本地生成一个唯一 ref,保证 broadcast 仍然触发——历史 BUG:msg_id="" 守卫
      让 alpha→zero 广播全部静默跳过,zero 误以为 alpha 没说话 → 看到的人类
      后续追问被错误归到 zero 头上。
    """
    if not from_instance_id or not chat_id:
        return 0
    if not msg_ref:
        # 本地合成唯一 ref:from+时间戳+文本哈希
        import hashlib as _hl, time as _t
        h = _hl.md5(f"{from_instance_id}|{chat_id}|{text}".encode("utf-8")).hexdigest()[:8]
        msg_ref = f"broadcast_{from_instance_id[:8]}_{int(_t.time()*1000)}_{h}"
    subs = load_subscriptions(from_instance_id)
    sub = subs.get(chat_id)
    if not sub or not sub.peers:
        return 0

    payload = {
        "from_instance_id": from_instance_id,
        "from_display_name": from_display_name,
        "chat_id": chat_id,
        "text": text,
        "msg_ref": msg_ref,
        "source_platform": sub.platform,
    }

    delivered = 0
    logger.info(
        "BROADCAST_HTTP_OUT from=%s chat=%r peer_count=%d text_head=%r msg_ref=%r",
        from_instance_id[:8], chat_id, len(sub.peers),
        (text[:60] + ("…" if len(text) > 60 else "")) if isinstance(text, str) else text,
        msg_ref,
    )
    for peer in sub.peers:
        if not peer.endpoint:
            logger.info(
                "BROADCAST_SKIP_PEER reason=no_endpoint from=%s peer=%s chat=%r",
                from_instance_id[:8], peer.uuid[:8], chat_id,
            )
            continue
        try:
            r = httpx.post(peer.endpoint, json=payload, timeout=timeout)
            if r.status_code == 200:
                delivered += 1
                logger.info(
                    "BROADCAST_HTTP_OK from=%s → peer=%s chat=%r status=200",
                    from_instance_id[:8], peer.uuid[:8], chat_id,
                )
            else:
                logger.warning(
                    "BROADCAST_HTTP_BAD_STATUS from=%s → peer=%s chat=%r endpoint=%s status=%d body=%r",
                    from_instance_id[:8], peer.uuid[:8], chat_id,
                    peer.endpoint, r.status_code, r.text[:120],
                )
        except Exception as exc:
            logger.warning(
                "BROADCAST_HTTP_EXC from=%s → peer=%s chat=%r endpoint=%s exc=%r",
                from_instance_id[:8], peer.uuid[:8], chat_id,
                peer.endpoint, exc,
            )
    return delivered


# ─────────────────────────────────────────────────────────────────────────────
# 接收广播(HTTP endpoint 收到时调)
# ─────────────────────────────────────────────────────────────────────────────

def receive_broadcast(payload: dict) -> dict:
    """HTTP endpoint 收到广播时调,完成 master 中转分发。

    master 进程不在任何单一实例上下文中——本函数不能依赖 ContextVar 默认值
    决定写入哪。正确做法:读 group→subscribers 全局订阅映射,对每个非 from 的
    订阅者实例,**分别**写入各自 messages.db + 在各自实例上下文 emit_event(让
    urgency 分类、debounce、wake fast-path 在实例自己的 events 表里生效)。

    每个 peer 的步骤:
    1. set_current_instance_id(peer_uuid) + set_instance_context(peer_uuid)
    2. record_broadcast_in(instance_id=peer_uuid) — 写对方库
    3. emit_event(kind='group_message', channel='internal:broadcast') — 路由到对方 events
    4. reset 上下文

    payload 字段:见 docs/architecture/decentralized-message-bus.md。
    """
    from_instance_id = (payload.get("from_instance_id") or "").strip()
    from_display_name = (payload.get("from_display_name") or "").strip()
    chat_id = (payload.get("chat_id") or "").strip()
    text = payload.get("text") or ""
    msg_ref = payload.get("msg_ref") or ""
    if not chat_id or not text or not from_instance_id:
        return {"ok": False, "reason": "missing required fields"}

    # 通过订阅表反查"谁订阅了这个群",除 from 外都是目标。
    # 订阅表是 per-instance 文件;在 master 没有全局订阅表的情况下,
    # 遍历所有 active 实例,读每个实例的 subscriptions.yaml[chat_id].peers
    # 与 from 是同群成员的即可视为候选目标。
    # (示例:zero 在订阅里有 [alpha] peer, alpha 在订阅里有 [zero] peer;
    #        我们要发到全部不是 from 自己的目标)
    targets = _resolve_broadcast_targets(chat_id=chat_id,
                                         from_instance_id=from_instance_id)
    if not targets:
        logger.info("broadcast from %s, chat=%s: no subscribers (skipped)",
                    from_instance_id[:8], chat_id[:16])
        return {"ok": True, "delivered": 0, "reason": "no subscribers"}

    from infrastructure.config import (set_current_instance_id,
                                       reset_current_instance_id)
    from domain.lifecycle.events import (set_instance_context,
                                          reset_instance_context,
                                          emit_event)
    from domain.messages import record_broadcast_in

    delivered = 0
    errors: list[str] = []
    for peer_iid in targets:
        # 切到 peer 实例上下文(record + emit 都会按此隔离)
        iid_token = set_current_instance_id(peer_iid)
        ctx_token = set_instance_context(peer_iid)
        try:
            # 1. 写入 peer 的 messages.db
            try:
                record_broadcast_in(
                    chat_id=chat_id,
                    from_display_name=from_display_name or from_instance_id[:8],
                    from_instance_id=from_instance_id,
                    text=text,
                    msg_ref=msg_ref if msg_ref else
                        f"broadcast_{from_instance_id[:8]}_{int(time.time()*1000)}",
                    source_platform=payload.get("source_platform") or "feishu",
                    instance_id=peer_iid,  # 显式:绕过默认值
                )
            except Exception as exc:
                errors.append(f"peer {peer_iid[:8]} record failed: {exc}")
                logger.warning("receive_broadcast: record failed for peer %s: %s",
                               peer_iid[:8], exc)
                continue

            # 2. emit 一个 group_message 事件(走和飞书入站完全一致的 urgency 分类)
            # 由 peer 自己的 cron + handler 决定立即 wake / 30s 累积。
            try:
                emit_event(
                    kind="group_message",
                    payload={
                        "text": text,
                        "sender_name": from_display_name or from_instance_id[:8],
                        "sender_id": from_instance_id,  # 永不跨实例输出
                        "sender_kind": "bot",          # 不当 human
                        "chat_name": "",
                        "chat_id": chat_id,
                        "mentions_bot": False,         # 靠 keywords/owner 判定 immediate
                        "mention_names": "",
                        "source": "broadcast",
                        "_bypass_chat_stream_write": True,  # 已写过 messages.db
                    },
                    channel="internal:broadcast",
                )
            except Exception as exc:
                # 写库已经成功,emit 失败不影响下一次自然 cron tick 也能_log 这条消息
                logger.warning(
                    "BROADCAST_EMIT_FAILED peer=%s from=%s chat=%r exc=%r",
                    peer_iid[:8], from_instance_id[:8], chat_id, exc,
                )
                errors.append(f"peer {peer_iid[:8]} emit failed: {exc}")
            else:
                logger.info(
                    "BROADCAST_DELIVERED peer=%s from=%s chat=%r text_head=%r",
                    peer_iid[:8], from_instance_id[:8], chat_id,
                    text[:60],
                )

            delivered += 1
        finally:
            try:
                reset_instance_context(ctx_token)
            except Exception:
                pass
            try:
                reset_current_instance_id(iid_token)
            except Exception:
                pass

    return {"ok": True, "delivered": delivered,
            "errors": errors if errors else None}


def _resolve_broadcast_targets(*, chat_id: str,
                                from_instance_id: str) -> list[str]:
    """反查"订阅了这个群的非 from 实例 UUID 集合"。

    设计上每个实例的 subscriptions.yaml[chat_id].peers 列出同群其他 endpoint,
    但 peers 里的 uuid 通常就是对方实例 UUID。我们扫描所有 active 实例的
    subscriptions,合并:
      - 任何 subscriptions[chat_id].peers 里的 uuid
      - 任何 "订阅了这个 chat_id" 的实例本身(因为实例自己订阅了 chat,说明它在群)
    然后排除 from 自己。

    如果没有任何实例的 subscriptions 里出现 chat_id(就是从没同步过),返回 []。
    """
    from infrastructure.config import discover_active_instances

    try:
        instances = discover_active_instances()
    except Exception as exc:
        logger.warning("_resolve_broadcast_targets: cannot discover instances: %s", exc)
        return []

    targets: set[str] = set()
    for iid in instances:
        subs = load_subscriptions(iid)
        sub = subs.get(chat_id)
        if sub is None:
            continue
        # 实例本身订阅了这个 chat → 它在群 → 加候选
        targets.add(iid)
        # peers 是它知道的同群其他实例 endpoint uuid
        for peer in sub.peers:
            if peer.uuid and peer.uuid != from_instance_id:
                targets.add(peer.uuid)
    targets.discard(from_instance_id)
    # 只保留确实有 apps/<id>/ 目录的(避免订阅 yaml 残留指向已删实例)
    return [t for t in targets if t in instances]
