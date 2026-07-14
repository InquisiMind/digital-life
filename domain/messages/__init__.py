"""每实例独立消息库(messages.db)—去中心化消息总线的一部分。

设计参见 ``docs/architecture/decentralized-message-bus.md``。

核心原则:
- 每个实例一个独立 SQLite 文件 ``apps/<id>/data/messages.db``
- 不共享任何 DB 跨实例;跨实例同步仅靠同群 HTTP 广播(Phase 3 才上)
- 字段 sender_id 是平台视角(per-app open_id),永不跨实例输出
- 不维护"全局统一同一个人"映射(unified_contacts 那套伪需求)

与 ``domain/conversations`` 的关系:Phase 4 之前,conversations 老的
``list_chat_messages``/``record_inbound_message``/``publish_chat_message``
会被重写成 forwards 到本模块。Phase 4 才彻底删 conversations 那套
(unified_contacts / chats.db)。

本模块只管"读写实例本地 messages.db"。广播是 publish_chat_message 的副作用,
单独函数做(HTTP 端点在 Phase 3 加)。
"""
from __future__ import annotations

import logging
import json
import os
import sqlite3
import threading
import time
import uuid as _uuid
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# 路径解析:每实例一个 messages.db
# ─────────────────────────────────────────────────────────────────────────────

_INSTANCE_DATA_PATH_OVERRIDE: Optional[Path] = None  # 仅测试用


def _instance_data_dir(instance_id: str | None = None) -> Path:
    """返回当前(或指定)实例的 data 目录路径:apps/<id>/data/。

    当 ``instance_id`` 显式传入时,绕过 ContextVar 默认值——master 进程
    接收 peer 广播时用此写入目标实例库(避免错写到 master 默认实例)。
    """
    if instance_id is None and _INSTANCE_DATA_PATH_OVERRIDE is not None:
        return _INSTANCE_DATA_PATH_OVERRIDE
    if instance_id is not None:
        from infrastructure.config import get_instance_data_dir
        return get_instance_data_dir(instance_id)
    from infrastructure.config import get_instance_data_dir
    return get_instance_data_dir()


def messages_db_path(instance_id: str | None = None) -> Path:
    """当前(或指定)实例的 messages.db 路径(通常 ``apps/<id>/data/messages.db``)。"""
    return _instance_data_dir(instance_id) / "messages.db"


# ─────────────────────────────────────────────────────────────────────────────
# 时间戳 — followup/messages-utc-and-flow-doc 后统一用 kvm 本地时区,跟 clock.py 对齐
# ─────────────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    """本地 ISO8601(带毫秒),跟着 ``domain.lifecycle.clock`` 走。

    历史版本用 ``datetime.now(timezone.utc)`` 存 UTC,跟 conversation_log /
    events 表都用本地时区(+08:00)不一致——读各自表的人会混淆。
    followup/messages-utc-and-flow-doc(2026-06-23)统一到本地。

    本地时区由 ``domain.lifecycle.clock.LOCAL`` 提供,部署在 CST 机器上就是
    +08:00。带毫秒保留(per-call ordering 用)。

    保留模块内本地函数(而非直接调 clock.now_iso)的原因:本模块自己声明
    不依赖 ContextVar,跟 events 那种带 _instance_channel_var 的模块解耦。
    """
    from domain.lifecycle.clock import now_dt
    return now_dt().isoformat(timespec="milliseconds")


# ─────────────────────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────────────────────

_SCHEMA_LOCK = threading.Lock()


def _ensure_schema(instance_id: str | None = None) -> None:
    """幂等创建 messages 表。

    单线程锁保证多实例并发 init 时只跑一次(thr-safe within-process)。
    跨进程多个实例各自 init 各自库,不会冲突。

    当 ``instance_id`` 传入时,对目标实例库建表——master 中转广播用。
    """
    p = messages_db_path(instance_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    with _SCHEMA_LOCK:
        conn = sqlite3.connect(str(p))
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS messages (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts              TEXT NOT NULL,
                    direction       TEXT NOT NULL CHECK (direction IN ('in', 'out')),
                    source          TEXT NOT NULL,
                    chat_id         TEXT NOT NULL,
                    platform_sender TEXT NOT NULL DEFAULT '',
                    sender_name     TEXT NOT NULL DEFAULT '',
                    sender_role     TEXT NOT NULL DEFAULT '',
                    text            TEXT NOT NULL,
                    msg_ref         TEXT NOT NULL DEFAULT '',
                    UNIQUE (source, msg_ref)
                );
                CREATE INDEX IF NOT EXISTS idx_messages_chat_ts
                    ON messages(chat_id, ts DESC);
                CREATE INDEX IF NOT EXISTS idx_messages_chat_id_ts
                    ON messages(chat_id, id DESC);
            """)
            # 多模态附件：多模态消息进来时存 attachment_id 列表（JSON array）
            # migration：老库表已存在时补列。ALTER TABLE ADD COLUMN 没有 IF NOT EXISTS
            # 原生支持，需要先 PRAGMA table_info 判断
            cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
            if "attachments_json" not in cols:
                conn.execute(
                    "ALTER TABLE messages ADD COLUMN attachments_json TEXT NOT NULL DEFAULT ''"
                )
            conn.commit()
        finally:
            conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# 读写 API(对外)
