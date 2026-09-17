"""Shared infrastructure for domain.todos: DB access, runtime hooks, path helpers."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List

from infrastructure.config import get_runtime_home
from infrastructure.persistence import sqlite
from domain.runtime.session_evidence import (
    DEFAULT_EXECUTION_TOOL_NAMES,
    InMemorySessionEvidenceStore,
    SessionEvidenceReader,
)

logger = logging.getLogger("digital_life.domain.todos")


# ──────────────────────────────── 默认 hook 实现 ────────────────────────────────

def _noop(*args, **kwargs):
    return None


_now_iso_hook: Callable[[], str]
_parse_iso_hook: Callable[[str], Any]
_now_dt_hook: Callable[[], Any]
_emit_event_hook: Callable[..., Any] = _noop
_set_alarm_hook: Callable[..., int] = lambda *args, **kwargs: 0
_cancel_alarms_by_filter_hook: Callable[..., int] = lambda *args, **kwargs: 0
_pop_due_events_hook: Callable[..., List[Dict[str, Any]]] = lambda *args, **kwargs: []
_consume_event_hook: Callable[..., Any] = _noop
_consume_energy_hook: Callable[[float], Any] = lambda amount: SimpleNamespace(energy=0.0)
_session_evidence: InMemorySessionEvidenceStore
_EXECUTION_TOOL_NAMES = DEFAULT_EXECUTION_TOOL_NAMES

from domain.lifecycle.clock import now_dt as _default_now_dt  # noqa: E402
from domain.lifecycle.clock import now_iso as _default_now_iso  # noqa: E402
from domain.lifecycle.clock import parse_iso as _default_parse_iso  # noqa: E402

_now_iso_hook = _default_now_iso
_parse_iso_hook = _default_parse_iso
_now_dt_hook = _default_now_dt
_session_evidence = InMemorySessionEvidenceStore(
    now_iso=lambda: _now_iso_hook(),
    execution_tool_names=_EXECUTION_TOOL_NAMES,
)


def configure_runtime_hooks(
    *,
    now_iso: Callable[[], str] | None = None,
    parse_iso: Callable[[str], Any] | None = None,
    now_dt: Callable[[], Any] | None = None,
    emit_event: Callable[..., Any] | None = None,
    set_alarm: Callable[..., int] | None = None,
    cancel_alarms_by_filter: Callable[..., int] | None = None,
    pop_due_events: Callable[..., List[Dict[str, Any]]] | None = None,
    consume_event: Callable[..., Any] | None = None,
    consume_energy: Callable[[float], Any] | None = None,
    session_evidence: SessionEvidenceReader | None = None,
) -> None:
    """注入适配器提供的运行时服务。"""
    global _now_iso_hook, _parse_iso_hook, _now_dt_hook
    global _emit_event_hook, _pop_due_events_hook
    global _set_alarm_hook, _cancel_alarms_by_filter_hook
    global _consume_event_hook, _consume_energy_hook, _session_evidence

    if now_iso is not None:
        _now_iso_hook = now_iso
    if parse_iso is not None:
        _parse_iso_hook = parse_iso
    if now_dt is not None:
        _now_dt_hook = now_dt
    if emit_event is not None:
        _emit_event_hook = emit_event
    if set_alarm is not None:
        _set_alarm_hook = set_alarm
    if cancel_alarms_by_filter is not None:
        _cancel_alarms_by_filter_hook = cancel_alarms_by_filter
    if pop_due_events is not None:
        _pop_due_events_hook = pop_due_events
    if consume_event is not None:
        _consume_event_hook = consume_event
    if consume_energy is not None:
        _consume_energy_hook = consume_energy
    if session_evidence is not None:
        if isinstance(_session_evidence, InMemorySessionEvidenceStore):
            _session_evidence.set_fallback(session_evidence)
        else:
            _session_evidence = session_evidence


# ──────────────────────────────── 路径 ────────────────────────────────

def tasks_dir() -> Path:
    return get_runtime_home() / "todos"


def _db_path() -> Path:
    """老路径：apps/{iid}/data/todos/todos.db（migration 用）。

    生产路径已经转移到 global_todos.db（见 get_db）。这里保留只是为了
    migration 工具/历史 warning 用，不要在新代码里调它。
    """
    return tasks_dir() / "todos.db"


# ──────────────────────────────── SQLite ────────────────────────────────

# global_todos.db 用的 schema，覆盖所有表：todos、todo_plans、todo_notes、
# todo_sessions、todo_triggers。每次 get_db() 自动幂等建表。
#
# todos 表新字段（vs 实例级旧表）：
#   project_id       关联项目（空=纯个人 todo）
#   assignee_instance 分给谁（可改=可转移）
#   assignee_kind    分配类型（instance / human / position）
#   origin_instance  哪个实例创建（迁移跟踪）
#   source           老字段，向后兼容（'personal' 或 'project:X'）
#
# 老的 ALTER ADD COLUMN（source / linked_deliverable_id / type 等）保留，
# 让老的实例 todos.db 也仍能打开（虽然不再使用）但不会 schema 失败。
_SCHEMA_SQL_GLOBAL = """
CREATE TABLE IF NOT EXISTS todos (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'idea',
    priority TEXT DEFAULT 'medium',
    deadline TEXT,
    tags TEXT DEFAULT '[]',
    project_id TEXT DEFAULT '',
    assignee_instance TEXT DEFAULT '',
    assignee_kind TEXT DEFAULT '',
    parent_id TEXT DEFAULT '',
    linked_deliverable_id TEXT DEFAULT '',
    type TEXT DEFAULT '',
    has_workspace INTEGER DEFAULT 0,
    source TEXT DEFAULT '',
    origin_instance TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_todos_assignee ON todos(assignee_instance, status);
CREATE INDEX IF NOT EXISTS idx_todos_project ON todos(project_id, status);
CREATE INDEX IF NOT EXISTS idx_todos_status ON todos(status);
CREATE INDEX IF NOT EXISTS idx_todos_parent ON todos(parent_id);

CREATE TABLE IF NOT EXISTS todo_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    content TEXT NOT NULL,
    deadline TEXT,
    status TEXT DEFAULT 'pending',
    order_num INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY (task_id) REFERENCES todos(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS todo_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES todos(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS todo_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    session_id TEXT,
    digest TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    FOREIGN KEY (task_id) REFERENCES todos(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS todo_triggers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    assignee TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    trigger_type TEXT NOT NULL DEFAULT 'time',
    due_at TEXT,
    trigger_condition TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    assignee_text TEXT DEFAULT '',
    note TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES todos(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_todo_triggers_assignee ON todo_triggers(assignee, status);
CREATE INDEX IF NOT EXISTS idx_todo_triggers_task ON todo_triggers(task_id);
CREATE INDEX IF NOT EXISTS idx_todo_triggers_status ON todo_triggers(status, due_at);
CREATE INDEX IF NOT EXISTS idx_todo_plans_todo ON todo_plans(task_id);
CREATE INDEX IF NOT EXISTS idx_todo_notes_todo ON todo_notes(task_id);
CREATE INDEX IF NOT EXISTS idx_todo_sessions_todo ON todo_sessions(task_id);
"""


def get_db() -> sqlite.Connection:
    """返回 global_todos.db 的连接。所有实例共享这个 DB。

    迁移后（2026-06-14 重构）：
      实例 todos.db → 退化为历史备份，不再使用
      全部 todos 数据存在 <repo>/data/global_todos.db
      通过 assignee_instance / project_id 列区分归属
    """
    from infrastructure.persistence.global_todos import (
        get_global_todos_db,
    )
    db = get_global_todos_db()
    # 老 schema ADD COLUMN 兜底（如果 OS restarted 或首次跑，可能没附加列）
    # 这里不动新 schema（global_todos.py 已经全在 schema 里建好），只防御性
    # 确保老的 SQL 查询不会因列缺失挂掉
    return db


# ──────────────────────────────── 便捷 hook 代理 ────────────────────────────────

def now_iso() -> str:
    return _now_iso_hook()


def parse_iso(s: str):
    return _parse_iso_hook(s)


def now_dt():
    return _now_dt_hook()


def emit_event(kind: str, payload: dict, fire_at: str = "") -> None:
    return _emit_event_hook(kind=kind, payload=payload, fire_at=fire_at)


def cancel_alarms_by_filter(kind: str, payload_filter: dict) -> int:
    return _cancel_alarms_by_filter_hook(kind=kind, payload_filter=payload_filter)


def set_alarm(event_kind: str, fire_at: str, payload: dict) -> int:
    return _set_alarm_hook(event_kind=event_kind, fire_at=fire_at, payload=payload)


def pop_due_events(limit: int = 20) -> list[dict]:
    return _pop_due_events_hook(limit=limit)


def consume_event(event_id: str) -> None:
    return _consume_event_hook(event_id)


def consume_energy(amount: float) -> Any:
    return _consume_energy_hook(amount)


def session_evidence_store() -> InMemorySessionEvidenceStore:
    return _session_evidence


def task_title_by_id(task_id: str) -> str:
    """按 task_id 查询任务标题，不存在返回 task_id 本身。"""
    db = get_db()
    try:
        row = db.execute("SELECT title FROM todos WHERE id=?", (task_id,)).fetchone()
        return str(row["title"]) if row else task_id
    finally:
        db.close()


def task_has_speckit(task_id: str) -> bool:
    """判断任务是否有 speckit 结构化拆解。"""
    return (tasks_dir() / task_id / "speckit" / "manifest.json").exists()


def get_active_task_workspace() -> tuple[str | None, Path | None]:
    """返回 (task_id, workspace_path)，无进行中任务则返回 (None, None)。

    查询 tasks 表中 status='in_progress' 的最新任务。
    terminal / execute_code 等执行工具以此决定默认工作目录。
    """
    db = get_db()
    try:
        row = db.execute(
            "SELECT id FROM todos WHERE status='in_progress' ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None, None
        task_id = row["id"]
        # 影子目录防护（9/17）：变形 instance_id 不允许凭空建 data/todos/<id>，
        # 返回 ws=None 让工具层降级到 repo 级目录。
        from infrastructure.config import get_app_instance_id, is_registered_instance
        iid = get_app_instance_id()
        if not is_registered_instance(iid):
            logger.warning(
                "[workspace-guard] instance_id=%r 非注册实例，拒绝创建 todo workspace，"
                "降级到 repo 级目录。", iid,
            )
            return task_id, None
        ws = tasks_dir() / task_id
        ws.mkdir(parents=True, exist_ok=True)
        return task_id, ws
    finally:
        db.close()
