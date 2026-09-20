"""对话日志 — 平台无关的 SQLite 双向记录。

记录人类消息（in）和模型回复（out），支持飞书/微信/实例间对话等平台。
sense_conversation 工具从此表查询数据。
"""

from __future__ import annotations

import logging
import sqlite3

from infrastructure.config import get_instance_state_db_path
from domain.lifecycle import clock

logger = logging.getLogger(__name__)

_MAX_ENTRIES_PER_CONVERSATION = 200


def _ensure_table() -> sqlite3.Connection:
    """获取 state.db 连接并确保 conversation_log 表存在。"""
    db_path = get_instance_state_db_path()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    # durability: WAL + FULL synchronous 防 WAL 半写损坏（state.db 是 corruption 高发库）。
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS conversation_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL DEFAULT 'feishu',
            conversation_id TEXT NOT NULL,
            chat_type TEXT NOT NULL DEFAULT 'dm',
            direction TEXT NOT NULL,
            text TEXT NOT NULL,
            sender_name TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_conv_lookup
            ON conversation_log(platform, conversation_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_conv_direction
            ON conversation_log(direction, created_at);
    """)
    conn.commit()
    return conn


def log_conversation(
    *,
    platform: str,
    conversation_id: str,
    chat_type: str,
    direction: str,
    text: str,
    sender_name: str = "",
) -> None:
    """写入一条对话记录。

    Args:
        platform: 'lark' | 'wechat' | 'internal'
        conversation_id: 平台内唯一 ID（飞书 oc_xxx、微信 wx_xxx）
        chat_type: 'dm' | 'group' | 'inter_instance'
        direction: 'in' | 'out'
        text: 消息文本
        sender_name: 发送者名称（人类消息时有效）
    """
    now = clock.now_iso()
    try:
        conn = _ensure_table()
        conn.execute(
            "INSERT INTO conversation_log (platform, conversation_id, chat_type, direction, text, sender_name, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (platform, conversation_id, chat_type, direction, text.strip(), sender_name, now),
        )
        conn.commit()
    except Exception as e:
        logger.warning("Failed to log conversation: %s", e)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def read_conversation(
    *,
    platform: str | None = None,
    conversation_id: str | None = None,
    chat_type: str | None = None,
    direction: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[dict]:
    """查询对话记录，支持多维度过滤。

    Args:
        platform: 平台过滤，不传则不过滤
        conversation_id: 对话对象过滤
        chat_type: 'dm' | 'group' 过滤
        direction: 'in' | 'out' 过滤
        limit: 返回条数
        offset: 偏移（翻页用）

    Returns:
        [{id, platform, conversation_id, chat_type, direction, text, sender_name, created_at}, ...]
    """
    try:
        conn = _ensure_table()
        where: list[str] = []
        params: list[str] = []

        if platform:
            where.append("platform=?")
            params.append(platform)
        if conversation_id:
            where.append("conversation_id=?")
            params.append(conversation_id)
        if chat_type:
            where.append("chat_type=?")
            params.append(chat_type)
        if direction:
            where.append("direction=?")
            params.append(direction)

        sql = "SELECT * FROM conversation_log"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([str(limit), str(offset)])

        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("Failed to read conversation log: %s", e)
        return []


def read_recent_sent_from_db(limit: int = 5) -> str:
    """从 conversation_log 读取最近发出的消息（替代 read_recent_sent MD 版本）。"""
    rows = read_conversation(direction="out", limit=limit)
    if not rows:
        return "（还没有发送过消息）"
    lines = []
    for r in rows:
        ts = r["created_at"]
        text = r["text"][:200]
        lines.append(f"{ts} | {text}")
    return "\n".join(lines)


def read_window_context(
    *,
    conversation_id: str,
    platform: str = "lark",
    exclude_text: str = "",
    limit: int = 5,
    self_name: str = "我",
    platform_default: str = "feishu",
) -> str:
    """渲染同窗口最近对话，供 mid-session 事件注入附带（指代消解用）。

    背景（9/18 zhp 提议）：多线程对话时，mid-session 注入的新消息事件只有
    当前消息原文，模型看不到同窗口上文，单字回复（"要"/"先重启think吧"）
    容易被误读为另一话题的审批确认（9/18 10:56 群消息"要"被 11:21 误读）。

    Args:
        conversation_id: 同窗口 ID（oc_xxx）
        platform: 平台，空串回退 feishu（写入侧 ingress pf=feishu）
        exclude_text: 当前正在注入的消息文本（拉取侧已 log，需排除防重复）
        limit: 附带条数
        self_name: direction=out 时的显示名
    Returns:
        渲染好的多行文本；无记录返回 ""（调用方判空不拼接）
    """
    rows = read_conversation(platform=platform or platform_default, conversation_id=conversation_id, limit=limit + 2)
    if not rows:
        return ""
    # 倒序 → 跳过当前消息（首个精确同文项，不限位置——拉取侧 log 与注入之间
    # 可能已插入本实例的 out 记录）→ 截 limit 条 → 反转回正序
    filtered: list[dict] = []
    skipped = False
    for r in rows:
        if (
            exclude_text
            and not skipped
            and r["text"].strip() == exclude_text.strip()
        ):
            skipped = True
            continue
        filtered.append(r)
        if len(filtered) >= limit:
            break
    if not filtered:
        return ""
    filtered.reverse()
    lines = []
    for r in filtered:
        ts = r["created_at"]
        stamp = ts[5:16].replace("T", " ") if len(ts) >= 16 else ts  # MM-DD HH:MM，跨天不误导
        who = self_name if r["direction"] == "out" else (r["sender_name"] or "对方")
        text = r["text"][:100]
        lines.append(f"[{stamp}] {who}：{text}")
    return "\n".join(lines)
