"""精力状态管理 — VitalSnapshot + 持久化 + 养育记录。

精力运作逻辑：
- 恢复：随时间自动恢复（默认 +25/h，env DIGITAL_LIFE_ENERGY_RECOVERY_PER_HOUR
  可配）。BLOCKED 全速 RUN_RUNNING 减速到 30%（agent 边工作边慢补）。每个 cron
  tick（默认 60s，全状态覆盖）写盘一次，用户/前端读 get_current_vitals()
  拿到的永远是当前时刻事实值，不再 stale。
- 衰减：每次工具调用/模型访问消耗精力（由外部 consume_energy 触发，agent
  LLM call 后按真实 token 量扣）。
- 补充：仅前端"加鸡腿"按钮（走 monitor.nurture_energy）。
- 入站消息不再影响精力（不在 handler 里 apply_nurture）——精力只通过
  consume_energy / nurture_energy 两个明确通道进出。

字段语义（拆字段解耦）：
- `updated_at`: 恢复计时基准。_apply_recovery 算 elapsed_h 时用它作 anchor，
  recovery 写回时不动它（保留"最后真实事件时刻"）。但 consume_energy /
  apply_nurture 会更新它（消耗/投喂也是真实事件）。
- `last_activity_at`: 主动性 idle baseline。initiative 系统算"距上次活动多久"
  时用它；wake 时由 scheduler.touch_activity() 显式推进，不再用 UPDATE
  vitals SET updated_at 这种 hack 偷渡。
"""

from __future__ import annotations

import json as _json
from dataclasses import dataclass as _dataclass
from datetime import timedelta as _td
from typing import Dict, List, Optional

from domain.lifecycle.clock import now_dt, now_iso, parse_iso

from domain.lifecycle.affairs.runtime import (
    _conn,
    init_db as _init_affairs_db,
)


def _vitals_enabled() -> bool:
    """capability 闸（设计文档 v1.1 特性 4）：服务型默认 vitals 关。

    关 = "不生效"而非"没有"：所有变更/持久化跳过，读返回恒定默认快照，
    不建 vitals 表（服务 data 目录零精力痕迹）；实例型恒 True，零变化。
    """
    try:
        from domain.service.capabilities import capability_enabled

        return capability_enabled("vitals")
    except Exception:
        return True


# ---------------- schema ----------------
VITALS_SCHEMA = """
CREATE TABLE IF NOT EXISTS vitals (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    energy           REAL NOT NULL DEFAULT 70.0,
    updated_at       TEXT NOT NULL,
    last_activity_at TEXT,
    last_nurture     TEXT,
    meta_json        TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS nurture_log (
    log_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    at               TEXT NOT NULL,
    kind             TEXT NOT NULL,
    deltas_json      TEXT NOT NULL,
    raw_text         TEXT,
    source           TEXT
);

CREATE INDEX IF NOT EXISTS idx_nurture_at ON nurture_log(at DESC);

-- 精力值采样（每 cron tick ~60s 一行，供前端画精力值波动曲线：涨=自然恢复，跌=消耗）。
-- 与 budget_log 的 occurred_at 同用 Unix 秒，便于与 token 图同时间轴叠放。
CREATE TABLE IF NOT EXISTS vital_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    at           REAL NOT NULL,           -- Unix ts（秒）
    energy       REAL NOT NULL,
    affair_state TEXT                     -- BLOCKED/RUNNING：区分全速恢复与 30% 速率段
);
CREATE INDEX IF NOT EXISTS idx_vital_history_at ON vital_history(at DESC);
"""


def init_vitals_db() -> None:
    _init_affairs_db()
    with _conn() as c:
        c.executescript(VITALS_SCHEMA)
        # schema migration: 老库没有 last_activity_at 列时给它加上,回填 = updated_at
        cols = {row[1] for row in c.execute("PRAGMA table_info(vitals)").fetchall()}
        if "last_activity_at" not in cols:
            c.execute("ALTER TABLE vitals ADD COLUMN last_activity_at TEXT")
            c.execute("UPDATE vitals SET last_activity_at = updated_at WHERE id = 1")
        row = c.execute("SELECT id FROM vitals WHERE id = 1").fetchone()
        if not row:
            now = now_iso()
            c.execute(
                "INSERT INTO vitals (id, energy, updated_at, last_activity_at) VALUES (1, 70.0, ?, ?)",
                (now, now),
            )


