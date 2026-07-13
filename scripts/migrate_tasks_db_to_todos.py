"""一次性迁移：tasks.db → todos.db + 表名统一。

对每个实例的 apps/{iid}/data/tasks/tasks.db 做：
1. ALTER TABLE RENAME（旧表名 → 新表名）
2. 文件改名 tasks/tasks.db → todos/todos.db（目录改名）
3. 旧 workspace 子目录移动到新目录下

用法：
  python3 scripts/migrate_tasks_db_to_todos.py           # dry-run
  python3 scripts/migrate_tasks_db_to_todos.py --commit  # 正式执行
"""
from __future__ import annotations

import sqlite3
import sys
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

INSTANCES = {
    "zero": "c2a5c8e8-e4f5-4c69-be3e-aac49903081d",
    "alpha": "5052c33a-e700-44dd-aea3-00e04a661ab1",
}

TABLE_RENAMES = [
    ("tasks", "todos"),
    ("task_plans", "todo_plans"),
    ("task_notes", "todo_notes"),
    ("task_sessions", "todo_sessions"),
    ("task_todos", "todo_triggers"),
]


def migrate_instance(name: str, iid: str, commit: bool = False) -> bool:
    data_dir = PROJECT_ROOT / "apps" / iid / "data"
    old_dir = data_dir / "tasks"
    new_dir = data_dir / "todos"
    old_db = old_dir / "tasks.db"
    new_db = new_dir / "todos.db"

    if not old_db.exists():
        print(f"[{name}] {old_db} 不存在，跳过")
        return True

    # 检查是否已经迁移过
    if new_db.exists():
        print(f"[{name}] {new_db} 已存在，可能已迁移过，跳过")
        return True

    conn = sqlite3.connect(str(old_db))
    conn.row_factory = sqlite3.Row
    # 看当前表
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    print(f"[{name}] 当前表: {tables}")

    if commit:
        # 1. ALTER TABLE RENAME
        for old_name, new_name in TABLE_RENAMES:
            if old_name in tables and new_name not in tables:
                conn.execute(f"ALTER TABLE {old_name} RENAME TO {new_name}")
                print(f"  RENAME TABLE {old_name} → {new_name}")
            elif new_name in tables:
                print(f"  {new_name} 已存在，跳过")
            else:
                print(f"  {old_name} 不存在，跳过")

        # 重建索引（旧索引指向旧表名）
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'").fetchall():
            conn.execute(f"DROP INDEX IF EXISTS {r[0]}")

        conn.commit()
        conn.close()

        # 2. 目录改名：tasks/ → todos/
        new_dir.mkdir(parents=True, exist_ok=True)
        # 移动 DB 文件
        shutil.copy2(str(old_db), str(new_db))
        # 移动 workspace 子目录
        for item in old_dir.iterdir():
            if item.name == "tasks.db":
                continue
            dest = new_dir / item.name
            if not dest.exists():
                shutil.move(str(item), str(dest))
        # 删旧 DB（已复制到新位置）
        old_db.unlink()
        # 如果旧目录空了就删
        try:
            old_dir.rmdir()
        except OSError:
            pass  # 目录非空（可能有 __pycache__ 等），留着

        print(f"[{name}] ✅ 迁移完成: {new_db}")
    else:
        conn.close()
        print(f"[{name}] dry-run，不执行。加 --commit 正式迁移。")
        # 预览
        data_count = {}
        for old_name, _ in TABLE_RENAMES:
            if old_name in tables:
                c = sqlite3.connect(str(old_db)).execute(f"SELECT count(*) FROM {old_name}").fetchone()[0]
                data_count[old_name] = c
        if data_count:
            print(f"  数据量: {data_count}")

    return True


def main():
    commit = "--commit" in sys.argv
    targets = INSTANCES
    if "--instance" in sys.argv:
        idx = sys.argv.index("--instance")
        if idx + 1 < len(sys.argv):
            iid = sys.argv[idx + 1]
            targets = {iid: iid}

    for name, iid in targets.items():
        migrate_instance(name, iid, commit)
    print("\n完成")


if __name__ == "__main__":
    main()
