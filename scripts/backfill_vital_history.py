"""One-shot backfill: 重建部署前的历史精力值波动曲线。

背景：``domain/vital/state.py`` 的 ``vital_history`` 采样表是新加的，
部署前系统跑了很久的自动恢复，但那段历史从没被采样记下。本脚本从当前
``vitals`` 当前值倒推 ``nurture_log`` 事件流，按恢复斜率 +25/h（BLOCKED
全速，无法精确知道历史 affair 状态故一律按全速近似）重建近期 N 小时的
精力值曲线，回填进 ``vital_history``。

近似性（务必知情）：
  - 历史时段的 BLOCKED/RUNNING 状态切换无记录，一律按 BLOCKED 全速恢复算。
    影响主要在长空段（如凌晨纯恢复），事件密集段近似很准。
  - env ``DIGITAL_LIFE_ENERGY_RECOVERY_PER_HOUR`` 若曾改过，回算用当前值。
  - clamp 到 [0,100]：防止倒推时漂出物理边界。

幂等：覆盖目标时间窗内的 vital_history 行（先 DELETE 再 INSERT）。
安全：默认 --dry-run，必须显式 --apply 才写库。

Usage:
    python3 scripts/backfill_vital_history.py                     # 列实例 + dry-run
    python3 scripts/backfill_vital_history.py --instance <iid>    # 指定实例 dry-run
    python3 scripts/backfill_vital_history.py --instance <iid> --hours 24 --apply
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _parse_iso(s: str) -> datetime:
    """容忍带/不带 timezone 的 ISO 字符串。"""
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # 容忍 'YYYY-MM-DD HH:MM:SS' 空格分隔
        dt = datetime.fromisoformat(s.replace(" ", "T"))
    if dt.tzinfo is None:
        # 历史 nurture_log 用 +08:00 写，naive 当作北京
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return dt


def _clamp(v: float) -> float:
    return max(0.0, min(100.0, v))


def _backfill_one(db_path: Path, hours: int, recovery_per_hour: float, apply: bool) -> tuple[int, int]:
    """返回 (回填采样数, 事件数)。"""
    if not db_path.exists():
        print(f"  跳过：{db_path} 不存在")
        return 0, 0
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        # 确认表存在（没建过说明没跑过新代码，跳过）
        try:
            row = conn.execute("SELECT energy FROM vitals WHERE id=1").fetchone()
        except sqlite3.OperationalError:
            print(f"  跳过：{db_path.name} 无 vitals 表")
            return 0, 0
        if not row:
            return 0, 0
        try:
            conn.execute("SELECT 1 FROM vital_history LIMIT 1")
        except sqlite3.OperationalError:
            print(f"  跳过：{db_path.name} 无 vital_history 表（先部署新代码再跑）")
            return 0, 0

        current_energy = float(row["energy"])
        now = datetime.now().astimezone()
        window_start = now - timedelta(hours=hours)
        window_start_iso = window_start.isoformat(timespec="seconds")

        events = conn.execute(
            "SELECT at, deltas_json FROM nurture_log WHERE at >= ? ORDER BY at DESC",
            (window_start_iso,),
        ).fetchall()

        # 倒推重建：从 now 的 current_energy 往回走
        # - 每个事件点：消耗(Δ<0)→ 之前更高 → energy -= Δ；投喂(Δ>0)→ 之前更低 → energy -= Δ
        #   统一式：energy(t-) = clamp(energy(t+) - Δ)，因为 Δ 是"在 t 时刻施加的变化"
        # - 段间（t- 到上一个事件 t+）：这段时间在恢复，倒推时 energy 是更高的，
        #   每秒 +recovery_per_hour/3600（按 BLOCKED 全速假设，近似）
        recovery_per_sec = recovery_per_hour / 3600.0

        # 采样点：每分钟一个，从 now 倒推到 window_start
        sample_minutes = hours * 60
        samples: list[tuple[float, float, str]] = []  # (unix_ts, energy, affair_state)

        cursor_t = now
        cursor_energy = current_energy
        ev_idx = 0  # events 已按时间 DESC，ev_idx=0 是最近
        # 把事件按"何时施加"对齐：cursor_t 往前走时，遇到事件时刻就施加

        for i in range(sample_minutes):
            step_end = cursor_t
            step_start = cursor_t - timedelta(minutes=1)
            # 这一分钟内发生的事件（step_start < ev.at <= step_end，倒推方向）
            # 同时也要把它们的反效果施加到 cursor_energy（往回走=能量更高）
            while ev_idx < len(events):
                ev_at = _parse_iso(events[ev_idx]["at"])
                if ev_at > step_end:  # 事件比 step_end 还晚，跳过（不应发生在 DESC 顺序，兜底）
                    ev_idx += 1
                    continue
                if ev_at <= step_start:  # 本分钟内没有更多事件了
                    break
                # 该事件落在 (step_start, step_end]：倒推时之前更高，减掉 Δ
                try:
                    deltas = json.loads(events[ev_idx]["deltas_json"] or "{}")
                except Exception:
                    deltas = {}
                d = float(deltas.get("energy") or 0.0)
                cursor_energy = _clamp(cursor_energy - d)
                ev_idx += 1

            # 这一分钟的恢复段倒推：往回 60s，能量更高（恢复走了 60s）
            cursor_energy = _clamp(cursor_energy + recovery_per_sec * 60)
            # 记采样点（用 step_start 的时间戳，表示"该时刻的精力值"）
            samples.append((step_start.timestamp(), cursor_energy, "BLOCKED"))
            cursor_t = step_start
            if cursor_t <= window_start:
                break

        samples.reverse()  # vital_history 期望按时间升序插入

        print(f"  重建 {len(samples)} 个采样点，覆盖 {hours}h，事件数 {len(events)}")
        print(f"  起点（{window_start.isoformat(timespec='hours')}）≈ {samples[0][1]:.1f}%，"
              f" 当前 {current_energy:.1f}%")

        if apply and samples:
            # 幂等：删目标窗口内旧行
            conn.execute("DELETE FROM vital_history WHERE at >= ?", (window_start.timestamp(),))
            conn.executemany(
                "INSERT INTO vital_history (at, energy, affair_state) VALUES (?, ?, ?)",
                samples,
            )
            conn.commit()
            print(f"  ✓ 已写库（先 DELETE {hours}h 窗口再 INSERT {len(samples)} 行）")
        elif not apply:
            print("  （dry-run，未写库；加 --apply 实际回填）")

        return len(samples), len(events)
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--instance", help="只处理指定实例 id（默认全部 apps/*/data/state.db）")
    parser.add_argument("--hours", type=int, default=24, help="回算近 N 小时（默认 24）")
    parser.add_argument("--apply", action="store_true", help="实际写库（默认 dry-run）")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    apps = repo_root / "apps"
    if not apps.is_dir():
        print("找不到 apps/ 目录")
        return 1

    # 恢复速率：读 env 覆盖（回算用当前配置值）
    recovery = float(os.environ.get("DIGITAL_LIFE_ENERGY_RECOVERY_PER_HOUR", "25"))
    print(f"恢复速率：{recovery}/h（按 BLOCKED 全速近似）\n")

    total_samples = 0
    if args.instance:
        dbs = [apps / args.instance / "data" / "state.db"]
    else:
        dbs = sorted(apps.glob("*/data/state.db"))

    for db_path in dbs:
        iid = db_path.parent.parent.name
        if len(iid) != 36 or iid.startswith("test"):  # 过滤测试实例
            continue
        print(f"[{iid[:8]}] {db_path}")
        n, _ = _backfill_one(db_path, args.hours, recovery, args.apply)
        total_samples += n
        print()

    print(f"完成：共回填 {total_samples} 个采样点（{'已写库' if args.apply else 'dry-run'}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