# ─────────────────────────────────────────────────────────────────────────────

def record_message(
    *,
    direction: str,
    source: str,
    chat_id: str,
    sender_name: str = "",
    text: str,
    msg_ref: str = "",
    platform_sender: str = "",
    sender_role: str = "",
    instance_id: str | None = None,
    attachments: list[str] | None = None,
) -> Optional[int]:
    """通用 INSERT,带 (source, msg_ref) 去重。

    返回插入后的 row id;若是 OR IGNORE 冲突(重复),返回既有行的 id。

    direction: 'in' / 'out'
    source: 'lark' / 'wechat' / ... / 'broadcast:<peer_uuid>'
    chat_id: 群 oc_ 或私聊 ou_
    platform_sender: 入站时填对方 open_id(per-app 视角,永不外漏);
                     出站时填 self_uuid;广播写入时填 from_uuid
    sender_name: display_name(给模型看的)
    sender_role: 'self'/'human'/'bot-broadcast'/'other'
    msg_ref: 平台原生 msg_id 或广播来源自带,配 source 做去重
    instance_id: 显式指定写入目标实例库。master 进程接收广播中转时必传,
                 绕过 ContextVar 默认值(否则会错写到 master 默认实例)。
    attachments: 可选的 attachment_id 列表（多模态附件）。JSON 序列化存 attachments_json 列。
    """
    if not chat_id or text is None or not str(text).strip():
        return None
    _ensure_schema(instance_id)
    p = messages_db_path(instance_id)
    if not msg_ref:
        msg_ref = f"local_{direction}_{int(time.time() * 1000)}_{(platform_sender or sender_name or 'x')[:8]}_{_uuid.uuid4().hex[:6]}"
    now = _now_iso()
    attachments_json = json.dumps(attachments, ensure_ascii=False) if attachments else ""

    conn = sqlite3.connect(str(p))
    try:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO messages "
            "(ts, direction, source, chat_id, platform_sender, sender_name, "
            " sender_role, text, msg_ref, attachments_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now, direction, source, chat_id, platform_sender, sender_name,
             sender_role, text, msg_ref, attachments_json),
        )
        conn.commit()
        if cursor.rowcount > 0:
            return cursor.lastrowid
        # 命中 UNIQUE 冲突,返回既有行的 id
        row = conn.execute(
            "SELECT id FROM messages WHERE source=? AND msg_ref=?",
            (source, msg_ref),
        ).fetchone()
        return row[0] if row else None
    except Exception as exc:
        logger.warning("record_message failed (chat=%s dir=%s): %s",
                       chat_id[:16], direction, exc)
        return None
    finally:
        conn.close()


def record_inbound(
    *,
    chat_id: str,
    sender_id: str,
    sender_name: str = "",
    text: str,
    msg_id: str = "",
    source: str = "feishu",
    sender_kind: str = "human",
    attachments: list[str] | None = None,
) -> Optional[int]:
    """入站消息:实例自己收到平台消息时调一次。

    sender_id = 平台视角的 sender open_id(per-app,存进 platform_sender)。
    sender_kind: 'human' / 'bot'(决定 sender_role)。
    attachments: 可选的 attachment_id 列表（多模态入站——图片/文件）。
    """
    role = "human" if sender_kind == "human" else "other"
    return record_message(
        direction="in",
        source=source,
        chat_id=chat_id,
        sender_name=sender_name,
        text=text,
        msg_ref=msg_id,
        platform_sender=sender_id,
        sender_role=role,
        attachments=attachments,
    )


