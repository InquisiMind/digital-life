"""一次性迁移脚本：legacy .md 存储 → modern 结构化存储。

把"半退役"的老双写源搬到新家,让老 .md 真正下线(不让数据继续碎片化)。

迁移目标:
  HIM.md    → contacts.about   (自由文本画像)
  GOALS.md  → todos 表 type='goal', project_id=''  (个人 goal)
  PLANS.md  → 头 task(type='goal') + 多个 todo_plan milestone
  DAILY.md  →
    HH:MM 项  → 跳过(已在 timers 表双写)
    文字项    → todos 表 type='daily', deadline=当日

用法:
  python3 scripts/migrate_legacy_md_to_modern_store.py             # dry-run
  python3 scripts/migrate_legacy_md_to_modern_store.py --apply     # 真写
  python3 scripts/migrate_legacy_md_to_modern_store.py --instance <uuid>
  python3 scripts/migrate_legacy_md_to_modern_store.py --only him|goals|plans|daily

安全保证:
  - 只 INSERT 新行 + 写 .md.legacy 副本,绝不 DELETE 老 .md 内容
  - 幂等:第 2 次跑发现 .md.legacy 已存在 → 处理源 .md(若非空再处理)或 skip
  - 自动去重:create_task 内含 _find_similar_task;contacts about 仅当 about 为空才覆盖
  - 失败粒度:每个 .md 文件独立 try/except,一个失败不影响其他
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

INSTANCES = {
    "zero": "c2a5c8e8-e4f5-4c69-be3e-aac49903081d",
    "alpha": "5052c33a-e700-44dd-aea3-00e04a661ab1",
}

# DAILY.md 里 HH:MM 开头的项 → 已在 timers 双写,跳过(不强迁,只迁文字项)
DAILY_HHMM_RE = re.compile(r"^-\s*\[[ x]\]\s*\d{1,2}:\d{2}\s+")
DAILY_DATE_RE = re.compile(r"^##\s*(?P<date>\d{4}-\d{2}-\d{2})")
# DAILY.md/bullet 项格式: - [x] 内容 / - [ ] 内容 / - 内容
DAILY_BULLET_RE = re.compile(r"^-\s*(?:\[(?P<done>[ x])\]\s*)?(?P<text>.+?)\s*$")

# GOALS.md: ##  标题 + (可选) > 描述 / > 状态... / > 创建... / > 优先级...
GOAL_HEADING_RE = re.compile(r"^##\s+\S")  # 任意二级标题开头
GOAL_META_RE = re.compile(
    r"(?:状态[:：]\s*(?P<status>[^|]+?))?"
    r"(?:\s*\|\s*创建[:：]\s*(?P<created>[^|]+?))?"
    r"(?:\s*\|\s*优先级[:：]\s*(?P<priority>[^|]+?))?"
    r"\s*$"
)

# PLANS.md: ## goal 标题  → ### 里程碑 → bullet
PLAN_GOAL_RE = re.compile(r"^##\s+(?P<title>.+?)\s*$")
PLAN_MILESTONE_RE = re.compile(r"^###\s+(?:里程碑|milestones?|milestone)", re.IGNORECASE)
PLAN_BULLET_RE = re.compile(r"^-\s*\[(?P<done>[ x])\]\s*(?P<text>.+?)\s*$")

_PRIORITY_MAP = {"高": "high", "中": "medium", "低": "low", "high": "high",
                 "medium": "medium", "low": "low"}


def _backup_legacy(path: Path) -> Path | None:
    """源 .md → 改名为 .md.legacy(若已存在则加 .N 后缀)。

    返回 legacy 路径;若源文件已经被改名(不存在)则返 None,意味着早就迁过。
    """
    if not path.exists():
        return None
    legacy = path.with_suffix(".md.legacy")
    # 若 .md.legacy 已存在 → 这次跑是第 N 次,加序号
    n = 1
    while legacy.exists():
        legacy = path.with_suffix(f".md.legacy.{n}")
        n += 1
    path.rename(legacy)
    return legacy


def _set_instance_ctx(iid: str) -> None:
    from infrastructure.config import set_current_instance_id
    set_current_instance_id(iid)


def migrate_him(iid: str, *, apply: bool) -> tuple[int, int]:
    """HIM.md → contacts.about。返回 (成功条数, 总尝试数)。

    策略:HIM.md 整体当 about 写到(优先级)zhp 的 contact:
      1) 找 name 含 'zhp' / 'zhanghaopu' / '张浩普' / '蓝先生' 的 contact
      2) 找不到 → 找一个 kind=human 且有 platform_id 的"主联系人"
      3) 还找不到 → 不主动 create(无 platform_id 是 stub,会被 zhp 真消息过来时合并)
    """
    base = PROJECT_ROOT / "apps" / iid / "data" / "memories"
    him_path = base / "HIM.md"
    # 若源已重命名为 .legacy(上次跑过部分成功),自动从 .legacy 读
    legacy_path = base / "HIM.md.legacy"
    src_path = him_path if him_path.exists() else (legacy_path if legacy_path.exists() else None)
    if src_path is None:
        return (0, 0)
    content = src_path.read_text(encoding="utf-8").strip()
    if not content:
        return (0, 0)

    print(f"  [HIM] 源 {src_path.name} = {len(content)} chars")
    # only_backup: 只在 src 还是原 .md 时再 rename(避免 .legacy.legacy.N 链)
    def _only_backup_if_original():
        if src_path == him_path:
            _only_backup_if_original()

    if not apply:
        print(f"  [HIM] dry-run: would write to primary contact.about")
        return (0, 1)

    _set_instance_ctx(iid)
    from domain.contacts.store import list_contacts, update_contact

    contacts = list_contacts(include_blocked=False)
    # 优先级匹配关键字(name + notes)
    KEYWORDS = ("zhp", "zhanghaopu", "张浩普", "蓝先生", "zhang", "haopu",
                "浩普", "蓝")
    primary = None
    for c in contacts:
        name = (c.get("name") or "").lower()
        notes = (c.get("notes") or "").lower()
        # 中文也要小写比较(虽然 latinfolding/locale 不同但中文不受 lower 影响)
        cn_name = c.get("name") or ""
        cn_notes = c.get("notes") or ""
        for k in KEYWORDS:
            if k in name or k in notes or k in cn_name or k in cn_notes:
                primary = c
                break
        if primary:
            break
    if primary is None:
        # fallback: 找 kind=human 且 platform_ids 非空的
        for c in contacts:
            if c.get("kind") == "human" and c.get("platform_ids"):
                primary = c
                break
    if primary is None:
        print(f"  [HIM] 跳过:找不到合适的 contact 写入(留 HIM.md.legacy)")
        _only_backup_if_original()
        return (0, 1)

    existing_about = primary.get("about") or ""
    if existing_about and existing_about.strip() == content.strip():
        print(f"  [HIM] 内容已存在于 contact {primary['id'][:8]},跳过")
        _only_backup_if_original()
        return (1, 1)
    # 仅当 about 为空才写(避免覆盖更新);如要换内容,先手工清
    if existing_about:
        print(f"  [HIM] contact {primary['id'][:8]} 已有 about ({len(existing_about)} chars),"
              f"追加新内容")
        new_about = existing_about.rstrip() + "\n\n" + "--- 迁移自 HIM.md ---\n\n" + content
    else:
        new_about = content

    updated = update_contact(primary["id"], about=new_about)
    if updated:
        print(f"  [HIM] ✅ 写入 contact {primary['id'][:8]} ({primary.get('name')}), "
              f"about 现在 {len(updated.get('about') or '')} chars")
        _only_backup_if_original()
        return (1, 1)
    print(f"  [HIM] ❌ update_contact 失败")
    return (0, 1)


def migrate_goals(iid: str, *, apply: bool) -> tuple[int, int]:
    """GOALS.md → todos 表 type='goal'。

    每个 `## 标题` → create_task(type='goal', title=标题, detail=description),
    description 来自随后的 `>` 行。
    """
    base = PROJECT_ROOT / "apps" / iid / "data" / "memories"
    goals_path = base / "GOALS.md"
    legacy_path = base / "GOALS.md.legacy"
    src_path = goals_path if goals_path.exists() else (legacy_path if legacy_path.exists() else None)
    if src_path is None:
        return (0, 0)
    lines = src_path.read_text(encoding="utf-8").splitlines()

    # 解析
    goals: list[dict] = []
    cur: dict | None = None
    for line in lines:
        if GOAL_HEADING_RE.match(line):
            if cur:
                goals.append(cur)
            title = line.lstrip("#").strip()
            # 去 emoji 前缀
            title = re.sub(r"^[^\w\u4e00-\u9fff]+\s*", "", title)
            cur = {"title": title, "detail": "", "priority": "medium"}
        elif cur is not None:
            s = line.strip()
            if s.startswith(">"):
                cur["detail"] += s.lstrip(">").strip() + " "
            elif s and not s.startswith("#"):
                # 正文里的 metadata 行
                m = GOAL_META_RE.match(s)
                if m and (m.group("priority") or m.group("status") or m.group("created")):
                    p = m.group("priority")
                    if p:
                        cur["priority"] = _PRIORITY_MAP.get(p.strip(), "medium")
    if cur:
        goals.append(cur)
    goals = [g for g in goals if g["title"]]

    print(f"  [GOALS] 解析到 {len(goals)} 个目标")
    for g in goals:
        print(f"    [{g['priority']}] {g['title'][:50]}")

    if not apply:
        return (0, len(goals))

    _set_instance_ctx(iid)
    from domain.todos.crud import create_task

    migrated = 0
    for g in goals:
        result = create_task(
            title=g["title"],
            detail=g["detail"].strip(),
            priority=g["priority"],
            type="goal",
            project_id="",
            status="planned",
            source="migrated:GOALS.md",
        )
        if result.get("ok"):
            migrated += 1
            print(f"    ✅ {result['task']['id']} {g['title'][:40]}")
        else:
            print(f"    skip: {result.get('reason', 'unknown')[:60]}")

    # 即使全部 skip(去重命中), 也算迁移已完成——不让重复跑卡住。
    # 触发 backup 只在 src 还是 HIM.md(没 rename 过)时, 避免 .legacy.legacy.1 链。
    if src_path == goals_path:
        _backup_legacy(goals_path)
    return (migrated, len(goals))


def migrate_plans(iid: str, *, apply: bool) -> tuple[int, int]:
    """PLANS.md → 每个 `## title` 一个 goal task + milestones 是 todo_plan。

    结构: ## 标题 / ### 里程碑 / - [ ] bullet
    """
    base = PROJECT_ROOT / "apps" / iid / "data" / "memories"
    plans_path = base / "PLANS.md"
    legacy_path = base / "PLANS.md.legacy"
    src_path = plans_path if plans_path.exists() else (legacy_path if legacy_path.exists() else None)
    if src_path is None:
        return (0, 0)
    lines = src_path.read_text(encoding="utf-8").splitlines()

    plans: list[dict] = []
    cur: dict | None = None
    in_milestone = False
    for line in lines:
        m_goal = PLAN_GOAL_RE.match(line)
        if m_goal:
            if cur:
                plans.append(cur)
            title = m_goal.group("title").strip()
            title = re.sub(r"^[^\w\u4e00-\u9fff]+\s*", "", title)
            cur = {"title": title, "milestones": []}
            in_milestone = False
            continue
        if cur is None:
            continue
        if PLAN_MILESTONE_RE.match(line):
            in_milestone = True
            continue
        if in_milestone:
            m = PLAN_BULLET_RE.match(line)
            if m:
                cur["milestones"].append({
                    "text": m.group("text").strip(),
                    "done": m.group("done") == "x",
                })
    if cur:
        plans.append(cur)
    plans = [p for p in plans if p["title"]]

    n_milestones = sum(len(p["milestones"]) for p in plans)
    print(f"  [PLANS] 解析到 {len(plans)} 个目标 + {n_milestones} 个里程碑")
    for p in plans:
        print(f"    {p['title'][:50]} ({len(p['milestones'])} milestones)")
        for ms in p["milestones"][:3]:
            tag = "✓" if ms["done"] else " "
            print(f"      [{tag}] {ms['text'][:50]}")

    if not apply:
        return (0, len(plans))

    _set_instance_ctx(iid)
    # 用 find 防 dup;但 create_task 已经内部去重。先建 goal task,再建 milestones。
    from domain.todos.crud import (create_task, create_plan, complete_plan,
                                    list_plans, _find_similar_task)

    migrated = 0
    for p in plans:
        # 先查 title 是否已存在(可能上一次跑过一遍)
        dup = _find_similar_task(p["title"], "")
        if dup and getattr(dup, "type", "") == "goal":
            tid = dup.id
            print(f"    reuse existing goal task {tid}")
        else:
            result = create_task(
                title=p["title"],
                type="goal",
                project_id="",
                status="planned",
                source="migrated:PLANS.md",
            )
            if not result.get("ok"):
                print(f"    skip goal {p['title'][:40]}: {result.get('reason','')[:60]}")
                continue
            tid = result["task"]["id"]
            print(f"    ✅ goal {tid} {p['title'][:40]}")
        migrated += 1
        # milestones (幂等:同 task + 同 content 已存在则跳过;resume modified 状态)
        for ms in p["milestones"]:
            existing = [pl for pl in list_plans(tid)
                        if (pl.get("content") or "") == ms["text"]]
            if existing:
                if ms["done"] and existing[0].get("status") != "done":
                    complete_plan(existing[0]["id"])
                continue
            try:
                plan_result = create_plan(tid, ms["text"])
                if ms["done"] and plan_result.get("ok") and plan_result.get("plan_id"):
                    complete_plan(plan_result["plan_id"])
            except Exception as e:
                print(f"      ! milestone insert fail: {e}")

    if src_path == plans_path:
        _backup_legacy(plans_path)
    return (migrated, len(plans))


def migrate_daily(iid: str, *, apply: bool) -> tuple[int, int]:
    """DAILY.md → todos 表 type='daily'(只迁文字项,跳过 HH:MM)。

    对每个 `## YYYY-MM-DD 周X` 段下的:`- [ ] 文字` / `- [x] 文字` / `- 文字` →
    建 task(type='daily', deadline=YYYY-MM-DD, status=done/planned)
    跳过 HH:MM 项(已在 timers 双写,plan 里也重复)
    """
    base = PROJECT_ROOT / "apps" / iid / "data" / "memories"
    daily_path = base / "DAILY.md"
    legacy_path = base / "DAILY.md.legacy"
    src_path = daily_path if daily_path.exists() else (legacy_path if legacy_path.exists() else None)
    if src_path is None:
        return (0, 0)
    lines = src_path.read_text(encoding="utf-8").splitlines()

    items: list[dict] = []
    cur_date: str | None = None
    for line in lines:
        m_date = DAILY_DATE_RE.match(line.strip())
        if m_date:
            cur_date = m_date.group("date")
            continue
        if cur_date is None:
            continue
        if DAILY_HHMM_RE.match(line):
            continue  # timers 自己管
        m = DAILY_BULLET_RE.match(line.strip())
        if not m:
            continue
        text = m.group("text").strip()
        if not text:
            continue
        items.append({
            "title": text,
            "date": cur_date,
            "done": m.group("done") == "x",
        })

    print(f"  [DAILY] 解析到 {len(items)} 个文字项(已跳过 HH:MM 项)")
    for it in items[:10]:
        tag = "✓" if it["done"] else " "
        print(f"    [{it['date']}][{tag}] {it['title'][:50]}")
    if len(items) > 10:
        print(f"    ... +{len(items)-10} more")

    if not apply:
        return (0, len(items))

    _set_instance_ctx(iid)
    from domain.todos.crud import create_task, list_tasks

    migrated = 0
    for it in items:
        # 幂等:同 deadline + 同 title 已存在则 skip(_find_similar_task 不够直接,手查)
        existing = [t for t in list_tasks()
                    if t.get("type") == "daily"
                    and (t.get("deadline") or "") == it["date"]
                    and (t.get("title") or "") == it["title"]]
        if existing:
            continue
        result = create_task(
            title=it["title"],
            type="daily",
            deadline=it["date"],
            status="done" if it["done"] else "planned",
            project_id="",
            source="migrated:DAILY.md",
        )
        if result.get("ok"):
            migrated += 1
        else:
            print(f"    skip: {result.get('reason','')[:60]}")

    if src_path == daily_path:
        _backup_legacy(daily_path)
    return (migrated, len(items))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真写(默认 dry-run)")
    ap.add_argument("--instance", default=None,
                    help="实例 name(zero/alpha)或 uuid;不传就跑所有")
    ap.add_argument("--only", choices=["him", "goals", "plans", "daily"], default=None,
                    help="只跑某一项迁移")
    args = ap.parse_args()

    if args.instance:
        if args.instance in INSTANCES:
            targets = {args.instance: INSTANCES[args.instance]}
        else:
            targets = {args.instance: args.instance}
    else:
        targets = dict(INSTANCES)

    runners = {"him": migrate_him, "goals": migrate_goals,
               "plans": migrate_plans, "daily": migrate_daily}
    if args.only:
        runners = {args.only: runners[args.only]}

    print(f"=== mode: {'APPLY' if args.apply else 'DRY-RUN'} | instances: {list(targets)} ===\n")
    grand = {k: [0, 0] for k in runners}
    skipped_instances = []
    for name, iid in targets.items():
        base = PROJECT_ROOT / "apps" / iid
        if not base.exists():
            print(f"[{name}] instance dir 不存在: {base}, skip")
            skipped_instances.append(name)
            continue
        print(f"[{name}] ({iid[:8]} ...)")
        for kind, fn in runners.items():
            try:
                ok, total = fn(iid, apply=args.apply)
                grand[kind][0] += ok
                grand[kind][1] += total
            except Exception as e:
                import traceback
                print(f"  [{kind}] ❌ 未捕获错误: {e}")
                traceback.print_exc()
        print()

    print("=== 汇总 ===")
    for k, (ok, total) in grand.items():
        print(f"  {k:7s}: {ok}/{total}")
    if not args.apply:
        print("\n(dry-run;加 --apply 实际迁移)")
    if skipped_instances:
        print(f"\nskipped instances (dir missing): {skipped_instances}")


if __name__ == "__main__":
    main()
