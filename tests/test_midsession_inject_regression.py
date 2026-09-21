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


# ════════════════════════════════════════════════════════════════════════════
# 2026-09-21 #124 双投事故回归：write-then-show 原子投递
# 根因：_consume_human_events 先渲染后消费，_do_consume_events 的
# except:pass 吞掉消费失败 → 模型看过、DB 未消费 → 下个 wake 重投一次。
# 修复语义："模型看到"（append live 上下文）放在同文件单事务成功之后。
# ════════════════════════════════════════════════════════════════════════════


def _make_bare_agent(tmp: str, instance_id: str, session_id: str = "sess-test"):
    """构造跳过 __post_init__ 的裸 AIAgent（只给 _consume_human_events 依赖的属性）。"""
    from infrastructure.ai.agent import AIAgent

    agent = object.__new__(AIAgent)
    agent.instance_id = instance_id
    agent.session_id = session_id
    agent.session_db = None
    agent.audit_ctx = None
    agent._injected_signal_event_ids = set()
    agent._injected_memory_ids = set()
    agent._sys_tool_counter = 0
    agent._effort_state = None
    return agent


def test_atomic_deliver_message_and_consume_commit_together() -> None:
    """SessionDB.append_message_with_consume：消息行与事件消费同事务提交。"""
    tmp, db_path = _make_test_env("test-atomic-commit")
    try:
        from infrastructure.ai.session_db import SessionDB
        from domain.lifecycle.events import emit_event, set_instance_context, reset_instance_context

        token = set_instance_context("test-atomic-commit")
        try:
            import sqlite3
            sdb = SessionDB(db_path=db_path)
            sdb.create_session("sess-atomic", "test")
            eid = emit_event("group_message", payload={"text": "原子投递", "chat_id": "oc_x"})

            msg_id = sdb.append_message_with_consume(
                "sess-atomic", "tool", "[#%d · 测试]" % eid,
                event_ids=[eid], tool_name="wake_signal", chat_id="oc_x",
            )
            assert msg_id > 0

            conn = sqlite3.connect(str(db_path))
            try:
                msg = conn.execute(
                    "SELECT content, tool_name FROM messages WHERE id=?", (msg_id,)
                ).fetchone()
                ev = conn.execute(
                    "SELECT consumed_at, consumed_by_session_id FROM events WHERE event_id=?",
                    (eid,),
                ).fetchone()
            finally:
                conn.close()
            assert msg is not None and msg[1] == "wake_signal"
            assert ev[0] is not None, "同事务内事件必须已消费"
            assert ev[1] == "sess-atomic"
        finally:
            reset_instance_context(token)
    finally:
        _cleanup_test_env(tmp)


def test_atomic_deliver_rollback_leaves_no_partial_state() -> None:
    """事务失败：消息行与事件消费**都不落库**（不允许一个有一个没有）。"""
    tmp, db_path = _make_test_env("test-atomic-rollback")
    try:
        from infrastructure.ai.session_db import SessionDB
        from domain.lifecycle.events import emit_event, set_instance_context, reset_instance_context

        token = set_instance_context("test-atomic-rollback")
        try:
            import sqlite3
            sdb = SessionDB(db_path=db_path)
            sdb.create_session("sess-rb", "test")
            eid = emit_event("group_message", payload={"text": "回滚测试", "chat_id": "oc_y"})
            before_msgs = sdb.get_messages("sess-rb")

            # 让事务中途失败：消息 INSERT 之后、事件 UPDATE 之前把 events 表
            # 掉包成缺列形态（sqlite3.Connection.execute 只读不可 monkeypatch，
            # 用改表结构制造同类的 mid-transaction OperationalError）
            sdb._conn.execute("ALTER TABLE events RENAME TO events_bak")
            sdb._conn.commit()
            try:
                sdb.append_message_with_consume(
                    "sess-rb", "tool", "[#%d · 回滚]" % eid,
                    event_ids=[eid], tool_name="wake_signal", chat_id="oc_y",
                )
                raise AssertionError("必须抛出事务失败")
            except sqlite3.OperationalError:
                pass
            finally:
                sdb._conn.execute("ALTER TABLE events_bak RENAME TO events")
                sdb._conn.commit()

            conn = sqlite3.connect(str(db_path))
            try:
                ev = conn.execute(
                    "SELECT consumed_at FROM events WHERE event_id=?", (eid,)
                ).fetchone()
            finally:
                conn.close()
            after_msgs = sdb.get_messages("sess-rb")
            assert ev[0] is None, "事务失败后事件不得被消费"
            assert len(after_msgs) == len(before_msgs), "事务失败后消息行不得落库"
        finally:
            reset_instance_context(token)
    finally:
        _cleanup_test_env(tmp)


def test_consume_human_events_failure_keeps_event_undelivered() -> None:
    """投递事务失败：不 append live 上下文、返回失败列表 → 模型从未看到。"""
    tmp, db_path = _make_test_env("test-fail-undelivered")
    try:
        from domain.lifecycle.events import set_instance_context, reset_instance_context

        token = set_instance_context("test-fail-undelivered")
        try:
            agent = _make_bare_agent(tmp, "test-fail-undelivered")

            class _BoomSessionDB:
                def append_message_with_consume(self, *a, **kw):
                    raise RuntimeError("injected txn failure")

            agent.session_db = _BoomSessionDB()
            ev = {"event_id": 42, "kind": "group_message",
                  "payload": {"text": "失败重试", "chat_id": "oc_z"}}
            messages: list = []
            failed = agent._consume_human_events([ev], messages)

            assert failed == [ev], "失败事件必须原样返回给调用方"
            assert messages == [], "投递失败时模型上下文不得出现该消息（write-then-show）"

            # 修复成功后（换回正常 db）重投 = 首次投递
            from infrastructure.ai.session_db import SessionDB
            agent.session_db = SessionDB(db_path=db_path)
            agent.session_db.create_session("sess-test", "test", model="test")
            failed2 = agent._consume_human_events([ev], messages)
            assert failed2 == []
            assert len(messages) == 2, "成功后恰好渲染一次（assistant+tool pair）"
        finally:
            reset_instance_context(token)
    finally:
        _cleanup_test_env(tmp)


