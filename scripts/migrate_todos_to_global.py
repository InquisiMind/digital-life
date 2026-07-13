#!/usr/bin/env python3
"""把零散在 apps/{iid}/data/todos/todos.db 和 projects/{pid}/data/todos.db
的 todos 数据迁移到统一的 data/global_todos.db。

设计文档 6.2/22.5 重写后：待办是独立 entity，关联关系是属性。所有 todos
应该存在一个全局 DB，通过 project_id / assignee_instance 等列表达
"挂哪个项目"、"分给谁"。

迁移逻辑：
  1. 源A：apps/{iid}/data/todos/todos.db 的 todos 表（含 source='personal'
     和 source='project:X' 两种）
     → import 到 global_todos.db.todos
     - source='personal' → project_id='', assignee_instance={iid}
     - source='project:X' → project_id='X', assignee_instance={iid}
       （apps/ 里的 todo 默认是创建实例拥有，除非老 mirror 显式给了别人）
  2. 源B：projects/{pid}/data/todos.db 的 project_todos 表
     → import 到 global_todos.db.todos
     - project_id={pid}, assignee_instance={row.assignee_instance}
     - origin_instance='' (这个 todo 不是实例创建的，是项目模板建出来的)

迁移后被吸收的源库保持不动（保留作 backup）。

跑前要求：服务停止（避免并发写入 global_todos.db）。

用法：
    python3 scripts/migrate_todos_to_global.py
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


repo_root = Path(__file__).resolve().parents[1]
GLOBAL_DB = repo_root / "data" / "global_todos.db"


def _read_source_todos(db_path: Path, instance_id: str):
    """读一个实例 todos.db 的所有 todos 返回 [(id, ..., origin_instance=instance_id)]。"""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM todos").fetchall()
    conn.close()
    return [dict(r) for r in rows], instance_id


def _read_source_project_todos(db_path: Path, project_id: str):
    """读一个项目 todos.db.project_todos（迁移后已 rename）的 todos。"""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    # 检测表是 project_todos 还是 project_tasks（老库没迁移）
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    table = "project_todos" if "project_todos" in tables else "project_tasks"
    parent_col = "parent_todo_id" if table == "project_todos" else "parent_task_id"
    rows = conn.execute(
        f"SELECT id, {parent_col} as parent_id, title, description, status, "
        f"priority, assignee_instance, assignee_kind, type, linked_deliverable_id, "
        f"sort_order, created_at, updated_at FROM {table}"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows], project_id


def _migrate_instance_todos(gdb, rows, instance_id):
    """把实例 todos.db 的所有 todos 吸收到 global。"""
    cnt = 0
    for r in rows:
        # 把 source 列映射到 project_id：
        #   'personal' or empty → ''
        #   'project:X' → X
        source = (r.get("source") or "").strip()
        project_id = ""
        if source.startswith("project:"):
            project_id = source.split(":", 1)[1]
        # 写入
        existing = gdb.execute(
            "SELECT id FROM todos WHERE id = ?", (r["id"],),
        ).fetchone()
        if existing:
            continue
        gdb.execute(
            "INSERT INTO todos (id, title, description, status, priority, "
            "deadline, tags, project_id, assignee_instance, assignee_kind, "
            "parent_id, linked_deliverable_id, type, has_workspace, "
            "source, origin_instance, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                r["id"], r["title"], r.get("description", ""),
                r.get("status", "idea"), r.get("priority", "medium"),
                r.get("deadline"), r.get("tags", "[]"),
                project_id, instance_id, "instance",
                r.get("parent_id", ""), r.get("linked_deliverable_id", ""),
                r.get("type", ""), r.get("has_workspace", 0),
                source, instance_id,
                r.get("created_at"), r.get("updated_at"),
            ),
        )
        cnt += 1
    return cnt


def _migrate_project_todos(gdb, rows, project_id):
    """把项目 project_todos 表的数据吸收到 global todos。"""
    cnt = 0
    for r in rows:
        existing = gdb.execute(
            "SELECT id FROM todos WHERE id = ?", (r["id"],),
        ).fetchone()
        if existing:
            continue
        gdb.execute(
            "INSERT INTO todos (id, title, description, status, priority, "
            "deadline, tags, project_id, assignee_instance, assignee_kind, "
            "parent_id, linked_deliverable_id, type, has_workspace, "
            "source, origin_instance, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                r["id"], r["title"], r.get("description", ""),
                r.get("status", "planned"), r.get("priority", "medium"),
                "", "[]",
                project_id,
                r.get("assignee_instance", "") or "",
                r.get("assignee_kind", "") or "",
                r.get("parent_id", "") or "",
                r.get("linked_deliverable_id", "") or "",
                r.get("type", "") or "",
                1 if r.get("type") else 0,
                f"project:{project_id}", "",
                r.get("created_at"), r.get("updated_at"),
            ),
        )
        cnt += 1
    return cnt


def _migrate_subtables(gdb, src_path: Path):
    """把 todo_plans / todo_notes / todo_sessions / todo_triggers 也吸收（id 去重）。"""
    for sub in ("todo_plans", "todo_notes", "todo_sessions", "todo_triggers"):
        try:
            src = sqlite3.connect(str(src_path))
            src.row_factory = sqlite3.Row
            rows = src.execute(f"SELECT * FROM {sub}").fetchall()
            src.close()
        except Exception:
            continue
        cols_in_global = [r[1] for r in gdb.execute(f"PRAGMA table_info({sub})").fetchall()]
        cnt = 0
        for r in rows:
            d = dict(r)
            # 没主键的（todo_plans/notes/sessions/triggers 有 INTEGER PRIMARY KEY AUTOINCREMENT）
            # → 直接按内容 INSERT；已存在的相同的跳过（task_id + content + created_at）
            cols = [c for c in d.keys() if c in cols_in_global]
            if not cols:
                continue
            # 去重条件：同一个 task_id + 同样创建时间 → 视为相同（已经吸收）
            existing = gdb.execute(
                f"SELECT 1 FROM {sub} WHERE task_id=? AND created_at=?",
                (d.get("task_id"), d.get("created_at")),
            ).fetchone() if "task_id" in cols and "created_at" in cols else None
            if existing:
                continue
            placeholders = ", ".join("?" for _ in cols)
            col_str = ", ".join(cols)
            # 不带 id 的 INSERT (autoincrement 重写)
            cols_no_id = [c for c in cols if c != "id"]
            placeholders = ", ".join("?" for _ in cols_no_id)
            col_str = ", ".join(cols_no_id)
            try:
                gdb.execute(
                    f"INSERT INTO {sub} ({col_str}) VALUES ({placeholders})",
                    [d.get(c) for c in cols_no_id],
                )
                cnt += 1
            except Exception:
                pass
        print(f"  {sub}: {cnt} rows absorbed")


def main():
    print(f"Global DB: {GLOBAL_DB}")
    if not GLOBAL_DB.exists():
        # 自动创建（schema 由 infrastructure.persistence.global_todos 管）
        from infrastructure.persistence.global_todos import get_global_todos_db
        get_global_todos_db().close()
        print("  created schema")

    gdb = sqlite3.connect(str(GLOBAL_DB))
    gdb.row_factory = sqlite3.Row
    gdb.execute("PRAGMA journal_mode=WAL")

    # 源A: apps/{iid}/data/todos/todos.db
    apps_dir = repo_root / "apps"
    total_a = 0
    for inst_dir in apps_dir.iterdir():
        if not inst_dir.is_dir() or inst_dir.name.startswith("."):
            continue
        iid = inst_dir.name
        todos_db = inst_dir / "data" / "todos" / "todos.db"
        if not todos_db.exists():
            continue
        rows, _ = _read_source_todos(todos_db, iid)
        cnt = _migrate_instance_todos(gdb, rows, iid)
        total_a += cnt
        print(f"  apps/{iid[:8]}: {cnt} todos absorbed (源 {len(rows)} 行)")
        # 子表（plans/notes/sessions/triggers）
        _migrate_subtables(gdb, todos_db)

    print(f"→ 实例源吸收总: {total_a} todos")

    # 源B: projects/{pid}/data/todos.db.project_todos
    projects_dir = repo_root / "projects"
    total_b = 0
    for proj_dir in projects_dir.iterdir():
        if not proj_dir.is_dir() or proj_dir.name.startswith("."):
            continue
        pid = proj_dir.name
        proj_db = proj_dir / "data" / "todos.db"
        if not proj_db.exists():
            continue
        rows, _ = _read_source_project_todos(proj_db, pid)
        cnt = _migrate_project_todos(gdb, rows, pid)
        total_b += cnt
        print(f"  projects/{pid}: {cnt} project todos absorbed (源 {len(rows)} 行)")

    print(f"→ 项目源吸收总: {total_b} todos")
    gdb.commit()
    gdb.close()

    # Final 状态
    gdb = sqlite3.connect(str(GLOBAL_DB))
    gdb.row_factory = sqlite3.Row
    rows = gdb.execute(
        "SELECT COUNT(*) as n, "
        "SUM(CASE WHEN project_id != '' THEN 1 ELSE 0 END) as project_count, "
        "SUM(CASE WHEN assignee_instance != '' THEN 1 ELSE 0 END) as assigned_count "
        "FROM todos"
    ).fetchone()
    print(f"\nFinal global_todos.db.todos: {rows['n']} rows "
          f"(项目关联 {rows['project_count']}, 有 assignee {rows['assigned_count']})")
    gdb.close()


if __name__ == "__main__":
    main()
