"""SQLite session store used by the project-owned agent runtime."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from infrastructure.config import get_runtime_state_db_path


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    user_id TEXT,
    model TEXT,
    model_config TEXT,
    system_prompt TEXT,
    parent_session_id TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    end_reason TEXT,
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER,
    output_tokens INTEGER,
    title TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    timestamp REAL NOT NULL,
    token_count INTEGER,
    finish_reason TEXT,
    reasoning TEXT,
    reasoning_details TEXT,
    codex_reasoning_items TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, timestamp);
"""


class SessionDB:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or get_runtime_state_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        # durability: WAL + FULL synchronous 防 WAL 半写损坏。
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA busy_timeout=5000")

        # 创建表（不含 segment_index）
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()

        # 兼容旧库：添加 segment_index 列（如果不存在）
        cursor = self._conn.execute("PRAGMA table_info(messages)")
        existing_cols = {row[1] for row in cursor.fetchall()}
        if "segment_index" not in existing_cols:
            self._conn.execute("ALTER TABLE messages ADD COLUMN segment_index INTEGER DEFAULT 0")
            self._conn.commit()

        # 兼容旧库：添加 chat_id 列（用于按 chat 检索 + 多 chat 上下文标识）
        if "chat_id" not in existing_cols:
            self._conn.execute("ALTER TABLE messages ADD COLUMN chat_id TEXT NOT NULL DEFAULT ''")
            self._conn.commit()

        # 创建 segment 索引
        try:
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_segment ON messages(session_id, segment_index)")
            self._conn.commit()
        except Exception:
            pass

        # chat_id 索引
        try:
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_chat "
                "ON messages(session_id, chat_id, timestamp)"
            )
            self._conn.commit()
        except Exception:
            pass
        self._conn.commit()

        # 当前段索引（按 session_id 隔离）
        self._current_segment: dict[str, int] = {}

    def create_session(
        self,
        session_id: str,
        source: str,
        model: str | None = None,
        model_config: dict[str, Any] | None = None,
        system_prompt: str | None = None,
        user_id: str | None = None,
        parent_session_id: str | None = None,
    ) -> str:
        with self._lock:
            cursor = self._conn.execute(
                """INSERT OR IGNORE INTO sessions
                (id, source, user_id, model, model_config, system_prompt, parent_session_id, started_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (session_id, source, user_id, model, json.dumps(model_config) if model_config else None, system_prompt, parent_session_id, time.time()),
            )
            self._conn.commit()
            is_new = cursor.rowcount > 0
        if is_new:
            # 新建 session：从段号 0 开始（写第一条 action_prompt 前 advance_segment
            # 推进到 1）。INSERT OR IGNORE 续接时不动 _current_segment——保留上轮状态，
            # 由 _load_prior_messages_with_compression 调 get_messages 时
            # _restore_segment_index 已经把它恢复到 MAX(seg)。
            self._current_segment[session_id] = 0
        return session_id

    def advance_segment(self, session_id: str) -> int:
        """推进段号到下一段，返回新段号。每个 wake 开始时调用一次。

        设计语义：段号 = wake 序号，单调递增、永不回退。同一 wake 内所有消息
        （user action_prompt / assistant / tool / sys_tool 慢变量注入）共享同一段号。
        这是新段启始的**唯一**入口——append_message 自身不自增。
        """
        self._current_segment[session_id] = self._current_segment.get(session_id, 0) + 1
        return self._current_segment[session_id]

    def end_session(
        self,
        session_id: str,
        end_reason: str,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        """结束一个 session。

        input_tokens / output_tokens 由 scheduler 在 agent 结束后传入（agent
        内部累计到 self.session_*_tokens）。传了就回写到 sessions 表对应列；
        不传保持 NULL（兼容旧调用）。
        """
        with self._lock:
            if input_tokens is not None and output_tokens is not None:
                self._conn.execute(
                    "UPDATE sessions SET ended_at=?, end_reason=?, "
                    "input_tokens=?, output_tokens=? WHERE id=?",
                    (time.time(), end_reason,
                     int(input_tokens), int(output_tokens), session_id),
                )
            else:
                self._conn.execute(
                    "UPDATE sessions SET ended_at=?, end_reason=? WHERE id=?",
                    (time.time(), end_reason, session_id),
                )
            self._conn.commit()

    def replace_sys_tool_messages(        self,
        session_id: str,
        tool_name: str,
        assistant_msg: dict[str, Any],
        tool_result_content: str,
    ) -> None:
        """原子 DELETE + 写入 sys_tool 的 assistant tool_call + tool result pair。

        用于慢变量注入：每次 wake 都重写当前 session 下同 tool_name 的两条
        （角色 assistant 占位 + tool result），保持最新版本，不累积。
        操作全程在 self._lock 内 + 一个事务，避免中间态让其他 thread 拉到空 history。

        Args:
            session_id: 当前 session id
            tool_name: 慢变量类别（如 session_digest / consciousness / task_board）
            assistant_msg: {role: assistant, content: None, tool_calls: [...]}
            tool_result_content: tool_result row 的 content 字符串
        """
        if not session_id or not tool_name:
            return
        tool_calls_json = json.dumps(
            assistant_msg.get("tool_calls") or [],
            ensure_ascii=False,
            default=str,
        )
        tool_call_id = ""
        if assistant_msg.get("tool_calls"):
            tool_call_id = assistant_msg["tool_calls"][0].get("id", "") or ""
        # 新 tool_call 行用合并后的 segment_index：上一段 +1 即可（占位 user 风格）
        segment_index = self._current_segment.get(session_id, 0)
        with self._lock:
            try:
                # 原子操作：DELETE 同 session + 同 tool_name + sys_ 前缀 tool_call_id 的旧条目
                # → INSERT 新条目
                # 关键：只删 sys_tool 注入的（agent._sys_tool_call 用 "sys_NNN" 累计 ID），
                # 模型手动调用的同名工具（tool_call_id 形如 "call_xxx" 或飞书自带格式）保留，
                # 不会被误删。
                self._conn.execute(
                    "DELETE FROM messages "
                    "WHERE session_id = ? AND role = 'tool' AND tool_name = ? "
                    "AND tool_call_id LIKE 'sys_%'",
                    (session_id, tool_name),
                )
                # 删除匹配的 sys_tool assistant 占位（content NULL/空 + tool_calls 含 sys_NNN + 同 name）
                # 模型手动调用的 assistant 行不会匹配（其 tool_call_id 不以 sys_ 前缀）
                try:
                    self._conn.execute(
                        'DELETE FROM messages WHERE session_id = ? AND role = "assistant" '
                        'AND (content IS NULL OR content = "") '
                        'AND tool_calls LIKE ? '
                        'AND tool_calls LIKE ?',
                        (session_id, f'%"name": "{tool_name}"%', '%"id": "sys_%'),
                    )
                except Exception:
                    pass

                # INSERT 新条目
                self._conn.execute(
                    """INSERT INTO messages
                    (session_id, role, content, tool_call_id, tool_calls, tool_name,
                     timestamp, token_count, finish_reason, reasoning, reasoning_details,
                     codex_reasoning_items, segment_index, chat_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        session_id,
                        "assistant",
                        None,
                        tool_call_id,
                        tool_calls_json,
                        None,
                        time.time(),
                        None,
                        None,
                        None,
                        None,
                        None,
                        segment_index,
                        "",
                    ),
                )
                self._conn.execute(
                    """INSERT INTO messages
                    (session_id, role, content, tool_call_id, tool_calls, tool_name,
                     timestamp, token_count, finish_reason, reasoning, reasoning_details,
                     codex_reasoning_items, segment_index, chat_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        session_id,
                        "tool",
                        tool_result_content,
                        tool_call_id,
                        None,
                        tool_name,
                        time.time(),
                        None,
                        None,
                        None,
                        None,
                        None,
                        segment_index,
                        "",
                    ),
                )
                # 同步 sessions 表的 message_count + tool_call_count
                self._conn.execute(
                    "UPDATE sessions SET message_count = message_count + 2, "
                    "tool_call_count = tool_call_count + 1 WHERE id = ?",
                    (session_id,),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str | None = None,
        tool_name: str | None = None,
        tool_calls: Any = None,
        tool_call_id: str | None = None,
        token_count: int | None = None,
        finish_reason: str | None = None,
        reasoning: str | None = None,
        reasoning_details: Any = None,
        codex_reasoning_items: Any = None,
        chat_id: str = "",
    ) -> int:
        # segment_index：直接读取当前段号。同一 wake 内的所有 role（user/assistant/
        # tool/system）共享同一段号；新 wake 由 advance_segment() 推进。这里**不自增、
        # 不偏移**——避免历史上"读后写产生 -1 段 + 段位混乱"的 bug。
        # 历史 bug 复盘：曾经 user 触发自增并 -1、其它 role 直接读 -1，叠加 get_messages
        # 中途被 _restore_segment_index reset 到 MAX(seg) 的副作用，让"读后写"的 row 都进
        # 了段 -1，破坏了用户/助手交错。
        segment_index = self._current_segment.get(session_id, 0)

        tool_calls_json = json.dumps(tool_calls, ensure_ascii=False, default=str) if tool_calls else None
        with self._lock:
            cursor = self._conn.execute(
                """INSERT INTO messages
                (session_id, role, content, tool_call_id, tool_calls, tool_name, timestamp, token_count,
                 finish_reason, reasoning, reasoning_details, codex_reasoning_items, segment_index, chat_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    role,
                    content,
                    tool_call_id,
                    tool_calls_json,
                    tool_name,
                    time.time(),
                    token_count,
                    finish_reason,
                    reasoning,
                    json.dumps(reasoning_details, ensure_ascii=False, default=str) if reasoning_details else None,
                    json.dumps(codex_reasoning_items, ensure_ascii=False, default=str) if codex_reasoning_items else None,
                    segment_index,
                    chat_id or "",
                ),
            )
            tool_count = len(tool_calls) if isinstance(tool_calls, list) else (1 if tool_calls else 0)
            self._conn.execute(
                "UPDATE sessions SET message_count=message_count+1, tool_call_count=tool_call_count+? WHERE id=?",
                (tool_count, session_id),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def get_messages(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM messages WHERE session_id=? ORDER BY timestamp, id", (session_id,)).fetchall()
        messages = [dict(row) for row in rows]
        for message in messages:
            if message.get("tool_calls"):
                try:
                    message["tool_calls"] = json.loads(message["tool_calls"])
                except Exception:
                    message["tool_calls"] = []
        # 延续 session 时恢复 segment_index
        self._restore_segment_index(session_id)
        return messages

    def _restore_segment_index(self, session_id: str) -> None:
        """延续 session 时，从 DB 恢复当前 segment_index。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(segment_index) as max_seg FROM messages WHERE session_id=?",
                (session_id,),
            ).fetchone()
        self._current_segment[session_id] = max(row["max_seg"] or 0, 0)

    def get_messages_by_chat(
        self, chat_id: str, limit: int = 20, exclude_session_id: str = ""
    ) -> list[dict[str, Any]]:
        """跨 session 拉某 chat 的近 N 条 messages（含 user/assistant/tool）。

        用于群聊/私聊 wake 时，让模型看到该 chat 在所有历史 session 中的对话
        （中的最近一段），而不限于当前 session_id。

        Args:
            chat_id: 目标 chat (oc_xxx)
            limit: 拉多少条（按时间倒序，最后逆序返回时间正序）
            exclude_session_id: 排除某 session（避免与 prev_history already-loaded 重复）
        """
        if not chat_id:
            return []
        sql = (
            "SELECT * FROM messages WHERE chat_id = ? "
            "AND role IN ('user', 'assistant')"
        )
        params: list = [chat_id]
        if exclude_session_id:
            sql += " AND session_id != ?"
            params.append(exclude_session_id)
        sql += " ORDER BY timestamp DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        rows = [dict(r) for r in rows]
        for m in rows:
            if m.get("tool_calls"):
                try:
                    m["tool_calls"] = json.loads(m["tool_calls"])
                except Exception:
                    m["tool_calls"] = []
        # 时间正序（最早到最近）
        return list(reversed(rows))

    def get_messages_in_segment(self, session_id: str, segment_index: int) -> list[dict[str, Any]]:
        """获取指定段的 messages。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE session_id=? AND segment_index=? ORDER BY timestamp, id",
                (session_id, segment_index),
            ).fetchall()
        messages = [dict(row) for row in rows]
        for message in messages:
            if message.get("tool_calls"):
                try:
                    message["tool_calls"] = json.loads(message["tool_calls"])
                except Exception:
                    message["tool_calls"] = []
        return messages

    def get_segment_count(self, session_id: str) -> int:
        """获取 session 的段数量。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(segment_index) as max_seg FROM messages WHERE session_id=?",
                (session_id,),
            ).fetchone()
        return (row["max_seg"] or 0) + 1

    def get_all_segments(self, session_id: str) -> list[list[dict[str, Any]]]:
        """获取 session 的所有段，每段一个列表。"""
        messages = self.get_messages(session_id)
        segments: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        current_index = -1
        for msg in messages:
            seg_idx = msg.get("segment_index", 0)
            if seg_idx != current_index:
                if current:
                    segments.append(current)
                current = [msg]
                current_index = seg_idx
            else:
                current.append(msg)
        if current:
            segments.append(current)
        return segments

    def get_message_count(self, session_id: str) -> int:
        """获取 session 的消息数量。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) as cnt FROM messages WHERE session_id=?",
                (session_id,),
            ).fetchone()
        return row["cnt"] or 0

    def get_tool_call_count(self, session_id: str) -> int:
        """获取 session 的工具调用次数。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT tool_call_count FROM sessions WHERE id=?",
                (session_id,),
            ).fetchone()
        return row["tool_call_count"] or 0 if row else 0

    def get_tool_message_by_call_id(
        self, session_id: str, tool_call_id: str
    ) -> dict[str, Any] | None:
        """精确按 tool_call_id 取回一条 tool 消息原文。

        给 recall_tool_result 工具用：上下文压缩只发生在发给 LLM 的 payload，
        DB 永远保留原始 content。LLM 调 recall_tool_result(tool_call_id=...) 时，
        通过本方法拿回该次工具调用的完整结果。

        跨 session 查找: continuation session 里模型看到的压缩指针来自前一个 session,
        所以当前 session_id 找不到时自动 fallback 到全库查(同一个 SessionDB = 同一个实例)。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT tool_name, content, timestamp FROM messages "
                "WHERE session_id=? AND tool_call_id=? AND role='tool' LIMIT 1",
                (session_id, tool_call_id),
            ).fetchone()
            if not row:
                # 跨 session fallback: continuation session 的压缩指针引用了旧 session 的 tool_call
                row = self._conn.execute(
                    "SELECT tool_name, content, timestamp FROM messages "
                    "WHERE tool_call_id=? AND role='tool' LIMIT 1",
                    (tool_call_id,),
                ).fetchone()
        if not row:
            return None
        return {
            "tool_name": row["tool_name"] or "",
            "content": row["content"] or "",
            "timestamp": float(row["timestamp"] or 0),
        }