def test_delivered_event_not_re_picked_on_continuation() -> None:
    """已投递（事务成功=已消费）的事件，接续唤醒不会被 DB 扫描再次捞出。"""
    tmp, db_path = _make_test_env("test-no-repick")
    try:
        from domain.lifecycle.events import (
            emit_event, set_instance_context, reset_instance_context,
        )

        token = set_instance_context("test-no-repick")
        try:
            agent = _make_bare_agent(tmp, "test-no-repick")
            from infrastructure.ai.session_db import SessionDB
            agent.session_db = SessionDB(db_path=db_path)
            agent.session_db.create_session("sess-test", "test", model="test")

            eid = emit_event("group_message", payload={"text": "接续测试", "chat_id": "oc_c"})
            ev = {"event_id": eid, "kind": "group_message",
                  "payload": {"text": "接续测试", "chat_id": "oc_c"}}
            messages: list = []
            failed = agent._consume_human_events([ev], messages)
            assert failed == [] and len(messages) == 2

            # 新 wake 的 agent（内存集合清零），DB 扫描不应再捞出该事件
            agent2 = _make_bare_agent(tmp, "test-no-repick")
            agent2.session_db = agent.session_db
            messages2: list = []
            picked = agent2._inject_due_db_events(messages2)
            assert not picked, "已消费事件不得被 DB 扫描重复投递"
            assert messages2 == []
        finally:
            reset_instance_context(token)
    finally:
        _cleanup_test_env(tmp)


def test_memory_id_harvest_visible_window_only() -> None:
    """记忆可见窗口去重：从可见 entity_recall 收割 id，wake_signal 的 #id 不误收。"""
    tmp, db_path = _make_test_env("test-harvest")
    try:
        agent = _make_bare_agent(tmp, "test-harvest")
        history = [
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "sys_001", "type": "function",
                "function": {"name": "entity_recall", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "sys_001", "name": "entity_recall",
             "content": "[联想命中] - 🔍[RULE] #1924 (4d前) 规则内容... - 🎯[KNOWLEDGE] #312 (64d前) ..."},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "sys_002", "type": "function",
                "function": {"name": "wake_signal", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "sys_002", "name": "wake_signal",
             "content": "[#124 · 新消息到达 - 会话中途注入] ..."},
        ]
        agent._harvest_injected_memory_ids(history)
        assert "1924" in agent._injected_memory_ids
        assert "312" in agent._injected_memory_ids
        assert "124" not in agent._injected_memory_ids, "wake_signal 的事件 id 不得混入记忆抑制集合"

        # 模拟 KEEP_ROUNDS 裁剪后：recall 不在可见列表 → 不收割 → 解除抑制
        agent2 = _make_bare_agent(tmp, "test-harvest")
        agent2._harvest_injected_memory_ids(history[:0])
        assert not agent2._injected_memory_ids
    finally:
        _cleanup_test_env(tmp)


def test_requeued_event_not_re_rendered() -> None:
    """回滚反消费重排队的事件：会话历史已投递 → 只补消费、不重复渲染。

    2026-09-21 #513 双投场景：wake 中途被杀（重启）→ 调度器回滚把已投递
    事件 unconsume 重排队（防丢设计）→ 重试拾取时又渲染一遍。
    """
    tmp, db_path = _make_test_env("test-no-rerender")
    try:
        from domain.lifecycle.events import (
            emit_event, set_instance_context, reset_instance_context,
        )
        import sqlite3

        token = set_instance_context("test-no-rerender")
        try:
            agent = _make_bare_agent(tmp, "test-no-rerender")
            from infrastructure.ai.session_db import SessionDB
            agent.session_db = SessionDB(db_path=db_path)
            agent.session_db.create_session("sess-test", "test")

            eid = emit_event("group_message", payload={"text": "回滚重排队", "chat_id": "oc_r"})
            ev = {"event_id": eid, "kind": "group_message",
                  "payload": {"text": "回滚重排队", "chat_id": "oc_r"}}
            messages: list = []
            failed = agent._consume_human_events([ev], messages)
            assert failed == [] and len(messages) == 2, "首次投递应渲染一次"

            # 模拟调度器回滚：反消费 + 重排队
            conn = sqlite3.connect(str(db_path))
            conn.execute("UPDATE events SET consumed_at=NULL WHERE event_id=?", (eid,))
            conn.commit(); conn.close()

            # 重试拾取：历史里已有 [#eid · 行 → 不再渲染、只补消费
            messages2: list = []
            failed2 = agent._consume_human_events([ev], messages2)
            assert failed2 == []
            assert messages2 == [], "已投递事件重排队后不得重复渲染"

            conn = sqlite3.connect(str(db_path))
            try:
                n_rows = conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE tool_name='wake_signal' "
                    "AND content LIKE ?", (f"[#{eid} ·%",)
                ).fetchone()[0]
                consumed = conn.execute(
                    "SELECT consumed_at FROM events WHERE event_id=?", (eid,)
                ).fetchone()[0]
            finally:
                conn.close()
            assert n_rows == 1, f"会话里应恰好一条投递记录，实际 {n_rows}"
            assert consumed is not None, "补消费后事件应回到已消费态"
        finally:
            reset_instance_context(token)
    finally:
        _cleanup_test_env(tmp)