# ---------------- data class ----------------

@_dataclass
class VitalSnapshot:
    energy: float
    updated_at: str
    last_activity_at: Optional[str] = None


# ---------------- core API ----------------

def _apply_recovery(base: VitalSnapshot, state: str = "BLOCKED") -> VitalSnapshot:
    """随时间恢复精力。state 决定 rate：BLOCKED 全速 / RUNNING 30% 减速。"""
    from domain.vital.simulation.engine import (
        ENERGY_RECOVERY_PER_HOUR,
        ENERGY_RECOVERY_RUNNING_FACTOR,
    )

    factor = ENERGY_RECOVERY_RUNNING_FACTOR if state == "RUNNING" else 1.0
    now = now_dt()
    last = parse_iso(base.updated_at)
    elapsed_h = max(0.0, (now - last).total_seconds() / 3600.0)
    new_energy = base.energy + ENERGY_RECOVERY_PER_HOUR * factor * elapsed_h

    return VitalSnapshot(
        energy=max(0, min(100, new_energy)),
        updated_at=base.updated_at,  # 不更新 updated_at——保留恢复计时锚
        last_activity_at=base.last_activity_at,
    )


def get_current_vitals(state: str = "BLOCKED", persist: bool = False) -> VitalSnapshot:
    """读当前精力快照。

    state:  "BLOCKED"（默认）/ "RUNNING" —— 决定 recovery rate。
    persist: True 时把恢复后的 energy + 当前时间作为新 updated_at 落盘(锚推进)。
            cron tick 应传 True(每次 tick 重置锚,防止 energy 重复累加)。
            API 读路径默认 False(纯显示,不污染 DB,不在高频轮询时反复写盘)。
    """
    if not _vitals_enabled():
        # capability 关：恒定默认快照，不 init 不落盘（字段保留不读）
        return VitalSnapshot(energy=70.0, updated_at=now_iso(), last_activity_at=now_iso())
    init_vitals_db()
    with _conn() as c:
        row = c.execute("SELECT * FROM vitals WHERE id = 1").fetchone()
    if not row:
        snap = VitalSnapshot(energy=70.0, updated_at=now_iso(), last_activity_at=now_iso())
        if persist:
            _persist_snapshot_for_tick(snap, affair_state=state)
        return snap

    keys = row.keys()
    base = VitalSnapshot(
        energy=row["energy"] if "energy" in keys else 70.0,
        updated_at=row["updated_at"],
        last_activity_at=row["last_activity_at"] if "last_activity_at" in keys else row["updated_at"],
    )

    recovered = _apply_recovery(base, state=state)

    # tick 路径:落盘 energy 并推进 updated_at 到 now(下次 tick 从此计 elapsed)。
    # 不动 last_activity_at(那是 initiative 的锚,只在 consume/nurture/touch 时推进)。
    if persist and abs(recovered.energy - base.energy) > 0.01:
        _persist_snapshot_for_tick(recovered, affair_state=state)
    elif persist:
        # 即使没有恢复量变化（已达上限等），tick 也采样一行 —— 让曲线连续不断点。
        _persist_snapshot_for_tick(recovered, affair_state=state)

    return recovered


def _persist_snapshot_for_tick(snap: VitalSnapshot, *, affair_state: str = "") -> None:
    """tick 路径专用:推进 updated_at 到 now,不碰 last_activity_at。

    同时向 vital_history 表追加一行采样（供前端画精力值波动曲线）。
    采样仅在 persist=True 的 tick 路径触发，API 读路径不污染历史。
    """
    import time as _time
    now_unix = _time.time()
    with _conn() as c:
        c.execute("UPDATE vitals SET energy=?, updated_at=? WHERE id=1",
                  (snap.energy, now_iso()))
        # 采样历史：失败不影响 tick 主路径（best-effort）。
        try:
            c.execute(
                "INSERT INTO vital_history (at, energy, affair_state) VALUES (?, ?, ?)",
                (now_unix, snap.energy, affair_state),
            )
        except Exception:
            pass