def record_outbound(
    *,
    chat_id: str,
    self_display_name: str,
    self_instance_id: str,
    text: str,
    msg_id: str = "",
    source: str = "feishu",
) -> Optional[int]:
    """出站消息:实例自己发群/私聊消息成功后调。

    self_instance_id 是当前实例 UUID,写入 platform_sender 字段。
    self_display_name 是 'zero'/'alpha' 等,写入 sender_name。
    """
    return record_message(
        direction="out",
        source=source,
        chat_id=chat_id,
        sender_name=self_display_name,
        text=text,
        msg_ref=msg_id,
        platform_sender=self_instance_id,
        sender_role="self",
    )


def record_broadcast_in(
    *,
    chat_id: str,
    from_display_name: str,
    from_instance_id: str,
    text: str,
    msg_ref: str,
    source_platform: str = "feishu",
    instance_id: str | None = None,
) -> Optional[int]:
    """接收广播:peer 实例 HTTP POST 过来后,本实例写一行 'in'。

    source 形如 'broadcast:<from_uuid>',与 (source, msg_ref) 配合做去重。
    sender_role='bot-broadcast' 让模型识别这是兄弟实例说的话。

    source_platform:这条消息原本是哪个平台发的(lark/wechat/dingtalk),
                    目前仅作信息保留,去重用 source 是 broadcast 前缀。
    instance_id:写入目标实例库。master 进程中转广播时必传——若 master 本身
                 不在 ContextVar 默认值匹配的目标实例上,会写到错误的实例库。
    """
    return record_message(
        direction="in",
        source=f"broadcast:{from_instance_id}",
        chat_id=chat_id,
        sender_name=from_display_name,
        text=text,
        msg_ref=msg_ref,
        platform_sender=from_instance_id,
        sender_role="bot-broadcast",
        instance_id=instance_id,
    )


def list_messages(chat_id: str, limit: int = 30) -> list[dict]:
    """读当前实例 messages.db 里某 chat 的最近消息(按 id DESC,再 reverse 成
    ts ASC)。返回的字典字段保留兼容旧 chat_messages API:

    - id, chat_id, msg_id (= msg_ref), sender_id (= platform_sender),
      sender_name, sender_kind (= sender_role), text, created_at (= ts)
    """
    if not chat_id:
        return []
    _ensure_schema()
    p = messages_db_path()
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, ts, direction, source, chat_id, platform_sender, "
            "       sender_name, sender_role, text, msg_ref, attachments_json "
            "FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            # 兼容旧 API 的字段名(checkout chat_messages 那边的字段):
            # id/chat_id/text 已对齐;另映射几组别名供上层用
            d["msg_id"] = d["msg_ref"]
            d["sender_id"] = d["platform_sender"]
            d["sender_kind"] = d["sender_role"]
            d["created_at"] = d["ts"]
            # 多模态附件 ID 列表（JSON 字符串解析，空 = []）
            att_json = d.pop("attachments_json", "") or ""
            try:
                d["attachments"] = json.loads(att_json) if att_json else []
            except (ValueError, TypeError):
                d["attachments"] = []
            out.append(d)
        out.reverse()  # 时间正序
        return out
    except Exception as exc:
        logger.warning("list_messages failed (chat=%s): %s", chat_id[:16], exc)
        return []
    finally:
        conn.close()


def list_plain_text(chat_id: str, limit: int = 30) -> str:
    """格式化成 chat_stream 段渲染需要的纯文本。

    形如::

        张三: hello
        zero: hi

    出站消息用 self 行;广播消息直接显示 from_display_name。
    """
    msgs = list_messages(chat_id, limit=limit)
    lines = []
    for m in msgs:
        role = m.get("sender_role", "")
        name = m.get("sender_name") or "?"
        text = (m.get("text") or "").strip()
        if not text:
            continue
        # 出站消息(sender_role='self')用前缀米以便区分,但仍然显示 display_name
        # 让人/模型理解"这是 zero/alpha 说的"
        prefix = "" if role != "self" else ""  # 暂不加 prefix,保持自然对话流
        line = f"{prefix}{name}：{text}"
        # 多模态附件：每条若有 attachments 列表，行末附 [附件 xxx] 提示——
        # 让 chat_stream 段给模型看到"这里有图，需要用 sense_image 查看"
        atts = m.get("attachments") or []
        if atts:
            att_hint = ", ".join(f"[附件 {a}]" for a in atts)
            line += f" {att_hint}"
        lines.append(line)
    return "\n".join(lines)


__all__ = [
    "messages_db_path",
    "_ensure_schema",
    "record_message",
    "record_inbound",
    "record_outbound",
    "record_broadcast_in",
    "list_messages",
    "list_plain_text",
]
