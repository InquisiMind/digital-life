#!/usr/bin/env python3
"""一次性修复：messages.segment_index 段位错乱。

背景：
  SessionDB 旧版算法中 `_current_segment` 在 4 处被不同算法读写，加上
  `get_messages` 中途把 `_current_segment` reset 到 `MAX(seg)` 的副作用，
  导致"读后写"的所有 assistant/tool 全部落到 segment_index = -1；
  user 仍被分到段 0。最终根因表现：同一 session 的 100+ 条 assistant
  集中在 -1 段、几条 user 集中在 0 段，scheduler 按段号数字序排段
  (sorted([-1, 0]) = [-1, 0]) 喂给模型时，回复被整体压到提问之前——
  因果错乱。

  本修复已重写 SessionDB 算法（create_session 不再无条件重置 / append_message
  不再自增且无 -1 偏移 / 新增 advance_segment 在每个 wake 推进段号）。本脚本
  修复**已经污染的历史数据**。

策略：
  按 (session_id, timestamp, id) 自然顺序遍历 messages。每段从 user 触发
  （role == 'user'，新段号 +1）。后续同 wake 内的 assistant/tool/慢变量
  注入（role != 'user'）沿用最近 user 的段号。

  这是新代码的"段号 == wake 序号，单调递增"语义的反向落实。脚本幂等——
  基于时间表的真实自然顺序重算，跑两次结果一致。

用法（仓库根目录）：
    # dry-run：只打印每个 session 修了多少行
    python3 scripts/fix_segment_index_history.py

    # 实写（自动备份 .state.db.bak）
    python3 scripts/fix_segment_index_history.py --apply

    # 单实例
    python3 scripts/fix_segment_index_history.py <instance_id> --apply

    # 自定义 apps 目录（单测用）
    python3 scripts/fix_segment_index_history.py --apps-dir /tmp/test-apps --apply
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SessionReport:
    """单个 session 的修复报告（可断言、可序列化）。"""

    session_id: str
    rows_total: int = 0
    rows_changed: int = 0
    segments_old: set[int] = field(default_factory=set)
    segments_new: set[int] = field(default_factory=set)
    first_user_old_seg: int | None = None
    sample_changes: list[tuple[int, int, int]] = field(default_factory=list)  # (row_id, old, new)

    @property
    def had_bad_segments(self) -> bool:
        return any(seg < 0 for seg in self.segments_old)

    def summary(self) -> str:
        seg_old_str = sorted(self.segments_old) or "∅"
        seg_new_str = (
            f"[{min(self.segments_new)}..{max(self.segments_new)}]"
            if self.segments_new
            else "∅"
        )
        flag = "✓" if self.rows_changed else "·"
        return (
            f"  {flag} session={self.session_id[:25]} "
            f"rows={self.rows_total} changed={self.rows_changed} "
            f"segs(old)={seg_old_str} segs(new)={seg_new_str}"
        )


def recompute_segments(rows: list[dict]) -> list[int]:
    """对单 session 的所有行重算 segment_index。

    输入：按 (timestamp, id) 升序排序好的 message rows。
    输出：每行对应的 segment_index 数组，与 rows 一一对应。

    规则：role == 'user' 触发新段（段号 +1）。其它 role 沿用最近 user 的段号。
    若首行不是 user（异常情况，可能老 session 残留），首段 = 0 安全默认。
    """
    out: list[int] = []
    current_seg = 0  # 第一段位 0（与 create_session 一致，跑了 advance_segment 后变 1，但
    #  对修复目的说，我们不需要纠结"0 vs 1"，只需要"段号单调递增 + 同 wake 一段"）
    for row in rows:
        role = row.get("role") or ""
        if role == "user":
            current_seg += 1
        out.append(current_seg)
    return out


def fix_session_db(db_path: Path, *, apply: bool) -> list[SessionReport]:
    """修复单个 state.db 文件，逐 session 重算 segment_index。返回每个 session 的报告。"""
    # dry-run 时用只读 URI，避免在 gateway 锁定 DB 时 open 失败。
    # apply 模式需要写权限，正常 open；执行前应已 stop gateway。
    if apply:
        conn = sqlite3.connect(str(db_path))
    else:
        conn = sqlite3.connect(
            f"file:{db_path}?mode=ro&immutable=1", uri=True
        )
    conn.row_factory = sqlite3.Row
    try:
        session_ids = [
            r["session_id"]
            for r in conn.execute(
                "SELECT DISTINCT session_id FROM messages ORDER BY session_id"
            ).fetchall()
        ]
        reports: list[SessionReport] = []
        # 把所有待修改的 (row_id, new_seg) 先收集，最后一次 UPDATE 循环；
        # 这样无论是否 apply，DB 读取都是同一份只读快照。
        pending_updates: list[tuple[int, int]] = []

        for sid in session_ids:
            rows = conn.execute(
                "SELECT id, role, segment_index, timestamp FROM messages "
                "WHERE session_id = ? ORDER BY timestamp, id",
                (sid,),
            ).fetchall()
            rows_list = [dict(r) for r in rows]
            new_segs = recompute_segments(rows_list)

            report = SessionReport(session_id=sid)
            report.rows_total = len(rows_list)
            report.segments_new = set(new_segs)

            for row, new_seg in zip(rows_list, new_segs):
                old_seg = int(row.get("segment_index") or 0)
                report.segments_old.add(old_seg)
                if row.get("role") == "user" and report.first_user_old_seg is None:
                    report.first_user_old_seg = old_seg
                if old_seg != new_seg:
                    report.rows_changed += 1
                    pending_updates.append((int(row["id"]), new_seg))
                    if len(report.sample_changes) < 3:
                        report.sample_changes.append(
                            (int(row["id"]), old_seg, new_seg)
                        )
            reports.append(report)

        if apply and pending_updates:
            conn.executemany(
                "UPDATE messages SET segment_index = ? WHERE id = ?",
                [(new_seg, rid) for rid, new_seg in pending_updates],
            )
            conn.commit()
        return reports
    finally:
        conn.close()


def discover_apps_dirs(apps_dir: Path) -> list[Path]:
    return sorted(
        p
        for p in apps_dir.iterdir()
        if (p / "data" / "state.db").is_file()
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "instance_id", nargs="?", default=None,
        help="可选：单个实例 ID（仅限定那个实例）；不传则跑 apps/ 下所有",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="实际写盘（默认 dry-run，仅打印）",
    )
    parser.add_argument(
        "--apps-dir", default=None,
        help="apps 目录（默认仓库根目录的 apps/）",
    )
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parent.parent
    apps_dir = Path(args.apps_dir) if args.apps_dir else root / "apps"
    if not apps_dir.is_dir():
        print(f"✗ apps/ 目录不存在: {apps_dir}", file=sys.stderr)
        return 1

    if args.instance_id:
        targets = [apps_dir / args.instance_id]
        if not targets[0].is_dir():
            print(f"✗ 实例不存在: {targets[0]}", file=sys.stderr)
            return 1
    else:
        targets = discover_apps_dirs(apps_dir)
        if not targets:
            print("⚠ 没找到任何含 data/state.db 的实例", file=sys.stderr)
            return 1

    print(
        f"模式: {'APPLY (写盘)' if args.apply else 'DRY-RUN (只打印)'}  "
        f"实例数: {len(targets)}\n"
    )

    total_changed = 0
    for inst_dir in targets:
        db_path = inst_dir / "data" / "state.db"
        if not db_path.is_file():
            continue
        # apply 时备份一次
        backup_path = db_path.with_suffix(".db.bak")
        if args.apply and not backup_path.exists():
            shutil.copy2(db_path, backup_path)

        try:
            reports = fix_session_db(db_path, apply=args.apply)
        except Exception as exc:
            print(f"✗ {inst_dir.name[:8]} 修复失败: {exc}", file=sys.stderr)
            continue

        inst_changed = sum(r.rows_changed for r in reports)
        bad_sessions = [r for r in reports if r.had_bad_segments]
        total_changed += inst_changed
        flag = "✓" if bad_sessions else "·"
        print(
            f"[{inst_dir.name[:8]}] {flag} 实例 session 数={len(reports)} "
            f"修改行数={inst_changed} 含负段 session 数={len(bad_sessions)}"
        )
        for r in reports:
            if r.had_bad_segments or r.rows_changed:
                print(r.summary())

    print(f"\n总修改行数: {total_changed}")
    if not args.apply:
        print(
            "这是 DRY-RUN。确认无误后加 --apply 实际写盘（自动备份 .db.bak）。"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