def touch_activity() -> None:
    """显式推进 last_activity_at——wake 时主动标记"刚活动"。

    替代旧 scheduler.py:581-601 的 UPDATE vitals SET updated_at hack。
    一行一事——以前那个 hack 被骂"绑在事件机制上"因为它偷偷改了 recovery 计时锚。
    """
    if not _vitals_enabled():
        return
    init_vitals_db()
    with _conn() as c:
        c.execute("UPDATE vitals SET last_activity_at=? WHERE id=1", (now_iso(),))


def _persist_snapshot(snap: VitalSnapshot, last_nurture: Optional[str] = None) -> None:
    """写入精力快照并更新 updated_at + last_activity_at（仅在真实事件时调用）。"""
    now = now_iso()
    with _conn() as c:
        if last_nurture:
            c.execute(
                "UPDATE vitals SET energy=?, updated_at=?, last_activity_at=?, last_nurture=? WHERE id=1",
                (snap.energy, now, now, last_nurture),
            )
        else:
            c.execute(
                "UPDATE vitals SET energy=?, updated_at=?, last_activity_at=? WHERE id=1",
                (snap.energy, now, now),
            )


def apply_nurture(kind: str, deltas: Dict[str, float],
                  raw_text: str = "", source: str = "") -> VitalSnapshot:
    """外部养育操作（管理台加鸡腿等）。更新 updated_at + last_activity_at。"""
    if not _vitals_enabled():
        return get_current_vitals(persist=False)
    init_vitals_db()
    current = get_current_vitals()
    now = now_iso()
    new = VitalSnapshot(
        energy=max(0, min(100, current.energy + deltas.get("energy", 0))),
        updated_at=now,
        last_activity_at=now,
    )
    _persist_snapshot(new, last_nurture=kind)

    with _conn() as c:
        c.execute(
            "INSERT INTO nurture_log (at, kind, deltas_json, raw_text, source) VALUES (?, ?, ?, ?, ?)",
            (now, kind, _json.dumps(deltas, ensure_ascii=False), raw_text, source),
        )
    return new


def consume_energy(amount: float, reason: str = "activity") -> VitalSnapshot:
    """消耗精力（工具调用/模型访问）。更新 updated_at + last_activity_at。"""
    return apply_nurture(kind=f"energy_cost:{reason}", deltas={"energy": -amount})


def recent_nurture_log(hours: int = 24) -> List[Dict]:
    init_vitals_db()
    since = (now_dt() - _td(hours=hours)).isoformat(timespec="seconds")
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM nurture_log WHERE at >= ? ORDER BY at DESC",
            (since,),
        ).fetchall()
    return [
        {
            "at": r["at"],
            "kind": r["kind"],
            "deltas": _json.loads(r["deltas_json"]),
            "raw_text": r["raw_text"] or "",
            "source": r["source"] or "",
        }
        for r in rows
    ]


def vital_history_series(hours: int = 24) -> List[Dict]:
    """读近 N 小时的精力采样序列（供前端精力值波动曲线：涨=恢复,跌=消耗）。

    返回按时间升序（旧→新）的 [at_iso, energy, affair_state] 列表。
    """
    import time as _time
    init_vitals_db()
    since_unix = _time.time() - hours * 3600
    with _conn() as c:
        rows = c.execute(
            "SELECT at, energy, affair_state FROM vital_history "
            "WHERE at >= ? ORDER BY at ASC",
            (since_unix,),
        ).fetchall()
    return [
        {
            "at_unix": r["at"],
            "energy": r["energy"],
            "affair_state": r["affair_state"] or "",
        }
        for r in rows
    ]


# ---------------- formatting ----------------

def format_vitals_report(snap: Optional[VitalSnapshot] = None) -> str:
    from domain.vital.simulation.engine import ENERGY_SEGMENTS

    snap = snap or get_current_vitals()
    energy = snap.energy

    seg_name = "未知"
    seg_exp = ""
    for name, lo, hi, exp in ENERGY_SEGMENTS:
        if lo <= energy <= hi:
            seg_name = name
            seg_exp = exp
            break

    lines = [f"## 精力状态  {energy:.0f}/100（{seg_name}）"]
    if seg_exp:
        lines.append(f"> {seg_exp}")

    return "\n".join(lines)


__all__ = [
    "VitalSnapshot",
    "init_vitals_db",
    "get_current_vitals",
    "touch_activity",
    "apply_nurture",
    "consume_energy",
    "recent_nurture_log",
    "format_vitals_report",
]
