"""Runtime audit DB: per-instance ``apps/<id>/data/runtime_log.db``.

Three tables — wake / turn / injection — separate "what really happened"
(real LLM turns) from "what we told the model" (fake-tool injections) so
the front-end can reconstruct any LLM call's input without redundancy.

The ``wake`` table is intentionally lean. Variable fields (trigger type,
tokens, model id, end reason, etc.) live in ``meta_json`` — schema stays
stable while the audit surface grows.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from infrastructure.config import get_app_instance_id, get_instance_data_dir
from infrastructure.persistence.instance.base import InstanceDB


SCHEMA_WAKE = """
CREATE TABLE IF NOT EXISTS wake (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id TEXT NOT NULL,
    wake_seq INTEGER NOT NULL,
    meta_json TEXT NOT NULL DEFAULT '{}',
    started_at REAL NOT NULL,
    ended_at REAL,
    UNIQUE(instance_id, wake_seq)
);
CREATE INDEX IF NOT EXISTS idx_wake_started ON wake(started_at DESC);
"""

SCHEMA_TURN = """
CREATE TABLE IF NOT EXISTS turn (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id TEXT NOT NULL,
    wake_id INTEGER NOT NULL REFERENCES wake(id),
    wake_seq INTEGER NOT NULL,
    llm_call_seq INTEGER NOT NULL,
    position_in_call INTEGER NOT NULL DEFAULT 0,
    role TEXT NOT NULL,
    content TEXT,
    tool_name TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    reasoning TEXT,
    finish_reason TEXT,
    token_count INTEGER,
    chat_id TEXT,
    error TEXT,
    timestamp REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turn_order ON turn(wake_id, llm_call_seq, position_in_call);
CREATE INDEX IF NOT EXISTS idx_turn_chat ON turn(chat_id, timestamp);
"""

SCHEMA_INJECTION = """
CREATE TABLE IF NOT EXISTS injection (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id TEXT NOT NULL,
    wake_id INTEGER NOT NULL REFERENCES wake(id),
    wake_seq INTEGER NOT NULL,
    sys_tool TEXT NOT NULL,
    scope_id TEXT NOT NULL DEFAULT '*',
    content TEXT,
    memory_refs TEXT,
    injected_before_call INTEGER NOT NULL DEFAULT 0,
    injected_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_injection_pos ON injection(wake_id, injected_before_call);
CREATE INDEX IF NOT EXISTS idx_injection_scope ON injection(instance_id, sys_tool, scope_id);
"""


# sys_tool 名 → dedup 策略。未来新增 fake tool 在此扩表。
DEDUP_STRATEGY: dict[str, str] = {
    "system_context": "latest",
    "session_digest": "latest",
    "consciousness": "latest",
    "social_context": "latest",
    "task_skill": "latest",
    "my_context": "latest",
    "task_board": "latest",
    "schedule": "latest",
    "wake_signal": "latest",
    "chat_stream": "append",
    "entity_recall": "append",
}


def _dedup_strategy(sys_tool: str) -> str:
    """Default append; explicit ``latest`` only for the slow_ctx rotation group."""
    if sys_tool in DEDUP_STRATEGY:
        return DEDUP_STRATEGY[sys_tool]
    if sys_tool.startswith("narrative_"):
        return "append"
    return "append"


class RuntimeLogDB(InstanceDB):
    SCHEMA_SQL = (SCHEMA_WAKE, SCHEMA_TURN, SCHEMA_INJECTION)

    def __init__(self, db_path: Path | None = None, instance_id: str | None = None) -> None:
        if db_path is None:
            db_path = get_instance_data_dir(instance_id) / "runtime_log.db"
        super().__init__(db_path)
        self.instance_id = instance_id or get_app_instance_id() or ""
        self._ensure_session_id_columns()

    def _ensure_session_id_columns(self) -> None:
        """幂등: wake/turn/injection 三表加 session_id 列(向下兼容)。"""
        try:
            for table in ("wake", "turn", "injection"):
                cols = [r[1] for r in self.fetchone(f"PRAGMA table_info({table})")]
                # fetchall not available via fetchone, use raw
                rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
                col_names = [r[1] for r in rows]
                if "session_id" not in col_names:
                    self.execute(f"ALTER TABLE {table} ADD COLUMN session_id TEXT DEFAULT ''")
                    self.execute(
                        f"CREATE INDEX IF NOT EXISTS idx_{table}_session ON {table}(session_id)"
                    )
            self.commit()
        except Exception:
            pass

    # ---- wake -------------------------------------------------------------

    def next_wake_seq(self) -> int:
        """下一个 wake_seq。

        历史 BUG: 只看 ``SELECT MAX(wake_seq) FROM wake``,但 wake.start 可能
        因为 audit DB 异常而失败,turn 表却已经写入(标了 251 / wake_id=251)。
        下一个成功 wake 又会拿到同一个 seq(因为 wake 表无对应行,MAX+1 还是 251),
        撞上一批幽灵 turn,导致前端 wake 详情串台(started_at=B,但 turn 是 14h 前的 A)。

        修复:取 wake + turn 两表 wake_seq 的最大值,确保单调,绝不复用。
        额外开销一次 SELECT,几十微秒,可以接受。
        """
        row = self.fetchone(
            "SELECT MAX(m) AS m FROM ("
            " SELECT MAX(wake_seq) AS m FROM wake WHERE instance_id = ?"
            " UNION ALL"
            " SELECT MAX(wake_seq) AS m FROM turn WHERE instance_id = ?"
            ")",
            (self.instance_id, self.instance_id),
        )
        cur = (row or {}).get("m")
        return (cur + 1) if cur is not None else 1

    def create_wake(
        self,
        *,
        wake_seq: int | None = None,
        meta: dict[str, Any] | None = None,
        started_at: float | None = None,
        session_id: str = "",
    ) -> int:
        """Start a wake. Variable fields go in ``meta`` (trigger, model, etc.)."""
        seq = wake_seq if wake_seq is not None else self.next_wake_seq()
        cur = self.execute(
            "INSERT INTO wake (instance_id, wake_seq, meta_json, started_at, session_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                self.instance_id,
                seq,
                json.dumps(meta or {}, ensure_ascii=False, default=str),
                started_at if started_at is not None else time.time(),
                session_id,
            ),
        )
        self.commit()
        return int(cur.lastrowid)

    def end_wake(
        self,
        wake_id: int,
        *,
        meta_updates: dict[str, Any] | None = None,
        ended_at: float | None = None,
    ) -> None:
        """Mark wake ended + merge meta. Use update_wake_meta if only metadata
        needs changing during the wake (without stamping ended_at)."""
        self.update_wake_meta(wake_id, meta_updates=meta_updates)
        self.execute(
            "UPDATE wake SET ended_at = ? WHERE id = ?",
            (ended_at if ended_at is not None else time.time(), wake_id),
        )
        self.commit()

    def update_wake_meta(
        self,
        wake_id: int,
        *,
        meta_updates: dict[str, Any] | None = None,
    ) -> None:
        """Merge meta_json without stamping ended_at — used mid-wake."""
        wake = self.get_wake(wake_id)
        if not wake:
            return
        merged_meta: dict[str, Any] = dict(wake.get("meta_json") or {})
        if isinstance(merged_meta, str):
            try:
                merged_meta = json.loads(merged_meta)
            except Exception:
                merged_meta = {}
        if meta_updates:
            merged_meta.update(meta_updates)
        self.execute(
            "UPDATE wake SET meta_json = ? WHERE id = ?",
            (
                json.dumps(merged_meta, ensure_ascii=False, default=str),
                wake_id,
            ),
        )
        self.commit()

    def get_wake(self, wake_id: int) -> dict[str, Any] | None:
        row = self.fetchone("SELECT * FROM wake WHERE id = ?", (wake_id,))
        if not row:
            return None
        try:
            row["meta_json"] = json.loads(row.get("meta_json") or "{}")
        except Exception:
            row["meta_json"] = {}
        return row

    def list_wakes(
        self,
        *,
        chat_id: str | None = None,
        trigger_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List wakes. ``chat_id`` / ``trigger_type`` filter on meta_json (best-effort).

        Returns at most ``limit`` items starting from the ``offset``-th match
        (after filtering), ordered by ``wake_seq DESC`` (newest first).
        """
        rows = self.fetchall(
            "SELECT * FROM wake WHERE instance_id = ? ORDER BY wake_seq DESC LIMIT ?",
            (self.instance_id, max(limit + offset, 50) * 4),
        )
        out: list[dict[str, Any]] = []
        for r in rows:
            try:
                r["meta_json"] = json.loads(r.get("meta_json") or "{}")
            except Exception:
                r["meta_json"] = {}
            if chat_id and r["meta_json"].get("trigger_chat_id") != chat_id:
                continue
            if trigger_type and r["meta_json"].get("trigger_type") != trigger_type:
                continue
            out.append(r)
        # Apply offset after filtering
        return out[offset : offset + limit]

    def count_wakes(self, *, chat_id: str | None = None) -> int:
        """Total wake count for this instance (optionally filtered by chat_id)."""
        if not chat_id:
            row = self.fetchone(
                "SELECT COUNT(*) as n FROM wake WHERE instance_id = ?",
                (self.instance_id,),
            )
            return row["n"] if row else 0
        # chat_id filter on meta_json — must scan all rows
        rows = self.fetchall(
            "SELECT meta_json FROM wake WHERE instance_id = ?",
            (self.instance_id,),
        )
        n = 0
        for r in rows:
            try:
                meta = json.loads(r.get("meta_json") or "{}")
            except Exception:
                meta = {}
            if meta.get("trigger_chat_id") == chat_id:
                n += 1
        return n

    # ---- turn -------------------------------------------------------------

    def append_turn(
        self,
        *,
        wake_id: int,
        wake_seq: int,
        llm_call_seq: int,
        role: str,
        content: str | None = None,
        position_in_call: int = 0,
        tool_name: str | None = None,
        tool_call_id: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        reasoning: str | None = None,
        finish_reason: str | None = None,
        token_count: int | None = None,
        chat_id: str | None = None,
        error: str | None = None,
        timestamp: float | None = None,
        session_id: str = "",
    ) -> int:
        cur = self.execute(
            """INSERT INTO turn
            (instance_id, wake_id, wake_seq, llm_call_seq, position_in_call, role,
             content, tool_name, tool_call_id, tool_calls, reasoning, finish_reason,
             token_count, chat_id, error, timestamp, session_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                self.instance_id,
                wake_id,
                wake_seq,
                llm_call_seq,
                position_in_call,
                role,
                content,
                tool_name,
                tool_call_id,
                json.dumps(tool_calls, ensure_ascii=False, default=str) if tool_calls else None,
                reasoning,
                finish_reason,
                token_count,
                chat_id or "",
                error,
                timestamp if timestamp is not None else time.time(),
                session_id,
            ),
        )
        self.commit()
        return int(cur.lastrowid)

    def list_turns(
        self,
        wake_id: int,
        *,
        up_to_call_seq: int | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM turn WHERE wake_id = ?"
        params: list[Any] = [wake_id]
        if up_to_call_seq is not None:
            sql += " AND llm_call_seq < ?"
            params.append(up_to_call_seq)
        sql += " ORDER BY llm_call_seq, position_in_call, id"
        rows = self.fetchall(sql, tuple(params))
        for r in rows:
            if r.get("tool_calls"):
                try:
                    r["tool_calls"] = json.loads(r["tool_calls"])
                except Exception:
                    r["tool_calls"] = []
        return rows

    def list_turns_by_chat(self, chat_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.fetchall(
            "SELECT * FROM turn WHERE chat_id = ? ORDER BY timestamp DESC, id DESC LIMIT ?",
            (chat_id, limit),
        )
        for r in rows:
            if r.get("tool_calls"):
                try:
                    r["tool_calls"] = json.loads(r["tool_calls"])
                except Exception:
                    r["tool_calls"] = []
        return list(reversed(rows))

    def last_turn_at(self) -> float | None:
        """本实例最近一次 turn 的 unix timestamp；表为空返回 None。

        作为 wake 存活心跳使用：turn 表每个 LLM call 都会写多行
        (``agent.py`` 调 ``WakeContext.assistant/tool_result`` →
        ``append_turn``)，因此 ``MAX(timestamp)`` 反映模型是否还在出 token。
        相比 ``affairs.updated_at``（wake 运行期间不动，只在状态变更时刷新），
        turn 心跳是细粒度且真实的存活信号。

        用于 cron stale-RUNNING 判定与进程重启 stale 清理。
        """
        row = self.fetchone(
            "SELECT MAX(timestamp) AS m FROM turn WHERE instance_id = ?",
            (self.instance_id,),
        )
        m = (row or {}).get("m")
        return float(m) if m is not None else None

    def last_wake_started_at(self) -> float | None:
        """最近一次 wake 的 ``started_at``；无 wake 返回 None。

        用于 wake 刚起、第一个 turn 尚未落地时的存活兜底
        （此时 ``last_turn_at`` 还为 None）。
        """
        row = self.fetchone(
            "SELECT started_at FROM wake WHERE instance_id = ? "
            "ORDER BY started_at DESC LIMIT 1",
            (self.instance_id,),
        )
        s = (row or {}).get("started_at")
        return float(s) if s is not None else None

    # ---- injection --------------------------------------------------------

    def inject(
        self,
        *,
        wake_id: int,
        wake_seq: int,
        sys_tool: str,
        content: str,
        scope_id: str = "*",
        injected_before_call: int = 0,
        memory_refs: list[int] | dict[str, Any] | None = None,
        session_id: str = "",
    ) -> int:
        if _dedup_strategy(sys_tool) == "latest":
            self.execute(
                """DELETE FROM injection
                   WHERE instance_id = ? AND wake_id = ? AND sys_tool = ? AND scope_id = ?""",
                (self.instance_id, wake_id, sys_tool, scope_id),
            )
        refs_json = (
            json.dumps(memory_refs, ensure_ascii=False, default=str)
            if memory_refs is not None
            else None
        )
        cur = self.execute(
            """INSERT INTO injection
            (instance_id, wake_id, wake_seq, sys_tool, scope_id, memory_refs,
             content, injected_before_call, injected_at, session_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                self.instance_id,
                wake_id,
                wake_seq,
                sys_tool,
                scope_id,
                refs_json,
                content,
                injected_before_call,
                time.time(),
                session_id,
            ),
        )
        self.commit()
        return int(cur.lastrowid)

    def list_injections(
        self,
        wake_id: int,
        *,
        before_call: int | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM injection WHERE wake_id = ?"
        params: list[Any] = [wake_id]
        if before_call is not None:
            sql += " AND injected_before_call = ?"
            params.append(before_call)
        sql += " ORDER BY id"
        rows = self.fetchall(sql, tuple(params))
        for r in rows:
            refs = r.get("memory_refs")
            if refs:
                try:
                    r["memory_refs"] = json.loads(refs)
                except Exception:
                    pass
        return rows

    def list_turns_by_session(self, session_id: str) -> list[dict[str, Any]]:
        """按 session_id 拉所有 turn(跨 wake)。"""
        rows = self.fetchall(
            "SELECT * FROM turn WHERE session_id = ? ORDER BY id",
            (session_id,),
        )
        for r in rows:
            if r.get("tool_calls"):
                try:
                    r["tool_calls"] = json.loads(r["tool_calls"])
                except Exception:
                    r["tool_calls"] = []
        return rows

    def list_injections_by_session(self, session_id: str) -> list[dict[str, Any]]:
        """按 session_id 拉所有 injection(跨 wake)。"""
        rows = self.fetchall(
            "SELECT * FROM injection WHERE session_id = ? ORDER BY injected_at",
            (session_id,),
        )
        for r in rows:
            refs = r.get("memory_refs")
            if refs:
                try:
                    r["memory_refs"] = json.loads(refs)
                except Exception:
                    pass
        return rows

    def list_wakes_by_session(self, session_id: str) -> list[dict[str, Any]]:
        """按 session_id 拉所有 wake。"""
        return self.fetchall(
            "SELECT * FROM wake WHERE session_id = ? ORDER BY started_at",
            (session_id,),
        )

    # ---- input reconstruction --------------------------------------------

    def render_input_for_call(
        self,
        wake_id: int,
        call_seq: int,
        *,
        persona_loader: Callable[[str], str] | None = None,
    ) -> list[dict[str, Any]]:
        """组装第 call_seq 次 LLM 调用实际收到的 messages（字面）。

        实现委托给唯一的组装函数 ``infrastructure.ai.assembly.assemble_llm_input``。
        该函数同时供发消息路径（``agent._chat`` 内部）和回溯路径（本函数）调用——
        组装逻辑只有一份，见 ``docs/architecture/llm-input-assembly.md``。

        Indexing matches ``llm_call_seq`` (0-based)。输出含 think（reasoning）
        已拼回历史 assistant content——这是本路径相对旧 _reconstruct_full 的关键改进。
        """
        # 旧的独立组装逻辑 ``_reconstruct_full`` 已废弃，保留作为字面对照证据。
        # 如需回退，将下行换为 ``self._reconstruct_full(wake, call_seq, persona_loader)``。
        from infrastructure.ai.assembly import assemble_llm_input
        return assemble_llm_input(
            wake_id=wake_id,
            call_seq=call_seq,
            audit=self,
            persona_loader=persona_loader,
        )

    def _reconstruct_full(
        self,
        wake: dict[str, Any],
        call_seq: int,
        persona_loader: Callable[[str], str] | None,
    ) -> list[dict[str, Any]]:
        wake_id = wake["id"]
        meta = wake.get("meta_json") or {}
        out: list[dict[str, Any]] = []
        # If scheduler stored the real system prompt verbatim, prefer that
        # (it includes L4 lifecycle + skill index + project position + persona).
        # Fall back to persona_loader(ref) if not stored.
        stored_sp = meta.get("system_prompt_text")
        if stored_sp and isinstance(stored_sp, str):
            out.append({"role": "system", "content": stored_sp})
        elif persona_loader and meta.get("system_prompt_ref"):
            persona_text = persona_loader(meta["system_prompt_ref"])
            if persona_text:
                out.append({"role": "system", "content": persona_text})

        # Continuation history: when ``is_continuation=True`` in scheduler, the
        # LLM was sent prior session messages before this wake's first
        # injection / action_prompt. Reconstruct them here.
        cont = meta.get("continuation_history")
        if isinstance(cont, list):
            for entry in cont:
                role = entry.get("role")
                if role not in ("user", "assistant", "tool"):
                    continue
                msg: dict[str, Any] = {"role": role, "content": entry.get("content") or ""}
                # Tool rows may come in OpenAI form ({"name": "..."}) or our
                # DB form ({"tool_name": "..."}). Accept both.
                name = entry.get("name") or entry.get("tool_name")
                if name:
                    msg["name"] = name
                if entry.get("tool_call_id"):
                    msg["tool_call_id"] = entry["tool_call_id"]
                if entry.get("tool_calls"):
                    msg["tool_calls"] = entry["tool_calls"]
                out.append(msg)

        all_turns = self.list_turns(wake_id)
        by_call: dict[int, list[dict[str, Any]]] = {}
        for t in all_turns:
            by_call.setdefault(t["llm_call_seq"], []).append(t)

        all_inj = self.list_injections(wake_id)
        by_cmd: dict[int, list[dict[str, Any]]] = {}
        for inj in all_inj:
            by_cmd.setdefault(inj["injected_before_call"], []).append(inj)

        for inj in by_cmd.get(0, []):
            ai, tl = self._injection_as_fake_pair(inj)
            out.append(ai)
            out.append(tl)
        # action_prompt: only the very first user (llm_call_seq == 0) starts the convo.
        for t in by_call.get(0, []):
            if t["role"] == "user":
                out.append({"role": "user", "content": t["content"] or ""})
        # For each subsequent LLM call k = 1..call_seq:
        # 1) emit prior call's assistant + tool turns (the LLM's own prior output)
        # 2) emit injections buffered before call k (e.g. entity_recall, narrative)
        # Note: group(k) injections apply to call k's input, so they appear
        # even when call_seq == k — they're what the model is about to see.
        for k in range(1, call_seq + 1):
            for t in by_call.get(k - 1, []):
                if t["role"] == "user":
                    continue
                msg: dict[str, Any] = {"role": t["role"]}
                if t["content"] is not None:
                    msg["content"] = t["content"]
                if t.get("tool_name"):
                    msg["name"] = t["tool_name"]
                if t.get("tool_calls"):
                    msg["tool_calls"] = t["tool_calls"]
                if t.get("tool_call_id"):
                    msg["tool_call_id"] = t["tool_call_id"]
                out.append(msg)
            for inj in by_cmd.get(k, []):
                ai, tl = self._injection_as_fake_pair(inj)
                out.append(ai)
                out.append(tl)
        return out

    @staticmethod
    def _injection_as_fake_pair(inj: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """One injection → (fake assistant tool_call placeholder, tool result).

        Mirrors ``agent._sys_tool_call`` so the replayed input has the same
        shape the LLM API actually received (OpenAI requires tool results to
        reference a real tool_call_id from a prior assistant message).
        """
        vir_id = f"fake_{inj['sys_tool']}_{inj['id']}"
        assistant_msg = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": vir_id,
                "type": "function",
                "function": {"name": inj["sys_tool"], "arguments": "{}"},
            }],
        }
        tool_msg = {
            "role": "tool",
            "tool_call_id": vir_id,
            "name": inj["sys_tool"],
            "content": inj["content"] or "",
            "_is_fake": True,
            "_scope_id": inj["scope_id"],
        }
        return assistant_msg, tool_msg
