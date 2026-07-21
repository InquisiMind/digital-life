"""Per-wake audit writer.

``WakeContext`` is the ergonomic surface scheduler/agent use to record one
wake. It tracks ``wake_id`` / ``wake_seq`` / ``llm_call_seq`` so callers
don't have to thread state through every code path — call
``ctx.user_msg("hi")``/``ctx.assistant_msg(...)``/``ctx.tool_result(...)``
without repeating wake-scoped IDs.

Lifecycle::

    ctx = WakeContext.start(audit, meta={"trigger_type": ..., "system_prompt_ref": ...})
    ctx.slow_ctx("session_digest", content="...")        # before_call=0
    ctx.action("hi", chat_id="oc_x")                      # marks user(0); bumps call_seq
    ctx.assistant(content="hi back")                      # turns at current call_seq
    ctx.tool_result(name="send_msg", tool_call_id="c1", content="sent")
    ctx.next_call()                                       # bumps to call_seq=1
    ctx.recall("entity_recall", content="...")            # before_call=1
    ctx.assistant(content="...", tool_calls=[...])
    ctx.end(input_tokens=100, output_tokens=50)

Why ``next_call`` is explicit (rather than automatic)::
    scheduler interleaves multiple LLM calls and assistant-only replies;
    explicit boundaries keep the model audit unambiguous.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from infrastructure.persistence.instance.runtime_log import RuntimeLogDB


@dataclass
class WakeContext:
    audit: RuntimeLogDB
    wake_id: int
    wake_seq: int
    llm_call_seq: int = 0
    _position: int = 0
    _ended: bool = False
    meta: dict[str, Any] = field(default_factory=dict)
    session_id: str = ""

    # ---- factory ----------------------------------------------------------

    @classmethod
    def start(
        cls,
        audit: RuntimeLogDB,
        *,
        meta: dict[str, Any] | None = None,
        session_id: str = "",
    ) -> "WakeContext":
        wake_id = audit.create_wake(meta=meta, session_id=session_id)
        wake_seq = audit.get_wake(wake_id)["wake_seq"]
        return cls(
            audit=audit,
            wake_id=wake_id,
            wake_seq=wake_seq,
            meta=meta or {},
            session_id=session_id,
        )

    # ---- slow_ctx + injections (before call 0, or between calls) ---------

    def slow_ctx(
        self,
        sys_tool: str,
        content: str,
        *,
        scope_id: str = "*",
        memory_refs: list[int] | dict[str, Any] | None = None,
    ) -> int:
        """Inject a slow-context (slow-var) fake tool call at the current
        call boundary — i.e. before this wake's current/next LLM call.

        Use ``recall(...)`` for mid-call (entity_recall, narrative).
        """
        return self.audit.inject(
            wake_id=self.wake_id,
            wake_seq=self.wake_seq,
            sys_tool=sys_tool,
            content=content,
            scope_id=scope_id,
            injected_before_call=self.llm_call_seq,
            memory_refs=memory_refs,
            session_id=self.session_id,
        )

    def recall(
        self,
        sys_tool: str,
        content: str,
        *,
        scope_id: str = "*",
        memory_refs: list[int] | dict[str, Any] | None = None,
    ) -> int:
        """Inject a mid-session recall (entity_recall, narrative_*) before
        the *next* LLM call. Equivalent to ``slow_ctx`` semantically —
        kept separate only to mark intent at the call site.

        For ``entity_recall`` the agent mirrors its in-memory ``_prune_recall_injections``
        on the audit side: each subsequent call replaces the prior one so the
        rendered audit input matches what the LLM actually saw (only the
        latest recall — not a stack of N recall rounds).
        """
        if sys_tool == "entity_recall":
            # Delete prior recall rows in this wake so the audit replay only
            # surfaces the latest set (mirrors agent._prune_recall_injections).
            try:
                self.audit.execute(
                    "DELETE FROM injection "
                    "WHERE wake_id = ? AND sys_tool = 'entity_recall'",
                    (self.wake_id,),
                )
            except Exception:
                pass
        return self.audit.inject(
            wake_id=self.wake_id,
            wake_seq=self.wake_seq,
            sys_tool=sys_tool,
            content=content,
            scope_id=scope_id,
            injected_before_call=self.llm_call_seq,
            memory_refs=memory_refs,
            session_id=self.session_id,
        )

    # ---- turns -----------------------------------------------------------

    def action(self, content: str, *, chat_id: str | None = None) -> int:
        """Record the user's action_prompt (occurs at llm_call_seq's request)."""
        row = self.audit.append_turn(
            wake_id=self.wake_id,
            wake_seq=self.wake_seq,
            llm_call_seq=self.llm_call_seq,
            position_in_call=self._position,
            role="user",
            content=content,
            chat_id=chat_id,
            session_id=self.session_id,
        )
        self._position += 1
        return row

    def record_system_prompt(self, system_text: str) -> None:
        """Persist the actual system prompt sent to the LLM (4-segment _full_system
        from scheduler). Storing it inside ``meta_json`` lets the audit replay
        match the real input the model saw, instead of guessing from a file ref.
        """
        if not system_text:
            return
        try:
            self.audit.update_wake_meta(
                self.wake_id,
                meta_updates={"system_prompt_text": system_text},
            )
        except Exception:
            pass

    def record_continuation(self, prev_history: list[dict]) -> None:
        """Persist the continuation history (real turns from prior session).

        When ``is_continuation=True`` in scheduler, the LLM receives
        ``conversation_history`` = prev turns from the previous session. Without
        this record, render_input_for_call() can't replay the actual input.
        Each turn is capped at 500 chars to prevent one huge meta_json row.
        """
        if not prev_history:
            return
        try:
            import json as _json
            light = []
            for m in prev_history[:200]:  # cap to 200 turns
                role = m.get("role")
                if role not in ("user", "assistant", "tool"):
                    continue
                entry = {"role": role}
                c = m.get("content") or ""
                if isinstance(c, str) and len(c) > 500:
                    c = c[:500] + "...[truncated]"
                entry["content"] = c
                if m.get("tool_name"):
                    entry["tool_name"] = m["tool_name"]
                if m.get("name"):
                    entry["name"] = m["name"]
                if m.get("tool_call_id"):
                    entry["tool_call_id"] = m["tool_call_id"]
                if m.get("tool_calls"):
                    entry["tool_calls"] = m["tool_calls"]
                light.append(entry)
            self.audit.update_wake_meta(
                self.wake_id,
                meta_updates={"continuation_history": light},
            )
        except Exception:
            pass

    def assistant(
        self,
        *,
        content: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        reasoning: str | None = None,
        finish_reason: str | None = None,
        token_count: int | None = None,
    ) -> int:
        row = self.audit.append_turn(
            wake_id=self.wake_id,
            wake_seq=self.wake_seq,
            llm_call_seq=self.llm_call_seq,
            position_in_call=self._position,
            role="assistant",
            content=content,
            tool_calls=tool_calls,
            reasoning=reasoning,
            finish_reason=finish_reason,
            token_count=token_count,
            session_id=self.session_id,
        )
        self._position += 1
        return row

    def tool_result(
        self,
        *,
        tool_name: str,
        tool_call_id: str,
        content: str,
        error: str | None = None,
    ) -> int:
        row = self.audit.append_turn(
            wake_id=self.wake_id,
            wake_seq=self.wake_seq,
            llm_call_seq=self.llm_call_seq,
            position_in_call=self._position,
            role="tool",
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            content=content,
            error=error,
            session_id=self.session_id,
        )
        self._position += 1
        return row

    def next_call(self) -> int:
        """Advance to the next LLM call. After calling this, ``slow_ctx``
        / ``recall`` will be recorded with ``injected_before_call`` of the
        new ``llm_call_seq``, and assistant/tool turns will be tagged
        with the new sequence too."""
        self.llm_call_seq += 1
        self._position = 0
        return self.llm_call_seq

    # ---- end -------------------------------------------------------------

    def end(
        self,
        *,
        end_reason: str = "normal",
        input_tokens: int = 0,
        output_tokens: int = 0,
        new_memory_digest_id: int | None = None,
        extra_meta: dict[str, Any] | None = None,
    ) -> None:
        if self._ended:
            return
        self._ended = True
        meta_updates: dict[str, Any] = {
            "end_reason": end_reason,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "llm_call_count": self.llm_call_seq + 1,
        }
        if new_memory_digest_id is not None:
            meta_updates["new_memory_digest_id"] = new_memory_digest_id
        if extra_meta:
            meta_updates.update(extra_meta)
        self.audit.end_wake(self.wake_id, meta_updates=meta_updates)

    # ---- read helpers (mirror of audit) ----------------------------------

    def render_input_for_call(self, call_seq: int, *, persona_loader=None) -> list[dict[str, Any]]:
        return self.audit.render_input_for_call(
            self.wake_id, call_seq, persona_loader=persona_loader
        )

    def list_turns(self) -> list[dict[str, Any]]:
        return self.audit.list_turns(self.wake_id)

    def list_injections(self, *, before_call: int | None = None) -> list[dict[str, Any]]:
        return self.audit.list_injections(self.wake_id, before_call=before_call)

    def to_log_dict(self) -> dict[str, Any]:
        """Snapshot useful for log lines and console output."""
        return {
            "wake_id": self.wake_id,
            "wake_seq": self.wake_seq,
            "llm_call_seq": self.llm_call_seq,
            "ended": self._ended,
            "meta": self.meta,
        }


def safe_dump_meta(meta: dict[str, Any]) -> str:
    """Stable JSON dump for ``meta`` dicts in logs."""
    return json.dumps(meta, ensure_ascii=False, default=str, sort_keys=True)


__all__ = ["WakeContext", "safe_dump_meta"]
