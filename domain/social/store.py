"""Social feed storage — 从用户 IM 账号拉取的消息持久化。

Schema:
  social_feed(id, instance_id, source, chat_id, chat_name, message_id, sender_name,
              sender_id, text, has_command, scanned, message_ts, created_at)

一个消息存一条, message_id 唯一去重。model wake 时从 unread 行注入上下文。
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 命令前缀 — zhp 在任意群里用这些触发数字生命(不需要 bot 在群里)
_COMMAND_PREFIXES = ("/zero", "/帮忙", "/dl")


def _state_db_path() -> Path:
    from infrastructure.config import get_runtime_state_db_path
    return get_runtime_state_db_path()


def ensure_schema() -> None:
    """建表（幂等）。在 instance ContextVar 已设置后调用。"""
    db_path = _state_db_path()
    if not db_path.exists():
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS social_feed (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instance_id TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'feishu',
                chat_id TEXT NOT NULL,
                chat_name TEXT NOT NULL DEFAULT '',
                message_id TEXT NOT NULL,
                sender_name TEXT NOT NULL DEFAULT '',
                sender_id TEXT NOT NULL DEFAULT '',
                text TEXT DEFAULT '',
                has_command INTEGER NOT NULL DEFAULT 0,
                scanned INTEGER NOT NULL DEFAULT 0,
                message_ts REAL NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT '',
                UNIQUE(message_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_social_feed_unread "
            "ON social_feed(instance_id, scanned, message_ts DESC)"
        )
        conn.commit()
    finally:
        conn.close()


def insert_message(
    *,
    source: str = "feishu",
    chat_id: str,
    chat_name: str = "",
    message_id: str,
    sender_name: str = "",
    sender_id: str = "",
    text: str = "",
    message_ts: float = 0,
    instance_id: str = "",
) -> bool:
    """插入一条消息。重复 message_id 静默跳过(OR IGNORE)。
    返回 True = 新插入, False = 已存在。
    """
    has_cmd = 1 if has_command(text) else 0
    created = datetime.utcnow().isoformat() + "+00:00"
    conn = sqlite3.connect(str(_state_db_path()))
    try:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO social_feed
                (instance_id, source, chat_id, chat_name, message_id,
                 sender_name, sender_id, text, has_command, scanned, message_ts, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (instance_id, source, chat_id, chat_name, message_id,
             sender_name, sender_id, text, has_cmd, message_ts, created),
        )
        conn.commit()
        return cur.rowcount > 0
    except sqlite3.Error as exc:
        logger.warning("social_feed insert failed: %s", exc)
        return False
    finally:
        conn.close()


def get_recent_unread(instance_id: str = "", limit: int = 30) -> list[dict[str, Any]]:
    """获取未 scanned 的最近消息(用于 wake 时注入上下文)。"""
    if not _state_db_path().exists():
        return []
    try:
        conn = sqlite3.connect(str(_state_db_path()))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT id, source, chat_name, sender_name, text, has_command, message_ts
                FROM social_feed
                WHERE scanned = 0 AND (instance_id = ? OR ? = '')
                ORDER BY message_ts DESC
                LIMIT ?
                """,
                (instance_id, instance_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def mark_scanned(ids: list[int]) -> None:
    """标记消息为已 scanned (已注入到模型上下文)。"""
    if not ids:
        return
    conn = sqlite3.connect(str(_state_db_path()))
    try:
        placeholders = ",".join("?" * len(ids))
        conn.execute(
            f"UPDATE social_feed SET scanned = 1 WHERE id IN ({placeholders})",
            ids,
        )
        conn.commit()
    except sqlite3.Error as exc:
        logger.warning("social_feed mark_scanned failed: %s", exc)
    finally:
        conn.close()


def has_command(text: str) -> bool:
    """检测消息是否含命令前缀 (/zero / 帮忙 /dl 等)。"""
    stripped = text.strip().lower()
    return any(stripped.startswith(prefix) for prefix in _COMMAND_PREFIXES)


def render_social_feed(instance_id: str = "", limit: int = 30) -> str:
    """渲染最近未读消息为注入文本。调完后自动 mark scanned。"""
    msgs = get_recent_unread(instance_id, limit)
    if not msgs:
        return ""
    lines = ["[zhp 的近况]"]
    for m in msgs:
        ts = datetime.fromtimestamp(m["message_ts"]).strftime("%H:%M") if m["message_ts"] else ""
        cmd_tag = " ⚡" if m["has_command"] else ""
        chat = m["chat_name"] or "?"
        sender = m["sender_name"] or "?"
        text = (m["text"] or "").replace("\n", " ").strip()[:120]
        lines.append(f"  · {ts} {chat} · {sender}{cmd_tag}: {text}")
    lines.append("[/zhp 的近况]")
    # 标记 scanned
    mark_scanned([m["id"] for m in msgs])
    return "\n".join(lines)


def get_pending_commands(instance_id: str = "") -> list[dict[str, Any]]:
    """获取未处理的 command 消息(用于触发 wake)。"""
    if not _state_db_path().exists():
        return []
    try:
        conn = sqlite3.connect(str(_state_db_path()))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT id, source, chat_id, chat_name, message_id,
                       sender_name, sender_id, text
                FROM social_feed
                WHERE has_command = 1 AND scanned = 0
                  AND (instance_id = ? OR ? = '')
                ORDER BY message_ts DESC
                """,
                (instance_id, instance_id),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except sqlite3.Error:
        return []


__all__ = [
    "ensure_schema",
    "insert_message",
    "get_recent_unread",
    "mark_scanned",
    "has_command",
    "render_social_feed",
    "get_pending_commands",
]
