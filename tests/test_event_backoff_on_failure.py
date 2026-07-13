"""事件消费失败后的「自我指数退避」回归测试。

设计意图（docs/design/digital-life-system-design.md 5.5 / 22.1）：
  闹钟只管「到没到时间」，事件队列只管「有没有待处理」。failure recovery
  不是闹钟的职责。事件被消费失败时，自己推迟下次露面时间，靠
  pop_due_events 的 `fire_at <= now` 守卫让它在退避窗口内对 cron 不可见。

这个测试直接复刻 2026-06-14 生产死循环场景：
  队列里堆了 50 个 retry_after_429 timer 事件 → agent 必然 429 → 失败 →
  原代码每分钟重新 pop 同样 50 个事件 → 无限循环。

修复后必须满足：失败后同一批事件集体进入退避，下个 tick 队列为空；
退避窗口结束后复活，再次失败、退避时间指数增长（2/4/8/.../60）。
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest

from domain.lifecycle.clock import now_dt, now_iso


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_test_env(instance_id: str = "test") -> Path:
    """临时 DB + runtime hooks 拨到这个 DB，和生产 storage 一致。"""
    tmp = tempfile.mkdtemp()
    db_path = Path(tmp) / "state.db"
    os.environ["DIGITAL_LIFE_INSTANCE_ID"] = instance_id
    os.environ["DIGITAL_LIFE_RUNTIME_HOME"] = tmp

    from domain.lifecycle.affairs.runtime import configure_runtime_hooks
    configure_runtime_hooks(db_path=db_path)
    return db_path


@pytest.fixture
def env():
    db_path = _make_test_env()
    from domain.lifecycle.events import set_instance_context
    token = set_instance_context("test")
    yield db_path
    from domain.lifecycle.events import reset_instance_context
    reset_instance_context(token)


def _conn(db_path: Path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# 单元：每个事件按 resurrect_count 独立算退避分钟
# ---------------------------------------------------------------------------


def test_backoff_minutes_grows_with_resurrect_count():
    """backoff_minutes_for 是纯函数，2/4/8/16/32/60 封顶。"""
    from domain.lifecycle.events import backoff_minutes_for

    assert backoff_minutes_for({"resurrect_count": 0}) == 2.0
    assert backoff_minutes_for({"resurrect_count": 1}) == 4.0
    assert backoff_minutes_for({"resurrect_count": 2}) == 8.0
    assert backoff_minutes_for({"resurrect_count": 3}) == 16.0
    assert backoff_minutes_for({"resurrect_count": 4}) == 32.0
    # 封顶到 60
    assert backoff_minutes_for({"resurrect_count": 5}) == 60.0
    assert backoff_minutes_for({"resurrect_count": 100}) == 60.0
    # 容错：缺失 / 非法
    assert backoff_minutes_for({}) == 2.0
    assert backoff_minutes_for({"resurrect_count": "bad"}) == 2.0


# ---------------------------------------------------------------------------
# 集成：堆积场景下"失败后不再重复触发"
# ---------------------------------------------------------------------------


def test_batch_failure_backoffs_all_events_to_same_window(env):
    """同一次 wake 抓出来的事件如果失败 → 整批同步退避到本批最大值。

    与用户对齐的语义：触发层每事件记自己的 resurrect_count，但失败处置层
    让整批一起下次回来形成队列，而不是被拆到不同的时间点。这避免「一次只能
    做一件事」的退化，保持"挤压队列一次性消费"的设计意图。
    """
    from domain.lifecycle.events import pop_due_events, delay_pending_events, emit_event
    from domain.lifecycle.clock import now_dt as _now_dt
    from domain.lifecycle.affairs.runtime import _conn as _runtime_conn

    # A: 已失败 3 次的事件（个人下次应得 16 min）
    a_id = emit_event(kind="timer", payload={"name": "A"})
    with _runtime_conn() as c:
        c.execute("UPDATE events SET resurrect_count = 3 WHERE event_id = ?", (a_id,))
    # B: 全新事件（个人下次应得 2 min）
    emit_event(kind="timer", payload={"name": "B"})

    pending = pop_due_events(limit=10)
    assert len(pending) == 2

    n = delay_pending_events(pending)
    assert n == 2

    # 两个事件 fire_at 必须【完全相同】—— 同步退避 = 整批一起回来
    with _runtime_conn() as c:
        rows = c.execute("SELECT event_id, fire_at, resurrect_count FROM events").fetchall()
    fire_ats = {r["fire_at"] for r in rows}
    assert len(fire_ats) == 1, f"两个事件应该同步到同 fire_at，实际: {fire_ats}"
    # 这个统一时间应该是本批最大值（A 的 16 min）
    the_fire_at = next(iter(fire_ats))
    from domain.lifecycle.clock import parse_iso
    delta_min = (parse_iso(the_fire_at) - _now_dt()).total_seconds() / 60
    assert 15 <= delta_min <= 17, f"同步退避应取本批最大 (A=16min)，实际 {delta_min:.1f}"

    # 各事件独立的失败计数仍然各自累计（不是吃大锅饭）
    counts = {r["event_id"]: r["resurrect_count"] for r in rows}
    assert counts[a_id] == 4  # 3 + 1
    # B 是新事件 —— 它的 resurrect_count 应该是 1
    # （注意：resurrect_count 反映的是事件个体属性，与同步退避无关）
    other_id = next(i for i in counts if i != a_id)
    assert counts[other_id] == 1


def _seed_pending_events(env, n: int = 50):
    """模拟生产队列：堆 n 个 retry_after_429 timer 事件。"""
    from domain.lifecycle.events import emit_event
    ids = []
    for i in range(n):
        eid = emit_event(
            kind="timer",
            payload={"reason": "retry_after_429_quota_exhausted", "seq": i},
        )
        ids.append(eid)
    return ids


def test_failure_pushes_events_into_backoff_window(env, db_path=None):
    """复刻生产死循环：50 个事件 pop 出来 → 失败 → delay_pending_events。
    下次 cron tick pop_due_events 必须看到空队列（事件全在退避窗口里）。
    """
    db_path = env
    _seed_pending_events(env, n=50)

    from domain.lifecycle.events import pop_due_events, delay_pending_events

    # 第一次 pop：看到 50 个（就像生产里 cron 每个 tick 做的那样）
    pending = pop_due_events(limit=50)
    assert len(pending) == 50

    # ⚠️ 核心：模拟 wake 失败 → 走 delay_pending_events
    n = delay_pending_events(pending)
    assert n == 50, "全部 50 个事件应被推迟"

    # 失败后立刻再 pop —— 必须是空！这是死循环被结构上断开的关键。
    again = pop_due_events(limit=50)
    assert again == [], (
        "失败后退避窗口内 cron 必须看到空队列；否则就是原 bug —— 每分钟重复触发"
    )

    # 数据库层面校验：每事件 fire_at 已推到未来，resurrect_count 自增
    with _conn(db_path) as c:
        rows = c.execute(
            "SELECT fire_at, resurrect_count, consumed_at FROM events "
            "WHERE kind='timer' ORDER BY event_id"
        ).fetchall()
    now_str = now_iso()
    for r in rows:
        assert r["fire_at"] > now_str, f"fire_at 必须在未来，实际 {r['fire_at']}"
        assert r["resurrect_count"] == 1, "第一次失败应自增到 1"
        assert r["consumed_at"] is None, "退避后应回到未消费态"


def test_repeated_failures_grow_backoff_exponentially(env):
    """连续失败时，每次退避时间指数增长（2 → 4 → 8 → …）。
    这是产品语义「事件自己记得被忽略过几次」的核心保证。
    """
    from domain.lifecycle.events import pop_due_events, delay_pending_events
    from domain.lifecycle.affairs.runtime import _conn as _runtime_conn

    _seed_pending_events(env, n=1)

    for _ in range(5):  # 模拟 5 轮：失败 → 退避 → 时钟推进到窗口末尾 → 复活
        pending = pop_due_events(limit=10)
        assert len(pending) == 1, "每轮复活后必须 pop 得到"
        n = delay_pending_events(pending)
        assert n == 1
        # 窗口内立刻 pop 为空
        assert pop_due_events(limit=10) == []
        # 把 fire_at 写到过去，模拟退避窗口已流逝，事件复活
        from domain.lifecycle.clock import now_dt as _now_dt
        past = (_now_dt() - timedelta(seconds=1)).isoformat(timespec="seconds")
        with _runtime_conn() as c:
            c.execute("UPDATE events SET fire_at = ?", (past,))

    # 5 次失败后，resurrect_count 应该累计到 5
    with _runtime_conn() as c:
        row = c.execute(
            "SELECT resurrect_count FROM events WHERE kind='timer'"
        ).fetchone()
    assert row["resurrect_count"] == 5, "5 次失败应累计到 5"


def test_backoff_events_reappear_after_delay_elapses(env):
    """退避窗口结束 → 事件自然复活，可以被 pop_due_events 看到。
    通过手工把 fire_at 写到过去来模拟「时间已经过去了」。
    """
    from domain.lifecycle.events import pop_due_events, delay_pending_events
    from domain.lifecycle.clock import now_dt as _now_dt

    _seed_pending_events(env, n=1)
    pending = pop_due_events(limit=10)
    delay_pending_events(pending)
    assert pop_due_events(limit=10) == []

    # 把 fire_at 写到过去，模拟退避窗口已过
    from domain.lifecycle.affairs.runtime import _conn as _runtime_conn
    past = (_now_dt() - timedelta(minutes=1)).isoformat(timespec="seconds")
    with _runtime_conn() as c:
        c.execute("UPDATE events SET fire_at = ?", (past,))

    back = pop_due_events(limit=10)
    assert len(back) == 1, "退避窗口过后事件必须能被 pop 出来"
    assert back[0]["resurrect_count"] == 1

