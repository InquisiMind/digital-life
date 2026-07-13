#!/usr/bin/env python3
"""Phase 4 deliverables → global_todos 一次性迁移脚本。

把每个 projects/<pid>/data/todos.db 的 deliverables 行迁到 data/global_todos.db.todos。
to id 复用原 deliverable id,linked_deliverable_id 指向自身。

幂等:已存在的(同 linked_deliverable_id)不会重复 INSERT,只 UPDATE 缺失字段。

策略:
- 已有对应 todo(linked_deliverable_id = did) → UPDATE 补 assignee_position/project_id
- 没有对应 todo → INSERT 新行,id 复用原 did
- 孤儿(project_id=pid 但 linked_deliverable_id 为空 / 指向不存在的 deliverable)→ log,不处理
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

REPO = Path("/Users/zhanghaopu/Documents/项目材料/探索项目/数字生命")


def migrate_one_project(pid: str, global_db: sqlite3.Connection) -> tuple[int, int, int]:
    """迁移单项目的 deliverables→todos。返回 (inserted, updated, orphans)。"""
    src_db_path = REPO / "projects" / pid / "data" / "todos.db"
    if not src_db_path.exists():
        return (0, 0, 0)

    conn = sqlite3.connect(str(src_db_path))
    conn.row_factory = sqlite3.Row
    try:
        delivs = conn.execute(
            "SELECT id, title, description, status, priority, "
            "assignee_instance, assignee_position, created_at, updated_at "
            "FROM deliverables"
        ).fetchall()
    except sqlite3.Error as e:
        print(f"  [skip] {pid} deliverables 表读取失败: {e}")
        conn.close()
        return (0, 0, 0)
    conn.close()

    inserted = 0
    updated = 0

    # 收集存在的 deliverable id,用于孤儿检测
    known_dids = {d["id"] for d in delivs}

    # global todos 里这项目所有 todo — 用于孤儿检测
    orphans = 0
    existing = global_db.execute(
        "SELECT id, linked_deliverable_id, assignee_position, project_id FROM todos WHERE project_id = ?",
        (pid,),
    ).fetchall()
    existing_map = {row["linked_deliverable_id"] or "": dict(row) for row in existing}

    for d in delivs:
        did = d["id"]
        existing_row = existing_map.get(did)

        if existing_row:
            # 已有对应 todo → UPDATE 补字段(assignee_position 之前的迁移可能没填)
            update_fields = {}
            if not existing_row.get("assignee_position") and d["assignee_position"]:
                update_fields["assignee_position"] = d["assignee_position"]
            if update_fields:
                set_clause = ", ".join(f"{k} = ?" for k in update_fields)
                values = list(update_fields.values())
                # ⚠ 关键修复:WHERE 只用 linked_deliverable_id 匹配(不再加 id=?
                # 因为 todo.id 跟 deliverable.id 不一样——todo.id 是新 UUID,
                # linked_deliverable_id 才指向 deliverable 原 id)。
                global_db.execute(
                    f"UPDATE todos SET {set_clause} WHERE linked_deliverable_id = ?",
                    values + [did],
                )
                updated += 1
                print(f"  [update] todo={existing_row['id'][:8]} (deliv={did[:8]}) assignee_position=+{update_fields.get('assignee_position', '')[:10]}")
            else:
                print(f"  [skip] todo={existing_row['id'][:8]} (deliv={did[:8]}) 字段齐全,跳过")
        else:
            # 没有对应 todo → INSERT 新行
            assignee_instance = d["assignee_instance"] or ""
            assignee_kind = "instance" if assignee_instance else ""
            global_db.execute(
                "INSERT INTO todos "
                "(id, title, description, status, priority, deadline, tags, "
                "project_id, assignee_instance, assignee_kind, assignee_position, parent_id, "
                "linked_deliverable_id, type, has_workspace, source, origin_instance, "
                "acceptance_criteria, detail, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, NULL, '[]', ?, ?, ?, ?, '', ?, '', 0, ?, '', '', '', ?, ?)",
                (
                    did,
                    d["title"] or "(no title)",
                    d["description"] or "",
                    d["status"] or "planned",
                    d["priority"] or "medium",
                    pid,
                    assignee_instance,
                    assignee_kind,
                    d["assignee_position"] or "",
                    did,
                    f"project:{pid}",
                    d["created_at"] or "",
                    d["updated_at"] or "",
                ),
            )
            inserted += 1
            print(f"  [insert] todo={did[:8]} (自身) 项目={pid} 标题={d['title'][:40]}")

    # 孤儿:project_id=pid 但 linked_deliverable_id 空 或 指向不存在的 did
    for row in existing:
        linked = row["linked_deliverable_id"] or ""
        if not linked or linked not in known_dids:
            orphans += 1
            print(f"  [orphan] todo={row['id'][:8]} project_id={pid} linked={linked[:8] or '(none)'} — 保留人工决定")

    return (inserted, updated, orphans)


def main():
    print("=" * 70)
    print("Phase 4 迁移:deliverables → global_todos.db")
    print("=" * 70)

    # 实例 context — 用 zero(它是 manager)
    import os
    os.environ["DIGITAL_LIFE_INSTANCE_ID"] = "c2a5c8e8-e4f5-4c69-be3e-aac49903081d"

    sys.path.insert(0, str(REPO))
    from infrastructure.persistence.global_todos import get_global_todos_db

    global_db = get_global_todos_db()
    global_db.row_factory = sqlite3.Row

    projects_dir = REPO / "projects"
    candidates = [p.name for p in sorted(projects_dir.iterdir()) if p.is_dir() and (p / "data" / "todos.db").exists()]

    total_inserted = 0
    total_updated = 0
    total_orphans = 0

    for pid in candidates:
        print(f"\n--- 项目 {pid} ---")
        i, u, o = migrate_one_project(pid, global_db)
        total_inserted += i
        total_updated += u
        total_orphans += o

    global_db.commit()

    # 验证
    print("\n" + "=" * 70)
    print("迁移汇总")
    print("=" * 70)
    print(f"INSERT 新 todo: {total_inserted}")
    print(f"UPDATE 补字段:  {total_updated}")
    print(f"孤儿 todo 待人工决定: {total_orphans}")

    print("\n=== 最终验证:各项目 todos 总数 ===")
    for row in global_db.execute(
        "SELECT project_id, count(*) AS n, "
        "count(CASE WHEN linked_deliverable_id != '' AND linked_deliverable_id IS NOT NULL THEN 1 END) AS deliv_n "
        "FROM todos WHERE project_id != '' GROUP BY project_id ORDER BY project_id"
    ):
        print(f"  {row['project_id']:30} | {row['n']:3} 行 | 其中 deliverable 类: {row['deliv_n']}")

    global_db.close()
    print("\n迁移完成。")


if __name__ == "__main__":
    main()
