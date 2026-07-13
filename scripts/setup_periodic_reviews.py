"""Setup periodic review alarms for trading_simulation.

Time-type todos that emit `task_todo_due` alarm events at:
  - 21:00 every weekday (Mon-Fri) — 日晚复盘
  - 21:00 Sunday — 周策略 review
  - 09:00 first day of next month — 月度里程碑

The fire of these alarms will wake the assignee with the todo content +
declare which skill to load (this will be done by scheduler injecting
relevant skill based on task.type).

Schedule produced covers the next 7 days from today — re-running this
script extends the schedule. Run via cron or manually each week.

Each todo created is idempotent: if a todo with the same content prefix
exists for the date, it's skipped.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

ZERO_IID = "c2a5c8e8-e4f5-4c69-be3e-aac49903081d"
ALPHA_IID = "5052c33a-e700-44dd-aea3-00e04a661ab1"
DAYS_AHEAD = 7


def _date_label(d: dt.date) -> str:
    return f"{d.isoformat()} ({['周一','周二','周三','周四','周五','周六','周日'][d.weekday()]})"


def main() -> int:
    from infrastructure.config import set_current_instance_id, reset_current_instance_id
    from domain.todos import list_todos, create_todo as _create_todo
    from domain.todos.crud import create_todo

    today = dt.date.today()
    created_count = 0
    skipped_count = 0

    # ── Generate schedule for next DAYS_AHEAD days ──────────────────
    schedule: list[tuple[dt.date, str, str, str]] = []  # (date, time, assignee, content)
    for i in range(DAYS_AHEAD):
        d = today + dt.timedelta(days=i)
        wd = d.weekday()
        # Mon-Fri 21:00 daily review (zero)
        if 0 <= wd <= 4:
            schedule.append((d, "21:00", ZERO_IID, f"每日晚复盘（{_date_label(d)}）：评估今日论断成立 + KPI 偏离 + 明日计划。完成后调 add_lesson 写 1 条反思"))
        # Saturday → skip
        # Sunday 21:00 weekly review (zero)
        if wd == 6:
            schedule.append((d, "21:00", ZERO_IID, f"每周策略 review（{_date_label(d)}）：逐条 thesis 评估是否还成立 + 是否要换论断 + 调整下周 plan"))
            # Also: first day of each month check (within horizon)
        if d.day == 1:
            schedule.append((d, "09:00", ZERO_IID, f"每月里程碑回顾（{_date_label(d)}）：本月目标是否达成 + 是否换论断 + 下月计划"))
        # Daily 16:00 trader report (alpha)
        if 0 <= wd <= 4:
            schedule.append((d, "16:00", ALPHA_IID, f"持仓日报（{_date_label(d)}）：持仓 / 浮盈 / 今日操作 / 异常。简短 5-10 行"))

    if not schedule:
        print(f"No review events scheduled in next {DAYS_AHEAD} days")
        return 0

    print(f"=== Scheduling next {DAYS_AHEAD} days of reviews ({len(schedule)} events) ===\n")
    for d, time_str, assignee_iid, content in schedule:
        iso = f"{d.isoformat()}T{time_str}:00+08:00"
        # Idempotency check: same content prefix in assignee's todos (any status)
        token = set_current_instance_id(assignee_iid)
        existing = []
        try:
            existing = list_todos(assignee=assignee_iid)
        except Exception:
            pass
        finally:
            reset_current_instance_id(token)

        dup = False
        for t in existing:
            if (t.get("content") or "").startswith(content[:40]) and t.get("status") in ("pending", "active"):
                dup = True
                break

        if dup:
            skipped_count += 1
            print(f"  ⊘ skip  ({iso}) [{assignee_iid[:8]}] {content[:60]}")
            continue

        result = create_todo(
            task_id="trading_management",
            assignee=assignee_iid,
            content=content,
            trigger_type="time",
            due_at=iso,
        )
        if result.get("ok"):
            created_count += 1
            print(f"  ✓ added ({iso}) [{assignee_iid[:8]}] {content[:60]}")
        else:
            print(f"  ✗ fail  ({iso}) {result.get('reason')}")

    print(f"\n=== done: {created_count} added, {skipped_count} skipped (already scheduled) ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
