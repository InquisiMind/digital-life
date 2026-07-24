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
# 扩展: 不只是前缀, "帮我"/"帮忙"/"zero"/"dl" 出现在文本任意位置也算 (zhp 口语习惯)
_COMMAND_PREFIXES = ("/zero", "/帮忙", "/dl")
# 关键词 (不含 / 前缀, 任意位置匹配)。仅当 sender 是 zhp 自己时触发——
# 别人说"帮我"不会唤醒模型。
_COMMAND_KEYWORDS = {"zero", "帮忙", "帮我", "帮我做", "dl",}

# ── 自动分类关键词表 ──
_WORK_KEYWORDS = (
    "项目", "需求", "会议", "排期", "上线", "bug", "测试", "交付",
    "工时", "周报", "复盘", "review", "评审", "计划", "进度",
    "代码", "提交", "部署", "验证", "debug", "crash", "错误",
    "紧急", "deadline", "milestone", "任务", "待办",
)
_SOCIAL_KEYWORDS = (
    "吃饭", "午饭", "晚饭", "早餐", "下午茶", "奶茶", "咖啡",
    "周末", "放假", "出去玩", "健身", "运动", "趣", "段子",
    "表情包", "开心", "生日",
)
_NOTIFICATION_KEYWORDS = (
    "全员", "公告", "通知", "请各位", "请大家", "务必",
    "recalled", "撤回", "system", "已读",
)


def _format_relative_ts(ts_raw: float) -> str:
    """Format a Unix timestamp as human-readable relative time.

    Returns '今天 HH:MM' / '昨天 HH:MM' / 'M月D日 HH:MM' / 'YYYY/M/D'.
    The year is always disambiguated so old messages can't be mistaken for recent ones.
    """
    if not ts_raw:
        return ""
    dt = datetime.fromtimestamp(ts_raw / 1000 if ts_raw > 1e12 else ts_raw)
    now = datetime.now()
    delta_days = (now.date() - dt.date()).days
    if delta_days == 0:
        return f"今天 {dt.strftime('%H:%M')}"
    if delta_days == 1:
        return f"昨天 {dt.strftime('%H:%M')}"
    if 0 < delta_days < 7:
        return dt.strftime("%m-%d %H:%M")
    # Older than a week: show full date with year
    return dt.strftime("%Y/%m-%d %H:%M")


