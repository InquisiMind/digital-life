"""Regression tests for mid-session inject behavior (2026-07-07 修复).

历史背景：`_inject_to_running_session` 曾为同一条 RUNNING 期收到的 message
做 4 件事——拼 wake prompt 模板、写到 sessions.db 当作 user message、镜像
到 runtime_log.turn、立即把 DB events 标记为已消费。这导致同一消息在同一
wake 内出现两次（一次 wake prompt、一次 wake_signal tool result），且 rest
边界下消息会被静默丢失（被 4 步提前消费掉了）。

修复后的设计原则（统一所有 kind）：

    RUNNING 期间新事件到达 → **只**写内存 ``signalled_events`` 池。
    - 不写 sessions.db
    - 不渲染 wake prompt
    - 不立即消费 DB events 队列
    - 不按 kind 分流处理

后续渲染和消费全部归 ``agent._inject_signalled_events`` 一份代码处理（路径 2）。
本测试锁定上述语义，防止回归。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest


def _make_test_env(instance_id: str = "test-inject") -> tuple[str, Path]:
    """Create a tmp runtime home + state.db + affairs + events tables."""
    tmp = tempfile.mkdtemp()
    db_path = Path(tmp) / "state.db"
    os.environ["DIGITAL_LIFE_INSTANCE_ID"] = instance_id
    os.environ["DIGITAL_LIFE_RUNTIME_HOME"] = tmp

    from domain.lifecycle.affairs.runtime import configure_runtime_hooks, init_db
    configure_runtime_hooks(db_path=db_path)
    init_db()

    import sqlite3 as _sqlite3
    _conn = _sqlite3.connect(str(db_path))
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.close()

    return tmp, db_path


def _cleanup_test_env(tmp: str) -> None:
    shutil.rmtree(tmp, ignore_errors=True)
    os.environ.pop("DIGITAL_LIFE_INSTANCE_ID", None)
    os.environ.pop("DIGITAL_LIFE_RUNTIME_HOME", None)


def _count_injected_user_messages(db_path: Path) -> int:
    """数 sessions/messages 表里 mid-session 注入产生的 user message 数。

    要避免错误命中真实 wake prompt 留下的 user message——区别是 mid-session
    注入的文本以 "## ── ↓ 当下事件 ↓ ──" 模板开头。本测试就是断言这种模板
    在 sessions.db 里**完全不应该出现**。
    """
    # 测试环境没建 sessions.db（_make_test_env 只建 state.db），所以这里
    # 读 state.db.messages（handler 也可能写到那里）。如果文件无 messages
    # 表 / 字段就对返回 0。
    import sqlite3

    if not db_path.exists():
        return 0
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            if "messages" not in tables:
                return 0
            row = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE text LIKE '## ── ↓ 当下事件 ↓ ──%'"
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
    except sqlite3.Error:
        return 0


def _events_consumed(db_path: Path) -> int:
    """state.db.events 表当前 consumed_at IS NOT NULL 的事件数。"""
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM events WHERE consumed_at IS NOT NULL"
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def _emit_group_message_event(payload_text: str = "mid-session 测试消息") -> int:
    """emit 一条 group_message 事件，模拟 RUNNING 期间外部触发。"""
    from domain.lifecycle.events import emit_event

    eid = emit_event(
        "group_message",
        payload={
            "text": payload_text,
            "sender_name": "tester",
            "chat_name": "test-chat",
            "chat_id": "oc_test",
            "mentions_bot": False,
        },
    )
    assert eid, "emit_event 必须返回有效 event_id"
    return eid


def test_mid_session_inject_does_not_write_wake_prompt_to_db() -> None:
    """消息事件 mid-session 注入**不应**写到 state.db 当作 wake-prompt user message。"""
    tmp, db_path = _make_test_env("test-no-wake-prompt")
    try:
        from domain.lifecycle.events import (
            _inject_to_running_session,
            set_instance_context,
            reset_instance_context,
        )

        token = set_instance_context("test-no-wake-prompt")
        try:
            # emit 一条 group_message 后直接调 _inject_to_running_session
            eid = _emit_group_message_event()
            _inject_to_running_session(eid, "test-no-wake-prompt")

            # 关键断言：sessions.db 不要残留 wake-prompt 模板的 user message
            # （历史 bug 是同一消息在 wake 内出现两次的根因）
            assert _count_injected_user_messages(db_path) == 0, (
                "mid-session 注入不应再写 wake-prompt 模板到 sessions.db "
                "（这是历史 bug '同消息渲染两次' 的根因）"
            )
        finally:
            reset_instance_context(token)
    finally:
        _cleanup_test_env(tmp)


def test_mid_session_inject_does_not_early_consume_event() -> None:
    """消息事件 mid-session 注入**不应**立即消费 DB events 队列。

    避免边界：如果模型在扫内存池前 rest 了，事件还在 DB 队列里，cron 下一轮
    pop_due_events 取出走正常 wake 流程——不丢消息。
    """
    tmp, db_path = _make_test_env("test-no-early-consume")
    try:
        from domain.lifecycle.events import (
            _inject_to_running_session,
            set_instance_context,
            reset_instance_context,
        )

        token = set_instance_context("test-no-early-consume")
        try:
            eid = _emit_group_message_event()
            _inject_to_running_session(eid, "test-no-early-consume")

            # 关键断言：inject 后 events 表里这条事件**不应**被消费
            # （消费责任归 agent._inject_signalled_events 路径）
            import sqlite3
            conn = sqlite3.connect(str(db_path))
            try:
                row = conn.execute(
                    "SELECT consumed_at FROM events WHERE event_id = ?", (eid,)
                ).fetchone()
            finally:
                conn.close()
            assert row is not None, "event 必须存在于 DB"
            assert row[0] is None, (
                f"event {eid} 在 inject 后不应被消费（应为 None），实际 consumed_at={row[0]!r}"
            )
        finally:
            reset_instance_context(token)
    finally:
        _cleanup_test_env(tmp)


def test_mid_session_inject_signals_to_memory_pool() -> None:
    """消息事件 mid-session 注入后必须调用 signal_new_events 写入内存池。

    注：用 mock 拦截 signal_new_events 调用 — 真实生产环境里它写到 instance-scoped
    内存 dict；但本测试目标是确认"signal 这一步被正确调用"，而不是测副作用细节。
    测试 setup 使用的 tmp path 与 _peek_single_event 解析路径不同（前者通过
    configure_runtime_hooks、后者通过 ContextVar→get_runtime_state_db_path），
    所以用 mock 拦截最直接。
    """
    from unittest.mock import patch

    tmp, db_path = _make_test_env("test-signals-to-pool")
    try:
        from domain.lifecycle.events import (
            _inject_to_running_session,
            emit_event,
            set_instance_context,
            reset_instance_context,
        )
        from domain.lifecycle import session_events

        token = set_instance_context("test-signals-to-pool")
        try:
            eid = emit_event("group_message", payload={"text": "测试"})
            # 让 _peek_single_event 找到事件：临时把 _peek_single_event 也 mock
            # （绕开 ContextVar 路径解析问题，仅验证"signal 被调用"的核心行为）
            fake_event = {
                "event_id": eid,
                "kind": "group_message",
                "payload": {"text": "测试"},
            }
            with (
                patch(
                    "domain.lifecycle.events._peek_single_event",
                    return_value=fake_event,
                ),
                patch.object(
                    session_events, "signal_new_events", wraps=session_events.signal_new_events
                ) as m_signal,
            ):
                _inject_to_running_session(eid, "test-signals-to-pool")

            assert m_signal.called, "mid-session 注入必须调用 signal_new_events"
            call_args = m_signal.call_args
            assert call_args is not None
            events_arg = call_args.args[0] if call_args.args else call_args.kwargs.get("events")
            assert events_arg and len(events_arg) == 1, (
                f"signal_new_events 应只调用一次且传入一条事件；实际 args={call_args}"
            )
            assert events_arg[0].get("event_id") == eid
        finally:
            reset_instance_context(token)
    finally:
        _cleanup_test_env(tmp)


@pytest.mark.parametrize("kind", ["group_message", "routine", "initiative"])
def test_mid_session_inject_unified_for_all_kinds(kind: str) -> None:
    """**所有** kind（不仅 message）走同一条路：只 signal，不早消费。

    防回归：曾经有个版本对非 message kind 走 _consume_event_safe 立刻消费，
    违反"统一原则"。本参数化测试确保任何 kind 都不被立刻消费。
    """
    tmp, db_path = _make_test_env(f"test-unified-{kind}")
    try:
        from domain.lifecycle.events import (
            _inject_to_running_session,
            emit_event,
            set_instance_context,
            reset_instance_context,
        )

        token = set_instance_context(f"test-unified-{kind}")
        try:
            eid = emit_event(kind, payload={"text": "烟雾测试"})
            _inject_to_running_session(eid, f"test-unified-{kind}")

            import sqlite3
            conn = sqlite3.connect(str(db_path))
            try:
                row = conn.execute(
                    "SELECT consumed_at FROM events WHERE event_id = ?", (eid,)
                ).fetchone()
            finally:
                conn.close()
            assert row is not None
            assert row[0] is None, (
                f"kind={kind} 不应在 inject 后被立刻消费（统一原则：所有 kind 都不该）"
            )
        finally:
            reset_instance_context(token)
    finally:
        _cleanup_test_env(tmp)


def test_mid_session_inject_skips_already_consumed_event() -> None:
    """race 防御：如果事件已被消费（如 wake 启动时 covered_event_ids），mid-session 不该再注入。

    历史 race 场景：
      1. emit event X → affair 是 RUNNING → 走 mid-session
      2. 但同时 wake 启动 covered_event_ids 也消费了 X（极端 race）
      3. _peek_single_event 取到已 consumed 的 X → signal 到内存池
      4. 模型下一轮 _consume_human_events 仍渲染 [消息 #X] → **幽灵消息**

    防御：_peek_single_event 加 ``consumed_at IS NULL`` 过滤。
    """
    tmp, db_path = _make_test_env("test-skip-consumed")
    try:
        from domain.lifecycle.events import (
            _inject_to_running_session,
            emit_event,
            consume_event,
            set_instance_context,
            reset_instance_context,
        )
        from domain.lifecycle.session_events import peek_signalled_events

        token = set_instance_context("test-skip-consumed")
        try:
            eid = emit_event("group_message", payload={"text": "race 测试"})

            # 先手动消费这条事件（模拟 wake 启动 covered_event_ids 的 race）
            consume_event(eid)

            # mid-session 注入时应该 sees it 已消费、信号池保持空
            _inject_to_running_session(eid, "test-skip-consumed")

            events = peek_signalled_events(instance_id="test-skip-consumed")
            assert not any(e.get("event_id") == eid for e in events), (
                f"已 consumed 的事件 {eid} 不应被 signal 到内存池（race 防御）"
            )
        finally:
            reset_instance_context(token)
    finally:
        _cleanup_test_env(tmp)
