"""Bootstrap `trading_simulation` project's task tree + initial todos.

Idempotent: re-running picks up the tree (skips existing) and just syncs todos.

Generated structure:
    根任务：模拟炒股 (zero 主担, project_root)
    ├─ 项目分工 (zero 主担, project_bootstrap)
    ├─ 项目管理 (zero 主担, project_management)
    │     todos:
    │       [zero, ongoing] 推进项目节奏
    │       [zero, time 09:30] 日晨会发起
    │       [zero, time 21:00] 日晚复盘
    ├─ 论断与策略制定 (zero 主担, project_bootstrap)
    │     todos:
    │       [zero, ongoing] 维护策略论断文档
    │       [zero, time weekly 21:00] 周策略 review
    └─ 交易执行与风控 (alpha 主担)
          todos:
            [alpha, ongoing] 持续关注盘中异动
            [alpha, time 16:00] 每日持仓日报
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

PROJECT_ID = "trading_simulation"
ZERO_IID = "c2a5c8e8-e4f5-4c69-be3e-aac49903081d"   # strategist / PM of meta
ALPHA_IID = "5052c33a-e700-44dd-aea3-00e04a661ab1"   # trader
TODAY = "2026-06-09"


def main() -> int:
    sys.path.insert(0, str(PROJECT_ROOT))
    from domain.project._infra import get_project_db
    from domain.project.crud import (
        list_project_tasks,
        create_project_task,
        update_project_task,
    )
    from domain.project.loader import load_all_projects

    projects = load_all_projects()
    cfg = projects.get(PROJECT_ID)
    if not cfg:
        print(f"project {PROJECT_ID} not found in projects yaml", file=sys.stderr)
        return 1
    print(f"=== Project: {cfg.name} ({cfg.description[:60]}...) ===")

    db = get_project_db(PROJECT_ID)
    try:
        # ── verify / create 根任务 ──
        existing = list_project_tasks(db, parent_task_id="")
        root_id = None
        for r in existing:
            if r["type"] == "project_root":
                root_id = r["id"]
                break

        if not root_id:
            # Use existing helper which creates root + 2 children
            from domain.project.crud import init_project_task_tree
            result = init_project_task_tree(
                db, PROJECT_ID,
                project_name=cfg.name,
                project_description=cfg.description,
                manager=cfg.manager or ZERO_IID,
            )
            root_id = result["root_task_id"]
            bootstrap_id = result["bootstrap_task_id"]
            mgmt_id = result["management_task_id"]
            print(f"  ✓ tree bootstrapped: root={root_id} bootstrap={bootstrap_id} mgmt={mgmt_id}")
        else:
            bootstrap_id = None
            mgmt_id = None
            for t in list_project_tasks(db, parent_task_id=root_id):
                if t["type"] == "project_bootstrap":
                    bootstrap_id = t["id"]
                elif t["type"] == "project_management":
                    mgmt_id = t["id"]
            print(f"  tree already initialized: root={root_id} bootstrap={bootstrap_id} mgmt={mgmt_id}")

        # ── multiplex: 让「项目管理」任务承担人明确 = zero (PM) ──
        if mgmt_id:
            update_project_task(db, mgmt_id, assignee_instance=ZERO_IID)

        # ── 添加 trading-specific 子任务（论断/策略 + 交易执行） ──
        # 论断与策略（zero 主担）+ Assoc project_bootstrap skill
        strat_existing = [t for t in list_project_tasks(db, parent_task_id=root_id)
                         if "论断" in t["title"] or "策略" in t["title"]]
        if not strat_existing:
            strat_id = create_project_task(
                db,
                title="论断与策略制定",
                description=(
                    "维护 trading_simulation 的核心论断文档："
                    "市场观察 / 龙头选择 / ETF 趋势 / 风控规则。"
                    "每周评估论断是否还成立，调整时通过 human_directive 通知真人。"
                ),
                parent_task_id=root_id,
                assignee_instance=ZERO_IID,
                assignee_kind="instance",
                type="project_bootstrap",
                sort_order=3,
            )
            print(f"  ✓ created strategy task: {strat_id}")
        else:
            print(f"  strategy task already exists: {strat_existing[0]['id']}")

        # 交易执行与风控（alpha 主担）
        exec_existing = [t for t in list_project_tasks(db, parent_task_id=root_id)
                        if "交易" in t["title"]]
        if not exec_existing:
            exec_id = create_project_task(
                db,
                title="交易执行与风控",
                description=(
                    "Alpha 作为交易员承担：盘中监控 / 下单执行 / 仓位控制 / 每日交易日报。"
                    "出现重大风控信号（持仓逼近止损线 / 异常波动）→ 通过 express_to_human 立即通知 zero 和真人。"
                ),
                parent_task_id=root_id,
                assignee_instance=ALPHA_IID,
                assignee_kind="instance",
                type="trade_execution",
                sort_order=4,
            )
            print(f"  ✓ created execution task: {exec_id}")
        else:
            print(f"  execution task already exists: {exec_existing[0]['id']}")

        # ── 注册初始 todos —— 写到对应承担者自己的实例 DB ──
        # 注意：create_todo 现在是 ctx-scoped，我们要切换 ContextVar 来写到正确实例。
        from infrastructure.config import set_current_instance_id, reset_current_instance_id
        from domain.todos import list_todos, create_todo
        from domain.todos.crud import create_todo as _create_todo

        def _ensure_todo(assignee_iid: str, *, content: str, trigger_type: str = "time",
                         due_at: str | None = None, condition: str | None = None) -> None:
            """Switch ContextVar to assignee's tasks DB, then create (skip duplicates)."""
            token = set_current_instance_id(assignee_iid)
            try:
                # Idempotency: scan existing todos for matching content prefix
                existing = list_todos(assignee=assignee_iid, status="pending")
                for t in existing:
                    if (t.get("content") or "").startswith(content[:40]):
                        print(f"    todo already exists: {content[:40]}... (id={t['id']})")
                        return
                # And cancel any status too
                # All todos table per instance — also check cancelled
                cancelled = list_todos(assignee=assignee_iid, status="cancelled")
                for t in cancelled:
                    if (t.get("content") or "").startswith(content[:40]):
                        print(f"    todo previously cancelled: {content[:40]}... recreating")
                        break
                _create_todo(
                    task_id=",".join([t.strip() for t in [strat_id if 'strat_id' in dir() else '', exec_id if 'exec_id' in dir() else ''] if t.strip()]) or "personal",
                    assignee=assignee_iid,
                    content=content,
                    trigger_type=trigger_type,
                    due_at=due_at,
                    condition=condition,
                )
                print(f"  ✓ todo for {assignee_iid[:8]} [{trigger_type}] {content[:50]}")
            finally:
                reset_current_instance_id(token)

        # 1. zero — ongoing: 推进项目节奏
        _ensure_todo(ZERO_IID, content="推进 trading_simulation 项目节奏：评估论断 → 检查 KPI 偏离 → 派活或决议 → 没事 rest",
                     trigger_type="ongoing")
        # 2. zero — daily 09:30 晨会发起（条件型，时间表达用 due_at；重复闹钟通过 alarm 系统）
        _ensure_todo(ZERO_IID, content=f"每日 09:30 发起日晨会（@alpha 简短对齐昨日状态 + 今日计划）",
                     trigger_type="condition",
                     condition="工作日 09:30 触发日晨会发起；周末跳过")
        # 3. zero — daily 21:00 复盘
        _ensure_todo(ZERO_IID, content=f"每日 21:00 收盘复盘（评估策略论断 / 持仓状态 / 明日计划）",
                     trigger_type="condition",
                     condition="每日 21:00 触发晚复盘")
        # 4. zero — weekly review
        _ensure_todo(ZERO_IID, content="每周日 21:00 发起周策略 review（评估论断当前是否成立，调整则通知真人和 alpha）",
                     trigger_type="condition",
                     condition="每周日 21:00 触发周策略 review")
        # 5. alpha — ongoing 持续关注盘中异动
        _ensure_todo(ALPHA_IID, content="交易时段持续关注持仓 / 候选龙头异动（非必要时静默）",
                     trigger_type="ongoing")
        # 6. alpha — daily 16:00 持仓日报
        _ensure_todo(ALPHA_IID, content="每日 16:00 提交持仓日报（持仓 / 浮盈 / 操作记录）给 zero 和群",
                     trigger_type="condition",
                     condition="每个交易日 16:00 触发持仓日报发起")

        # ── Final summary ──
        print("\n=== task tree snapshot ===")
        for t in list_project_tasks(db):
            print(f"  [{t['status']:>10}] {t['type']:<22} {t['id']:<10} parent={t.get('parent_task_id') or '-'} owner={t.get('assignee_instance', '-')[:8]}")
        print(f"\n=== {PROJECT_ID} bootstrapped ===")
        return 0
    finally:
        try:
            db.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
