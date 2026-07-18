"""SimulationEngine — 数字生命的环境模拟核心。

精力系统：五段式 亢奋(85-100) → 清醒(60-85) → 平淡(40-60) → 疲惫(20-40) → 精疲力竭(0-20)

运作逻辑：
- 衰减：每次工具调用/模型访问消耗 ENERGY_COST_PER_CALL（所有调用平权）
- 恢复：随时间自动恢复，无论 RUNNING 还是 BLOCKED 状态
- 补充：仅管理台"加鸡腿"事件手动补充

三种事件：
  1. vital_threshold — RUNNING 状态下精力跌破阈值（进入疲惫/精疲力竭），提醒休息
  2. initiative — BLOCKED 状态下精力充足+空闲超时，触发主动探索
  3. nurture_energy — 前端手动加鸡腿（由 monitor 直接调 apply_deltas + emit_event）
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ── 精力段位 ──

ENERGY_SEGMENTS = [
    ("亢奋", 85, 100, "精力充沛"),
    ("清醒", 60, 85, "状态很好"),
    ("平淡", 40, 60, "还行"),
    ("疲惫", 20, 40, "有点累，想休息"),
    ("精疲力竭", 0, 20, "撑不住了"),
]

# ── 精力常量 ──

ENERGY_MAX = 100
# 每小时恢复（BLOCKED 全速 / RUNNING 减速）。默认 25 —— BLOCKED 状态下 4 小时
# 不动可从 0 恢复到 100，一夜休眠基本回满血。可通过 env
# DIGITAL_LIFE_ENERGY_RECOVERY_PER_HOUR 覆盖默认值（_resolve_energy_token_constants）。
ENERGY_RECOVERY_PER_HOUR = 25.0
# RUNNING（agent 活跃）时恢复减速系数——agent 边工作边慢补，但不补为零。
# 0.3 = 30% rate（7.5/h at 默认 25）。这个系数是产品魔数，不可配（写死在代码里）。
ENERGY_RECOVERY_RUNNING_FACTOR = 0.3
ENERGY_COST_PER_CALL = 0.2          # 每次工具调用/模型访问的"动作成本"（不与 token 挂钩）

# 精力-token 耦合（设计文档 15.4）：LLM call 走真实 token usage 消耗精力，
# 不再用上面的 ENERGY_COST_PER_CALL。
#
# 设计目标（与用户对齐 2026-06-14）：
#   - 一天满跑 ≤ 2000 万 token → 把满力 100 刚好耗光
#   - 一小时满跑 ≤ 200 万 token → 大约 1 小时扣 10 精力
#
# 推导：20M token / 100 精力 = 200K token/精力 = 0.005 精力/1K token
# output 实际计价比 input 贵，按 10× 比例分配：
#   - INPUT  = 0.005/k token （便宜，主要承担 prompt context）
#   - OUTPUT = 0.05/k token  （贵 10×，承担生成）
#
# 实际场景验算：
#   - 一次普通 LLM call: 50k input + 100 output ≈ 0.25 + 0.005 = 0.255 精力
#   - 一个 wake 平均 5-10 次 call → 1-3 精力
#   - 一小时满跑 1-2M token → 5-10 精力
#   - 一天满跑 20M token → 100 精力（正好满血耗光）
#   - 休息一天（无消耗）→ 恢复 4.17×24 = 100（正好满血复活）
#
# 工具调用（sense/terminal/todo 等）仍保持固定"动作成本"0.05-0.3，
# 不变；和 token usage 是两套独立的成本。
ENERGY_PER_KTOKEN_INPUT = 0.005
ENERGY_PER_KTOKEN_OUTPUT = 0.05


def _resolve_energy_token_constants() -> None:
    """从环境变量读 ENERGY_PER_KTOKEN_INPUT / OUTPUT / RECOVERY_PER_HOUR（config_center 注入），覆盖默认值。

    模块顶部硬编码默认值，config center 启动时如果有对应 env 就覆盖。
    """
    global ENERGY_PER_KTOKEN_INPUT, ENERGY_PER_KTOKEN_OUTPUT, ENERGY_RECOVERY_PER_HOUR
    try:
        v_in = os.environ.get("DIGITAL_LIFE_ENERGY_PER_KTOKEN_INPUT")
        if v_in:
            ENERGY_PER_KTOKEN_INPUT = float(v_in)
        v_out = os.environ.get("DIGITAL_LIFE_ENERGY_PER_KTOKEN_OUTPUT")
        if v_out:
            ENERGY_PER_KTOKEN_OUTPUT = float(v_out)
        v_rec = os.environ.get("DIGITAL_LIFE_ENERGY_RECOVERY_PER_HOUR")
        if v_rec:
            ENERGY_RECOVERY_PER_HOUR = float(v_rec)
    except (ValueError, TypeError):
        pass


# 模块导入时自动解析一次（后续 config_center 也可以再调本函数强制刷新）
_resolve_energy_token_constants()


# 主动探索
INITIATIVE_ENERGY_THRESHOLD = 50.0  # 精力 > 50 才可以主动探索
INITIATIVE_IDLE_HOURS = 2.0         # 空闲 > 2 小时触发 (2026-07-18 从 1.0 提到 2.0，减少主动探索打扰)


def _find_segment(energy: float):
    for name, lo, hi, exp in ENERGY_SEGMENTS:
        if lo <= energy <= hi:
            return name, exp
    return "未知", ""


def _now():
    from domain.lifecycle.clock import now_dt
    return now_dt()


def _now_iso():
    from domain.lifecycle.clock import now_iso
    return now_iso()


class SimulationEngine:
    """数字生命环境模拟核心 — 仅精力状态管理。"""

    def __init__(self):
        self._vitals: Optional[Dict[str, float]] = None
        self._current_segment: Optional[str] = None
        self._last_activity_at: Optional[Any] = None
        try:
            import os, sqlite3
            from domain.lifecycle.clock import parse_iso
            from pathlib import Path
            override = os.environ.get("DIGITAL_LIFE_STATE_DB")
            if override:
                db_path = Path(override)
            else:
                from infrastructure.config import get_runtime_state_db_path
                db_path = get_runtime_state_db_path()
            db = sqlite3.connect(str(db_path))
            # 优先读 last_activity_at（vital-refactor 后的 initiative idle 锚）；
            # 老库 migration 会自动加该列并回填 = updated_at，但健壮起见 fallback。
            r = db.execute(
                "SELECT last_activity_at, updated_at FROM vitals WHERE id=1"
            ).fetchone()
            db.close()
            if r:
                # state.py 的 init_vitals_db 会自动 ALTER + 回填 last_activity_at
                val = r[0] or r[1]
                self._last_activity_at = parse_iso(val) if val else None
        except Exception:
            pass

    # ── 内部状态读写 ──

    def _load(self) -> None:
        from domain.lifecycle.affairs.runtime import get_vitals
        self._vitals = get_vitals()

    def _save(self) -> None:
        from domain.lifecycle.affairs.runtime import save_vitals
        if self._vitals:
            save_vitals(self._vitals)

    def _v(self) -> Dict[str, float]:
        """读当前 vitals snapshot,给 check_energy_events 比段位用。

        走 domain.vital.state.get_current_vitals() —— 这是事实路径(已按 elapsed
        + rate 算过恢复)。曾经走 affairs.runtime.get_vitals(),但后者是废弃的
        DECAY=-40/h 反方向实现,会污染段位判断。
        """
        from domain.vital.state import get_current_vitals
        snap = get_current_vitals()
        self._vitals = {"energy": snap.energy}
        return self._vitals

    # ── 状态查询 ──

    def get_state(self) -> Dict[str, Any]:
        v = self._v()
        energy = v["energy"]
        return {
            "energy": round(energy, 1),
            "segment": _find_segment(energy)[0],
            "now": _now_iso(),
        }

    def get_energy_state(self) -> Dict[str, Any]:
        v = self._v()
        energy = v["energy"]
        seg_name, seg_exp = _find_segment(energy)
        return {
            "energy": round(energy, 1),
            "segment": seg_name,
            "experience": seg_exp,
            "now": _now_iso(),
        }

    # ── 精力操作 ──

    def apply_deltas(self, deltas: Dict[str, float]) -> None:
        v = self._v()
        for dim, delta in deltas.items():
            if dim in v:
                v[dim] = max(0, min(ENERGY_MAX, v[dim] + delta))
        self._vitals = v
        self._last_activity_at = _now()
        self._save()

    def consume_energy(self, amount: float) -> float:
        v = self._v()
        v["energy"] = max(0, v["energy"] - abs(amount))
        self._vitals = v
        self._last_activity_at = _now()
        self._save()
        return v["energy"]

    def sync_last_activity_at(self) -> None:
        """从 DB 重读 last_activity_at 到内存。

        vital-refactor 后:scheduler.wake 改成 touch_activity()(显式更新
        last_activity_at)→ 紧接调本方法同步内存。本类的 _last_activity_at 是
        check_energy_events 里 initiative idle 计时的内存锚。
        """
        try:
            import sqlite3
            from domain.lifecycle.clock import parse_iso
            from infrastructure.config import get_runtime_state_db_path
            import os
            from pathlib import Path
            override = os.environ.get("DIGITAL_LIFE_STATE_DB")
            db_path = Path(override) if override else get_runtime_state_db_path()
            db = sqlite3.connect(str(db_path))
            try:
                # 优先 last_activity_at(refactor 后的锚),fallback updated_at
                r = db.execute(
                    "SELECT last_activity_at, updated_at FROM vitals WHERE id=1"
                ).fetchone()
                if r:
                    val = r[0] or r[1]
                    self._last_activity_at = parse_iso(val) if val else None
            finally:
                db.close()
        except Exception as exc:
            logger.warning("sync_last_activity_at failed: %s", exc)

    # ── 时间推进 ──

    def tick(self, state: str = "BLOCKED") -> Dict[str, Any]:
        """每隔 cron_tick 调用一次,推进恢复 + 检测衍生事件。

        state: "BLOCKED"（默认,全速恢复） / "RUNNING"（30% 恢复）。

        重要:recovery 计算走 domain.vital.state.get_current_vitals(state=...,
        persist=True),而不是 affairs.runtime.get_vitals() —— 后者写的是"每小時
        -40 衰减"方向跟设计相反,是历史残留。state.py 的 _apply_recovery 才是
        事实路径。
        """
        from domain.vital.state import get_current_vitals
        from .energy_events import check_energy_events

        get_current_vitals(state=state, persist=True)  # 推进恢复 + 推进 updated_at
        check_energy_events(self)                       # 段位变化 → vital_threshold
        return {"vitals": self._v()}


# ── 全局单例 ──

_engine: Optional[SimulationEngine] = None


def get_engine() -> SimulationEngine:
    global _engine
    if _engine is None:
        _engine = SimulationEngine()
    return _engine


def reset_engine() -> None:
    global _engine
    _engine = None


__all__ = [
    "SimulationEngine",
    "get_engine",
    "reset_engine",
    "ENERGY_SEGMENTS",
    "ENERGY_MAX",
    "ENERGY_RECOVERY_PER_HOUR",
    "ENERGY_COST_PER_CALL",
    "INITIATIVE_ENERGY_THRESHOLD",
    "INITIATIVE_IDLE_HOURS",
]
