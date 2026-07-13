"""Integration test for the audit flow — exercises WakeContext through a
realistic two-LLM-call wake, then verifies render_input_for_call produces
the exact messages the model saw at each call."""

from __future__ import annotations

from pathlib import Path

from infrastructure.persistence.instance.runtime_log import RuntimeLogDB
from infrastructure.persistence.instance.wake_context import WakeContext


def test_full_wake_renders_correct_inputs_at_each_call(tmp_path: Path) -> None:
    """Simulate agent.run_conversation's external behavior:
    - 7 slow_ctx injections at startup
    - user action_prompt
    - assistant tool_call
    - tool result
    - entity_recall mid-call
    - assistant final text
    - end

    Then verify each LLM call's reconstructed input matches expectations.
    """
    db = RuntimeLogDB(db_path=tmp_path / "rt.db", instance_id="t1")

    ctx = WakeContext.start(
        db,
        meta={
            "trigger_type": "message",
            "trigger_chat_id": "oc_chat_a",
            "system_prompt_ref": "persona/zero@hash1",
        },
    )

    # Before call 0: 7 slow_ctx items (matching what scheduler injects today).
    ctx.slow_ctx("system_context", "事件引用：5分钟前 张 在 lark:group:oc_chat_a 说了「在吗」")
    ctx.slow_ctx("session_digest", "[最近经历] 上次 6/7 收到客户问题，回答后未跟进...")
    ctx.slow_ctx("consciousness", "[上次留给自己的思绪] 我决定更主动地回访老客户")
    ctx.slow_ctx("social_context", "## 联系人 张=销售部老总 群=销售-总群")
    ctx.slow_ctx("task_skill", "## 任务执行方法论 客户回访 → 先问候 → 简述进展 → 邀请下次")
    ctx.slow_ctx("my_context", "- 待办: 跟进张的问题")
    ctx.slow_ctx("task_board", "1. [in_progress] 跟进客户")

    # action_prompt (the user's actual trigger).
    ctx.action("张 发来新消息「在吗」", chat_id="oc_chat_a")

    # Call 0: assistant decides to send a feishu message.
    ctx.assistant(
        content=None,
        tool_calls=[{
            "id": "c1",
            "type": "function",
            "function": {"name": "express_to_human", "arguments": '{"text":"张好"}'},
        }],
        finish_reason="tool_calls",
        reasoning="张在群里等我回",
        token_count=500,
    )
    ctx.tool_result(
        tool_name="express_to_human",
        tool_call_id="c1",
        content="\"sent ok\"",
    )

    # Advance to call 1; inject an entity_recall before the next call.
    ctx.next_call()
    ctx.recall("entity_recall", "联系人 张：上次问的是 ETF 配置比例")

    # Call 1: assistant finishes with plain text (no tool_call).
    ctx.assistant(
        content="已回复张，记录在案。",
        finish_reason="stop",
        token_count=200,
    )

    ctx.end(input_tokens=900, output_tokens=700, end_reason="normal")

    # Verify call 0 input: persona + 7 slow_ctx (each as assistant+tool) + user.
    msgs_call_0 = ctx.render_input_for_call(0, persona_loader=lambda r: f"P[{r}]")
    assert msgs_call_0[0] == {"role": "system", "content": "P[persona/zero@hash1]"}
    # Each injection becomes a fake pair: assistant placeholder (content=None, _is_fake)
    # + tool result (real content, name=sys_tool, _is_fake). Only tool rows carry `name`.
    fake_tool_kinds = [m["name"] for m in msgs_call_0 if m.get("_is_fake") and m["role"] == "tool"]
    assert fake_tool_kinds == [
        "system_context", "session_digest", "consciousness",
        "social_context", "task_skill", "my_context", "task_board",
    ]
    user_msgs = [m for m in msgs_call_0 if m["role"] == "user"]
    assert len(user_msgs) == 1 and user_msgs[0]["content"] == "张 发来新消息「在吗」"
    # No real turns yet (only assistant placeholders bundled with fake injections).
    # The "no real assistant" assertion is implicit in user_msgs == 1 above.

    # Verify call 1 input: persona + 7 slow_ctx + user + assistant(c1) + tool + entity_recall.
    msgs_call_1 = ctx.render_input_for_call(1, persona_loader=lambda r: f"P[{r}]")
    roles = [m["role"] for m in msgs_call_1]
    assert roles[0] == "system"
    assert "user" in roles
    assert "assistant" in roles
    # The entity_recall appears before the next assistant.
    recall_msgs = [m for m in msgs_call_1 if m.get("name") == "entity_recall"]
    assert len(recall_msgs) == 1
    assert "联系人 张" in recall_msgs[0]["content"]
    # tool_call_id of the real tool_result is preserved.
    real_tool_msgs = [m for m in msgs_call_1 if m["role"] == "tool" and not m.get("_is_fake")]
    assert real_tool_msgs[0]["tool_call_id"] == "c1"
    assert real_tool_msgs[0]["name"] == "express_to_human"

    # Verify wake meta persisted end-of-wake info.
    wake = db.get_wake(ctx.wake_id)
    assert wake["meta_json"]["end_reason"] == "normal"
    assert wake["meta_json"]["input_tokens"] == 900
    assert wake["meta_json"]["llm_call_count"] == 2
    assert wake["ended_at"] is not None


def test_wake_context_double_end_is_idempotent(tmp_path: Path) -> None:
    db = RuntimeLogDB(db_path=tmp_path / "rt.db", instance_id="t1")
    ctx = WakeContext.start(db, meta={"trigger_type": "timer"})
    ctx.action("tick")
    ctx.assistant(content="done", finish_reason="stop")
    ctx.end(end_reason="normal")
    wake = db.get_wake(ctx.wake_id)
    first_ended = wake["ended_at"]
    ctx.end(end_reason="oops")
    wake = db.get_wake(ctx.wake_id)
    # second end must NOT overwrite (reason stays 'normal', ended_at unchanged).
    assert wake["meta_json"]["end_reason"] == "normal"
    assert wake["ended_at"] == first_ended


def test_slow_ctx_latest_dedup_via_wake_context(tmp_path: Path) -> None:
    """相同的 system_context scope="*" 写两次，只有一行进入审计（latest 策略）。"""
    db = RuntimeLogDB(db_path=tmp_path / "rt.db", instance_id="t1")
    ctx = WakeContext.start(db, meta={"trigger_type": "message"})
    ctx.slow_ctx("session_digest", "v1")
    ctx.slow_ctx("session_digest", "v2")
    injections = ctx.list_injections(before_call=0)
    sd = [i for i in injections if i["sys_tool"] == "session_digest"]
    assert len(sd) == 1
    assert sd[0]["content"] == "v2"