def _auto_categorize(
    text: str,
    chat_name: str,
    has_cmd: bool,
    at_me: bool,
    at_all: bool,
    sender_is_app: bool,
) -> str:
    """基于消息内容+群名+元标志自动分 6 类。

    优先级(高到低): command > mention(@我) > notification(通知/广播) > work > social > default
    """
    if has_cmd:
        return "command"
    if at_me:
        return "mention"
    if sender_is_app or "recalled" in text.lower():
        return "system"
    combined = f"{chat_name} {text}".lower()
    if at_all or any(kw in combined for kw in _NOTIFICATION_KEYWORDS):
        return "notification"
    if any(kw in combined for kw in _WORK_KEYWORDS):
        return "work"
    if any(kw in combined for kw in _SOCIAL_KEYWORDS):
        return "social"
    return "default"


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
        # ALTER 兼容老库: 加 reviewed 列 (区分"注上下文了 scanned" vs "模型 review 过了 reviewed")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(social_feed)").fetchall()}
        if "reviewed" not in cols:
            conn.execute("ALTER TABLE social_feed ADD COLUMN reviewed INTEGER NOT NULL DEFAULT 0")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_social_feed_reviewed "
                "ON social_feed(instance_id, scanned, reviewed, message_ts DESC)"
            )
        # 加 at_all + sender_is_app 列(用于过滤噪音/机器人消息)
        if "at_all" not in cols:
            conn.execute("ALTER TABLE social_feed ADD COLUMN at_all INTEGER NOT NULL DEFAULT 0")
        if "sender_is_app" not in cols:
            conn.execute("ALTER TABLE social_feed ADD COLUMN sender_is_app INTEGER NOT NULL DEFAULT 0")
        # at_me: @ 了当前用户(zhp)
        if "at_me" not in cols:
            conn.execute("ALTER TABLE social_feed ADD COLUMN at_me INTEGER NOT NULL DEFAULT 0")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_social_feed_at_me "
                "ON social_feed(instance_id, at_me, scanned, message_ts DESC)"
            )
        # category: 自动分类 (command / mention / work / social / notification / system)
        if "category" not in cols:
            conn.execute("ALTER TABLE social_feed ADD COLUMN category TEXT NOT NULL DEFAULT ''")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_social_feed_category "
                "ON social_feed(instance_id, category, scanned, message_ts DESC)"
            )
        # tags: 逗号分隔的自由标签 (模型可打)
        if "tags" not in cols:
            conn.execute("ALTER TABLE social_feed ADD COLUMN tags TEXT NOT NULL DEFAULT ''")
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
    at_all: bool = False,
    sender_is_app: bool = False,
    at_me: bool = False,
) -> bool:
    """插入一条消息。重复 message_id 静默跳过(OR IGNORE)。
    返回 True = 新插入, False = 已存在。
    """
    has_cmd = 1 if has_command(text) else 0
    # 自动分类 (category): 枚举值
    #   command     — zhp 发的命令 (有 command keyword)
    #   mention     — @了 zhp 或 chat 里有 @所有人的广播
    #   work        — 群名/内容含工作关键词 (项目/需求/会议/排期/上线/bug/测试/交付/工时)
    #   social      — 群名含社交信号 (生活/吃饭/运动/趣/闲聊/水)
    #   notification— 通知/公告类 (全员/公告/系统/ recalled /撤回)
    #   default     — 其它
    category = _auto_categorize(text or "", chat_name or "", has_cmd, bool(at_me), bool(at_all), bool(sender_is_app))
    created = datetime.utcnow().isoformat() + "+00:00"
    conn = sqlite3.connect(str(_state_db_path()))
    try:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO social_feed
                (instance_id, source, chat_id, chat_name, message_id,
                 sender_name, sender_id, text, has_command, scanned,
                 message_ts, created_at, at_all, sender_is_app, at_me,
                 category, tags)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, '')
            """,
            (instance_id, source, chat_id, chat_name, message_id,
             sender_name, sender_id, text, has_cmd, message_ts, created,
             1 if at_all else 0, 1 if sender_is_app else 0, 1 if at_me else 0,
             category),
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


def get_unreviewed_unread(instance_id: str = "", limit: int = 50) -> list[dict[str, Any]]:
    """获取未 review 的最近消息(供 social_review 周期事件消费)。

    semantics:
      scanned = 1 表示已注入某次 wake 的 context (render_social_feed 调用)
      reviewed = 1 表示模型已经过 social_review 事件 review 过(决定 create_todo 或忽略)

    本函数返回 scanned=0 AND reviewed=0: 还没被注入过、也没被 review 过的最新消息。
    social_review handler 拿到这批 → 注入 prompt → 模型决定 actionable → mark_reviewed
    以及 mark_scanned (一次完成避免重复注入)。
    """
    if not _state_db_path().exists():
        return []
    try:
        conn = sqlite3.connect(str(_state_db_path()))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT id, source, chat_name, sender_name, text, has_command,
                       message_ts, reviewed, scanned, at_all, sender_is_app, at_me
                FROM social_feed
                WHERE scanned = 0 AND reviewed = 0
                  AND (instance_id = ? OR ? = '')
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


def mark_reviewed(ids: list[int]) -> None:
    """标记消息为已 reviewed (模型已经过 social_review 决策完毕)。

    通常与 mark_scanned 一起调: review 完既 mark_reviewed 也 mark_scanned,
    让后续其它 wake 不再注入此消息。
    """
    if not ids:
        return
    conn = sqlite3.connect(str(_state_db_path()))
    try:
        placeholders = ",".join("?" * len(ids))
        conn.execute(
            f"UPDATE social_feed SET reviewed = 1, scanned = 1 WHERE id IN ({placeholders})",
            ids,
        )
        conn.commit()
    except sqlite3.Error as exc:
        logger.warning("social_feed mark_reviewed failed: %s", exc)
    finally:
        conn.close()


def has_command(text: str) -> bool:
    """检测消息是否含命令前缀 (/zero / 帮忙 /dl 等)。"""
    stripped = text.strip().lower()
    return any(stripped.startswith(prefix) for prefix in _COMMAND_PREFIXES)


def add_tags(message_id: str, tags: str) -> bool:
    """给一条 social_feed 消息追加自由标签(tags 字段, 逗号分隔)。

    模型可调用: 发现某消息值得标记(like "股票"/"紧急"/"zhp关心的")。
    幂等: 已有相同 tag 不重复追加。
    """
    if not message_id or not tags:
        return False
    conn = sqlite3.connect(str(_state_db_path()))
    try:
        row = conn.execute(
            "SELECT tags FROM social_feed WHERE message_id = ?", (message_id,)
        ).fetchone()
        if not row:
            return False
        existing = set(t.strip() for t in (row[0] or "").split(",") if t.strip())
        new_tags = set(t.strip() for t in tags.split(",") if t.strip())
        merged = ",".join(sorted(existing | new_tags))
        conn.execute(
            "UPDATE social_feed SET tags = ? WHERE message_id = ?",
            (merged, message_id),
        )
        conn.commit()
        return True
    except sqlite3.Error as exc:
        logger.warning("social_feed add_tags failed: %s", exc)
        return False
    finally:
        conn.close()


def is_zhp_command(text: str, sender_id: str, self_open_id: str = "") -> bool:
    """检测是否是 zhp 自己发的含关键词的消息——应立刻唤醒模型。

    判定逻辑:
      - sender 必须是 zhp 本人 (sender_id == self_open_id)
      - text 含命令前缀 /zero / 帮忙 /dl (前缀匹配)
      - 或 text 含关键词 zero/帮忙/帮我/dl (子串匹配, 捕捉口语化触发)

    别人发的同样消息不会触发——保护隐私 + 避免噪音。
    """
    stripped = (text or "").strip().lower()
    if not stripped:
        return False
    # 不是 zhp 本人 → 不即时触发
    if self_open_id and sender_id != self_open_id:
        # 但仍然如果含 /zero 前缀就算（兼容他人显式喊)
        return any(stripped.startswith(prefix) for prefix in _COMMAND_PREFIXES)
    # zhp 自己发的: 检测关键词
    if any(stripped.startswith(prefix) for prefix in _COMMAND_PREFIXES):
        return True
    return any(kw in stripped for kw in _COMMAND_KEYWORDS)


def render_social_feed(instance_id: str = "", limit: int = 30) -> str:
    """渲染最近未读消息为注入文本。调完后自动 mark scanned。

    过滤规则(帮模型过滤噪音):
      - sender_is_app=1 的机器人消息: 跳过(系统通知/机器人回复不是真人意图)
      - 保留所有 has_command=1 / at_all=1 的消息(明确有意图或广播)
      - 其余消息保留但标注优先级: 有 command → 🔡, at_all → ⚠️
    """
    msgs = get_unreviewed_unread(instance_id, limit)
    if not msgs:
        return ""

    # 过滤: 机器人消息不展示(除非 @all 或有 command)
    filtered = []
    for m in msgs:
        if m.get("sender_is_app") and not m.get("at_all") and not m.get("has_command"):
            continue  # 机器人普通消息跳过
        filtered.append(m)
    if not filtered:
        return ""

    lines = ["[zhp 的近况]  📌=@我 ⚠️=@所有人 🔡=含命令前缀"]
    for m in filtered:
        ts_raw = m["message_ts"]
        ts = _format_relative_ts(ts_raw)
        chat = (m.get("chat_name") or "?")[:15]
        sender = (m.get("sender_name") or "?")[:8]
        text = (m.get("text") or "").replace("\n", " ").strip()[:80]
        tags = []
        if m.get("at_me"): tags.append("📌")
        if m.get("has_command"): tags.append("🔡")
        if m.get("at_all"): tags.append("⚠️")
        tag_str = " ".join(tags)
        lines.append(f"  · {ts} {chat} | {sender} {tag_str}: {text}")
    lines.append(f"[/zhp 的近况 · {len(filtered)} 条]")
    mark_scanned([m["id"] for m in filtered])
    return "\n".join(lines)


def render_social_review(instance_id: str = "", limit: int = 50) -> str:
    """渲染待 review 消息为 social_review 事件 prompt 内容。调完后 mark_reviewed。

    与 render_social_feed 的区别:
      - render_social_feed 给默认 wake"近况"注入, mark_scanned (但不 mark_reviewed)
      - render_social_review 给 social_review 周期事件专用, 同时 mark_scanned+mark_reviewed
    这保证: 普通注入看得见但不算"已 review"; social_review 事件才真正决定 actionable。
    """
    msgs = get_unreviewed_unread(instance_id, limit)
    if not msgs:
        return ""
    lines = ["[待 review 的消息]"]
    for m in msgs:
        ts_raw = m["message_ts"]
        ts = _format_relative_ts(ts_raw)
        cmd_tag = " ⚡" if m["has_command"] else ""
        chat = m["chat_name"] or "?"
        sender = m["sender_name"] or "?"
        text = (m["text"] or "").replace("\n", " ").strip()[:200]
        lines.append(f"  · [{ts}] {chat} · {sender}{cmd_tag}: {text}")
    lines.append("[/待 review 的消息]")
    # mark_reviewed 内部同时 mark_scanned
    mark_reviewed([m["id"] for m in msgs])
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
    "get_unreviewed_unread",
    "mark_scanned",
    "mark_reviewed",
    "has_command",
    "_format_relative_ts",
    "render_social_feed",
    "render_social_review",
    "get_pending_commands",
]
