"""一次性迁移脚本：WORK.md 待办条目 → tasks 表。

只迁移 pending（待办）和 in_progress（进行中）的条目。
已完成（done）条目不迁（历史数据，留在 WORK.md 归档副本里）。

用法：
  python3 scripts/migrate_work_md_to_todos.py           # dry-run，只显示
  python3 scripts/migrate_work_md_to_todos.py --commit  # 正式写入
  python3 scripts/migrate_work_md_to_todos.py --instance <uuid>  # 指定实例

安全保证：
- 只 INSERT，不 DELETE 任何东西
- WORK.md 原文件重命名为 WORK.md.bak 保留
- 幂等：标题已存在则跳过（靠 _find_similar_task 去重）
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# 实例 UUID
INSTANCES = {
    "zero": "c2a5c8e8-e4f5-4c69-be3e-aac49903081d",
    "alpha": "5052c33a-e700-44dd-aea3-00e04a661ab1",
}

# WORK.md 条目解析（- [x?] 文本 | 来源:X | 创建:ISO | 优先级:Y [| 完成于:ISO]）
WORK_ITEM_RE = re.compile(
    r'^- \[(?P<done>[ x])\] (?P<text>.+?)(?: \| 来源:(?P<source>[^|]+))?'
    r'(?: \| 创建:(?P<created>[^|]+))?'
    r'(?: \| 优先级:(?P<priority>[^|]+?))?'
    r'(?: \| 完成于:(?P<completed>[^|]+))?$'
)

_PRIORITY_MAP = {"高": "high", "中": "medium", "低": "low"}


def parse_work_md(content: str) -> list[dict]:
    """解析 WORK.md 内容为条目列表。"""
    items = []
    current_section = "todo"
    for line in content.splitlines():
        line = line.strip()
        if "## 进行中" in line or "## 待办" in line:
            current_section = "active"
        elif "## 完成" in line:
            current_section = "done"
        elif line.startswith("- ["):
            m = WORK_ITEM_RE.match(line)
            if m:
                is_done = m.group("done") == "x"
                text = m.group("text").strip()
                priority = _PRIORITY_MAP.get(
                    (m.group("priority") or "中").strip(), "medium"
                )
                items.append({
                    "text": text,
                    "priority": priority,
                    "status": "done" if is_done else (
                        "in_progress" if current_section == "active" and not is_done
                        else "planned"
                    ),
                    "section": current_section,
                })
    return items


def migrate_instance(name: str, iid: str, commit: bool = False) -> int:
    """迁移单个实例的 WORK.md。返回迁移条目数。"""
    work_path = PROJECT_ROOT / "apps" / iid / "data" / "memories" / "WORK.md"
    if not work_path.exists():
        print(f"[{name}] WORK.md 不存在于 {work_path}")
        return 0

    content = work_path.read_text(encoding="utf-8")
    items = parse_work_md(content)

    # 只迁 pending 和 in_progress（done 的留作归档）
    active = [i for i in items if i["status"] in ("planned", "in_progress")]
    print(f"[{name}] 解析到 {len(items)} 条，其中活跃（待迁）{len(active)} 条：")
    for i in active:
        print(f"  [{i['status']:12}] {i['text'][:60]}")

    if not commit:
        print(f"[{name}] dry-run 模式，不写入。加 --commit 正式迁移。")
        return len(active)

    if not active:
        print(f"[{name}] 无活跃条目，跳过。")
        return 0

    # 设置实例上下文
    from infrastructure.config import set_current_instance_id
    set_current_instance_id(iid)
    from domain.todos.crud import create_task, _find_similar_task

    migrated = 0
    for item in active:
        # 去重：标题已存在则跳过
        dup = _find_similar_task(item["text"], "")
        if dup:
            print(f"  跳过（已存在相似）：{item['text'][:40]}")
            continue
        result = create_task(
            title=item["text"],
            priority=item["priority"],
            status=item["status"],
            source="migrated:WORK.md",
        )
        if result.get("ok"):
            migrated += 1
            print(f"  ✅ 创建：{result.get('task', {}).get('id')} {item['text'][:40]}")
        else:
            print(f"  ❌ 失败：{item['text'][:40]} → {result.get('reason')}")

    # 备份原 WORK.md
    backup = work_path.with_suffix(".md.bak")
    backup.write_text(content, encoding="utf-8")
    print(f"[{name}] 原文件备份到 {backup.name}")

    print(f"[{name}] 迁移完成：{migrated}/{len(active)} 条")
    return migrated


def main():
    commit = "--commit" in sys.argv
    instance_arg = None
    for i, arg in enumerate(sys.argv):
        if arg == "--instance" and i + 1 < len(sys.argv):
            instance_arg = sys.argv[i + 1]

    targets = {}
    if instance_arg:
        if instance_arg in INSTANCES:
            targets = {instance_arg: INSTANCES[instance_arg]}
        else:
            # 当作 uuid
            targets = {instance_arg: instance_arg}
    else:
        targets = INSTANCES

    total = 0
    for name, iid in targets.items():
        total += migrate_instance(name, iid, commit)
    print(f"\n总计迁移 {total} 条")


if __name__ == "__main__":
    main()
