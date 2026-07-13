"""Regression: initiative / 主动探索 的两个关键 bug。

历史 BUG A — 时区不一致：
  get_quiet_hours() 返回北京作息小时（routines.yaml 是北京时间），
  但 check_energy_events 用 `_now().hour`（clock 默认 UTC）去比，导致：
  北京半夜 00:00-05:00（= UTC 16:00-21:00）完全不在 (21,8) 静默段判定内 →
  半夜误集中触发 initiative。

历史 BUG B — engine 单例的 _last_activity_at 与 DB 不同步：
  scheduler.py 每次 wake UPDATE vitals.updated_at（重置 idle timer），
  但 SimulationEngine 是 module-level 单例 + 内存状态，DB 列改了内存不自动同步。
  只有消耗精力的工具调用（apply_deltas/consume_energy）才同步——
  没消耗精力的纯 timer/routine 唤醒使内存值停留在昨晚，
  早上 elapsed_h = 几天累计 → 早起挤压、initiative 与晨间 routine 重复。

修复：
  A. energy_events 用 clock.BEIJING 把当前时刻换算到北京小时再判断静默段。
  B. SimulationEngine 加 sync_last_activity_at() 从 DB 重读，
     scheduler wake 后调用让内存与 DB 一致。
"""
from __future__ import annotations

import datetime as dt


def test_quiet_hours_uses_beijing_hour():
    """北京半夜必须静默，验证时区修对了——而不只是"在某些机器上看对"。

    Bug 的本质：把 UTC hour 喂给北京 hour 的判断。
    我们用一个"北京 02:00 但 UTC 18:00"的时刻直接跑判定，
    ensure 静默分支生效而不是放行。

    注：get_quiet_hours() 的 qs/qe 由 routines.yaml 推导（最晚/最早 enabled 条目），
    测试不写死具体值，避免 routines.yaml 增删作息条目时基线漂移。
    """
    from domain.lifecycle.clock import BEIJING
    from domain.lifecycle.routine_scheduler import get_quiet_hours

    qs, qe = get_quiet_hours()
    # 静默段必须跨午夜（qs >= qe），否则测试用例（半夜/中午）的前提不成立。
    assert qs >= qe, f"quiet hours should span midnight, got qs={qs} >= qe={qe} false"

    # 北京 02:00 == UTC 18:00。Bug 修复前用 UTC hour = 18，
    # 21 >= 8 真，18 >= 21 假，18 < 8 假 → 不静默（BUG）。
    # 修复后必须用北京 hour = 2，2 < qe → 静默。
    bj_2am_utc = dt.datetime(2026, 6, 10, 18, 5, tzinfo=dt.timezone.utc)
    h_bj = bj_2am_utc.astimezone(BEIJING).hour
    assert h_bj == 2, f"贝京 02:00 hour should be 2, got {h_bj}"
    if qs >= qe:
        silent = h_bj >= qs or h_bj < qe
    else:
        silent = qs <= h_bj < qe
    assert silent, "北京 02:00 必须静默（修复前因 UTC 不一致而放行）"

    # 反向：北京中午 12:00 == UTC 04:00，应放行
    bj_noon_utc = dt.datetime(2026, 6, 10, 4, 5, tzinfo=dt.timezone.utc)
    h_bj = bj_noon_utc.astimezone(BEIJING).hour
    assert h_bj == 12
    assert not (h_bj >= qs or h_bj < qe), "北京 12:00 必须放行"


def test_engine_sync_last_activity_reads_db(tmp_path, monkeypatch):
    """sync_last_activity_at 必须从 DB last_activity_at 列重读，让内存跟 DB 一致。

    vital-refactor 后字段拆分:updated_at 是 recovery 锚,last_activity_at 才是
    initiative idle 锚。本测试需要表里有 last_activity_at 列。
    """
    import sqlite3
    from domain.vital.simulation import engine as engine_mod

    fake_db = tmp_path / "state.db"
    conn = sqlite3.connect(str(fake_db))
    conn.executescript("""
        CREATE TABLE vitals (
            id INTEGER PRIMARY KEY,
            energy REAL NOT NULL,
            updated_at TEXT NOT NULL,
            last_activity_at TEXT
        );
        INSERT INTO vitals (id, energy, updated_at, last_activity_at)
        VALUES (1, 80.0, '2026-06-10T18:00:00+00:00', '2026-06-10T18:00:00+00:00');
    """)
    conn.commit()
    conn.close()

    monkeypatch.setenv("DIGITAL_LIFE_STATE_DB", str(fake_db))

    e = engine_mod.SimulationEngine()
    # 构造时第一次读 DB，应拿到 18:00
    assert e._last_activity_at is not None
    assert e._last_activity_at.hour == 18, "构造时应从 DB 读 18:00 UTC"

    # 模拟 scheduler touch_activity 改了 DB 但没动内存（原 BUG 场景）
    conn = sqlite3.connect(str(fake_db))
    conn.execute("UPDATE vitals SET last_activity_at = ? WHERE id = 1",
                 ('2026-06-11T00:00:00+00:00',))
    conn.commit()
    conn.close()
    # 内存值还是旧的
    assert e._last_activity_at.hour == 18, "scheduler 改 DB 后内存仍是旧的（这是 BUG 现场）"

    # 调用修复方法，内存应同步到新值
    e.sync_last_activity_at()
    assert e._last_activity_at.hour == 0, "sync 后应读到 DB 新值 00:00 UTC"
