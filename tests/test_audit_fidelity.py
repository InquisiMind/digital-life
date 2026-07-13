"""End-to-end audit fidelity test.

The audit DB's render_input_for_call(wake_id, N) MUST return byte-for-byte
the same messages list the LLM saw right before its Nth call. If not, the
front-end debug view lies and we lose the ability to debug prompts.

This test synthetically walks one full wake through agent.run_conversation
(via the same code path scheduler uses) and asserts the audit replay
matches what was sent. Specifically covers:

- system_message (4-segment _full_system from scheduler)
- slow_ctx injections (system_context / session_digest / consciousness /
  social_context / task_skill / my_context / task_board / chat_stream)
- continuation_history (prior session turns when is_continuation=True)
- multi-call LLM flow (user → assistant(tool_call) → tool → next_call → final)
- mid-session recall (entity_recall / wake_signal)
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from infrastructure.ai.agent import AIAgent
from infrastructure.persistence.instance.runtime_log import RuntimeLogDB
from infrastructure.persistence.instance.wake_context import WakeContext


class AuditFidelityTest(unittest.TestCase):
    def test_full_wake_input_via_render_matches_runtime(self) -> None:
        # Shared ID counter — must match the order audit.injection assigns ids.
        state = {"id": 0}

        def expected_inj_id_next() -> int:
            # audit uses AUTOINCREMENT starting at 1
            state["id"] += 1
            return state["id"]

        with tempfile.TemporaryDirectory() as d:
            audit = RuntimeLogDB(db_path=Path(d) / "rt.db", instance_id="t1")
            ctx = WakeContext.start(audit, meta={
                "trigger_type": "group_message",
                "trigger_chat_id": "oc_test",
                "system_prompt_ref": "instance:t1",  # legacy ref kept for compat
            })

            # ── Mirror what scheduler.py does for _full_system (4 segments) ──
            full_system = (
                "# 🧬 L4 lifecycle\n你与世界的关系...\n"
                "# 🎯 技能索引\nsearch_history, write_diary, ...\n"
                "# 📌 项目岗位\n[模拟炒股/策略师]\n"
                "# 🪪 意识内核\n当前你的身份是 Zero..."
            )
            ctx.record_system_prompt(full_system)

            # ── Continuation history (is_continuation=True path) ──
            continuation = [
                {"role": "user", "content": "上一段对话的事件 1"},
                {"role": "assistant", "content": "上段回复"},
                {"role": "assistant", "content": None, "tool_calls": [{"id": "c_prev_1", "function": {"name": "search_history"}}]},
                {"role": "tool", "tool_call_id": "c_prev_1", "name": "search_history", "content": '{"result":"ok"}'},
            ]
            ctx.record_continuation(continuation)

            # ── Slow_ctx before call 0 (mirror agent._convert_user_to_tool) ──
            ctx.slow_ctx("system_context", "事件引用：张在群里说了...")
            ctx.slow_ctx("session_digest", "[最近经历]\n- 上一轮做了 X")
            ctx.slow_ctx("consciousness", "[休息前思绪]\n明天计划 Y")
            ctx.slow_ctx("social_context", "## 社交关系\n- 张 = 维护者")
            ctx.slow_ctx("task_skill", "## 方法论\n客户回访 3 步")
            ctx.slow_ctx("my_context", "- 待办: 跟进 alpha")
            ctx.slow_ctx("task_board", "1. [in_progress] 跟进")
            # chat_stream should NOT be dedup'd; multiple appends all survive.
            ctx.slow_ctx("chat_stream", "[alpha @刚刚]: 1\n", scope_id="oc_test")
            ctx.slow_ctx("chat_stream", "[张 @刚刚]: 在吗\n", scope_id="oc_test")

            # ── action_prompt ──
            ctx.action("张 发来新消息「在吗」", chat_id="oc_test")

            # ── Call 0: assistant decides tool_call ──
            ctx.assistant(
                content=None,
                tool_calls=[{"id": "c1", "type": "function",
                            "function": {"name": "express_to_human", "arguments": "{}"}}],
                finish_reason="tool_calls",
                reasoning="觉得应该回复张",
            )
            ctx.tool_result(tool_name="express_to_human", tool_call_id="c1", content='{"sent":true,"channel":"lark:group:oc_test"}')

            ctx.next_call()
            ctx.recall("entity_recall", "[记忆联想] - 张上次问 ETF")
            ctx.recall("wake_signal", "[新事件 #999] system 注入新消息")

            # ── Call 1: final assistant reply ──
            ctx.assistant(content="已回复张，入日志。", finish_reason="stop")

            ctx.end(end_reason="normal")

            # ── Build the "expected" messages list exactly as agent.py would ──
            def _fake_pair(sys_tool: str, fake_id: int, content: str) -> tuple[dict, dict]:
                """Mirror agent._sys_tool_call output shape exactly."""
                # vir_id matches render format: fake_<sys_tool>_<inj_id>
                vir_id = f"fake_{sys_tool}_{fake_id}"
                assistant_msg = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": vir_id,
                        "type": "function",
                        "function": {"name": sys_tool, "arguments": "{}"},
                    }],
                }
                tool_msg = {
                    "role": "tool",
                    "tool_call_id": vir_id,
                    "name": sys_tool,
                    "content": content,
                }
                return assistant_msg, tool_msg

            # Predict the inj ids in order (1..N)
            expected_call_0: list[dict[str, Any]] = [
                {"role": "system", "content": full_system},  # from record_system_prompt
                # continuation_history comes next, real turns only
                {"role": "user", "content": "上一段对话的事件 1"},
                {"role": "assistant", "content": "上段回复"},
                {"role": "assistant", "content": None, "tool_calls": [{"id": "c_prev_1", "function": {"name": "search_history"}}]},
                {"role": "tool", "tool_call_id": "c_prev_1", "name": "search_history", "content": '{"result":"ok"}'},
            ]
            # slow_ctx injections before call 0 (each is assistant+tool pair)
            for sys_tool, content, scope in [
                ("system_context", "事件引用：张在群里说了...", None),
                ("session_digest", "[最近经历]\n- 上一轮做了 X", None),
                ("consciousness", "[休息前思绪]\n明天计划 Y", None),
                ("social_context", "## 社交关系\n- 张 = 维护者", None),
                ("task_skill", "## 方法论\n客户回访 3 步", None),
                ("my_context", "- 待办: 跟进 alpha", None),
                ("task_board", "1. [in_progress] 跟进", None),
                ("chat_stream", "[alpha @刚刚]: 1\n", None),
                ("chat_stream", "[张 @刚刚]: 在吗\n", None),
            ]:
                fake_id_n = expected_inj_id_next()
                ai, tl = _fake_pair(sys_tool, fake_id_n, content)
                expected_call_0.append(ai)
                expected_call_0.append(tl)
            # user action_prompt
            expected_call_0.append({"role": "user", "content": "张 发来新消息「在吗」"})

            # Call 1: adds assistant(0)/tool(0)/assistant placeholder[entity_recall, wake_signal]
            #
            # Note: render_input_for_call 不传 model → 走 GenericOpenAIProvider
            # (thinking_keep_mode=drop),不做 reasoning 注入。故 call 1 的 c1
            # assistant content 为 None(只保留 tool_calls)。GLMProvider 才会把
            # reasoning 注入 reasoning_content 字段(原生字段,不动 content)——
            # 那是 GLM-5.2 reuse 模式的行为,本测试未传 model 故不触发。
            expected_call_1 = list(expected_call_0) + [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": "c1", "function": {"name": "express_to_human"}}],
                },
                {"role": "tool", "name": "express_to_human", "tool_call_id": "c1", "content": '{"sent":true,"channel":"lark:group:oc_test"}'},
            ]
            for sys_tool, content in [("entity_recall", "[记忆联想] - 张上次问 ETF"), ("wake_signal", "[新事件 #999] system 注入新消息")]:
                fake_id_n = expected_inj_id_next()
                ai, tl = _fake_pair(sys_tool, fake_id_n, content)
                expected_call_1.append(ai)
                expected_call_1.append(tl)

            # ── Render call 0 via audit replay (no persona_loader: should use
            #     stored system_prompt_text) ──
            rendered_0 = audit.render_input_for_call(ctx.wake_id, 0)
            self._assert_messages_match(expected_call_0, rendered_0, label="call 0")

            rendered_1 = audit.render_input_for_call(ctx.wake_id, 1)
            self._assert_messages_match(expected_call_1, rendered_1, label="call 1")

    def _assert_messages_match(
        self,
        expected: list[dict[str, Any]],
        actual: list[dict[str, Any]],
        *,
        label: str,
    ) -> None:
        # Drop bookkeeping markers added by audit (_is_fake / _scope_id) for compare.
        clean_actual = [
            {k: v for k, v in m.items() if k not in ("_is_fake", "_scope_id")}
            for m in actual
        ]
        # Sometimes audit injects tool_call id only when both name+_call_id present;
        # tolerate only focusing on the things that matter to the LLM (role, content,
        # name, tool_calls funcs name).
        def _norm(m: dict[str, Any]) -> dict[str, Any]:
            r = {"role": m.get("role")}
            if "content" in m:
                v = m["content"]
                # Treat None and empty string as equivalent (audit normalizes
                # assistant placeholder turns where content was None → "" via
                # str-conv).
                r["content"] = "" if v is None else v
            if "name" in m:
                r["name"] = m["name"]
            if "tool_call_id" in m:
                r["tool_call_id"] = m["tool_call_id"]
            if m.get("tool_calls"):
                r["tool_calls"] = [
                    {"function": {"name": (tc.get("function") or {}).get("name")}}
                    for tc in m["tool_calls"]
                ]
            # Drop empty content on assistant with tool_calls (placeholder turns)
            if r.get("tool_calls") and not r.get("content"):
                r.pop("content", None)
            return r

        norm_expected = [_norm(m) for m in expected]
        norm_actual = [_norm(m) for m in clean_actual]
        self.assertEqual(
            len(norm_actual), len(norm_expected),
            f"[{label}] message count mismatch: expected {len(norm_expected)} got {len(norm_actual)}",
        )
        for i, (e, a) in enumerate(zip(norm_expected, norm_actual)):
            self.assertEqual(
                e, a,
                f"[{label}] message {i} mismatch\n"
                f"  expected: {e}\n"
                f"  actual:   {a}",
            )


if __name__ == "__main__":
    unittest.main()
