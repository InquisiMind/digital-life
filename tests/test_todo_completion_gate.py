"""完成门禁语义测试（设计文档 6.6 重写后）。

待办只有一道硬门禁：必须写过 task_note（写明结果/阻塞原因/验收说明）。
仅此一条。不再有「session 内必须调 terminal」或「session 内必须向人汇报」
这种形似门禁实则虚设、还导致死循环的硬约束。

验证：
- 任务没 notes → done 拒绝
- 任务有 notes → done 通过（不管 session 是否调过 terminal / 是否向人汇报过）
- require_execution_evidence / require_human_reply 参数不再被接受（彻底砍）
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch
from datetime import datetime, timezone, timedelta


def _setup_isolated_db(tmp_path: Path):
    """在 tmp_path 下创建隔离的 todos.db，含测试任务 + notes + sessions。"""
    db_path = tmp_path / "todos.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS todos (
            id TEXT PRIMARY KEY, title TEXT, description TEXT,
            status TEXT DEFAULT 'idea', priority TEXT DEFAULT 'medium',
            deadline TEXT, tags TEXT DEFAULT '[]',
            source TEXT DEFAULT 'personal', linked_deliverable_id TEXT,
            type TEXT DEFAULT '', parent_id TEXT DEFAULT '',
            has_workspace INTEGER DEFAULT 0,
            created_at TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS todo_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
            content TEXT, deadline TEXT, status TEXT DEFAULT 'pending',
            order_num INTEGER DEFAULT 0, created_at TEXT, completed_at TEXT,
            FOREIGN KEY (task_id) REFERENCES todos(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS todo_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
            content TEXT, created_at TEXT,
            FOREIGN KEY (task_id) REFERENCES todos(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS todo_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
            session_id TEXT, digest TEXT, started_at TEXT, ended_at TEXT,
            FOREIGN KEY (task_id) REFERENCES todos(id) ON DELETE CASCADE
        );
    """)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO todos (id, title, status, created_at, updated_at) VALUES (?, ?, 'in_progress', ?, ?)",
        ("t1", "测试任务", now, now),
    )
    conn.execute(
        "INSERT INTO todos (id, title, status, created_at, updated_at) VALUES (?, ?, 'in_progress', ?, ?)",
        ("t-empty-notes", "未写笔记的任务", now, now),
    )
    conn.execute(
        "INSERT INTO todo_notes (task_id, content, created_at) VALUES (?, ?, ?)",
        ("t1", "## 验收说明\n任务实质完成，所有步骤 done。", now),
    )
    conn.commit()
    conn.close()
    return db_path


def _with_isolated_db(tmp_path: Path):
    """Patch domain.todos.crud.get_db 让它使用 tmp_path 下的隔离 DB。

    crud 用 `from ._infra import get_db`（导入时绑定），所以必须 patch crud 自身
    的符号，patch _infra.get_db 不生效。
    """
    db_path = _setup_isolated_db(tmp_path)
    import domain.todos.crud as crud
    import domain.todos.session_tracking as session_tracking

    def _fake_get_db():
        c = sqlite3.connect(str(db_path))
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        return c

    # crud 已 `from ._infra import get_db`，所以 patch crud.get_db；
    # session_tracking 走 crud.get_task / crud.read_notes，不直接持 get_db。
    return patch.object(crud, "get_db", _fake_get_db)


def test_completion_blocked_when_no_notes(tmp_path):
    """任务未写过 task_note → done 必须被拒绝。
    这是唯一保留的硬门禁：防止自评自夸（设计文档 6.6）。
    """
    with _with_isolated_db(tmp_path):
        from domain.todos.session_tracking import completion_ready

        # 任务 t-empty-notes 没笔记 → 不应通过
        ok, reason = completion_ready("t-empty-notes", session_id="any-session-id")
        assert ok is False
        assert "task_note" in reason or "笔记" in reason, f"reject reason 应说明必须写笔记，实际：{reason}"


def test_completion_passes_with_notes_even_without_session_evidence(tmp_path):
    """任务有笔记 → done 必须通过，即使 session 里从未调过 terminal / 从未向人汇报。

    这是新增产品语义——原先的 require_execution_evidence / require_human_reply
    已被彻底砍掉（设计文档 6.6 重写）。理由：
      1. 任务经常跨多 session 完成
      2. 很多任务本来就不需要执行类工具
      3. 形似门禁实则虚设（瞎调 pwd 就过）
      4. 历史 bug：导致死循环（alpha 盯盘 6-13 案例）
    """
    with _with_isolated_db(tmp_path):
        from domain.todos.session_tracking import completion_ready

        # session_id 指向一个全新空的 session（什么都没做）
        ok, reason = completion_ready("t1", session_id="fresh-empty-session")
        assert ok is True, f"有笔记就该通过，实际拒绝：{reason}"


def test_completion_ready_signature_no_longer_accepts_require_kwargs(tmp_path):
    """completion_ready 的 require_execution_evidence / require_human_reply 参数
    应已彻底砍掉——任何调用方传这两个 kwarg 都该报 TypeError，避免再次复活。
    """
    with _with_isolated_db(tmp_path):
        from domain.todos.session_tracking import completion_ready

        # 调用方若误传旧参数 → TypeError（防止死灰复燃）
        try:
            completion_ready("t1", require_execution_evidence=True)
            raise AssertionError("completion_ready 不应再接受 require_execution_evidence 参数")
        except TypeError:
            pass

        try:
            completion_ready("t1", require_human_reply=True)
            raise AssertionError("completion_ready 不应再接受 require_human_reply 参数")
        except TypeError:
            pass


def test_completion_unknown_task(tmp_path):
    """不存在的 task → done 直接拒绝，返回不存在。"""
    with _with_isolated_db(tmp_path):
        from domain.todos.session_tracking import completion_ready

        ok, reason = completion_ready("non-existent-task-id")
        assert ok is False
        assert "不存在" in reason
