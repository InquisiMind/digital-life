"""Services registry — 服务型运行时的注册表与串行 lease（全局共享 DB）。

存储位置：<repo>/data/services.db（跨实例共享，模式对齐 global_todos.db）。
测试可用环境变量 DIGITAL_LIFE_SERVICES_DB 重定向（路径指向 tmp 文件）。

两张表：
  services(service_id PK, agent_def_id, project_id, display_name, status,
           capabilities_json, subscriptions_json, created_at, updated_at)
      - 实例型运行时不建行：service_id == agentID 的退化情形，约定即可
        （总体设计文档 v1.1 收束一），存量实例零迁移。
      - 服务型每个运行体一行。status: active | archived。
  service_leases(service_id PK, holder, acquired_at)
      - 同一服务同时只允许一个 worker（设计文档特性 7 / C 类"服务串行执行闸"）。
        服务 worker 是独立进程，实例的进程内 wake lock 在这里不适用，
        落为 DB lease。
      - holder 约定：master:<pid>（调度循环预订并持有到 worker 退出）、
        worker:<pid>（手工直跑的 worker）。同一 holder 重复 acquire = 续租。
      - 抢占规则：holder 相同直接续；否则 acquired_at 超过 stale_seconds
        才可抢（master 中途死亡留下的僵尸 lease 由超时兜底）。

职责边界：本表只承担"全局路由/管理"语义——id→def 二跳、订阅、开关、
lease。服务自己的事件/会话/记忆在服务私有的 state.db
（apps/{agent_def_id}/services/{service_id}/data/state.db，D1=B 布局），
不经过这里。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("digital_life.infrastructure.persistence.services")

SERVICE_STATUS_ACTIVE = "active"
SERVICE_STATUS_ARCHIVED = "archived"

# 僵尸 lease 超时：worker 是合法长跑进程（实测 wake 有跑过 20+ 分钟），
# 阈值必须盖过最长 wake；正常崩溃由调度循环 reap 立即释放，超时只兜
# "master 中途死亡"这一种情形。
DEFAULT_LEASE_STALE_SECONDS = 3600.0

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS services (
    service_id TEXT PRIMARY KEY,
    agent_def_id TEXT NOT NULL,
    project_id TEXT DEFAULT '',
    display_name TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    capabilities_json TEXT DEFAULT '{}',
    subscriptions_json TEXT DEFAULT '["message"]',
    tools_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS service_leases (
    service_id TEXT PRIMARY KEY,
    holder TEXT NOT NULL,
    acquired_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    template_id TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    stage TEXT DEFAULT '',
    workspace_path TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_services_project ON services(project_id, status);
CREATE TABLE IF NOT EXISTS project_members (
    project_id TEXT NOT NULL,
    member_id TEXT NOT NULL,
    role TEXT DEFAULT '',
    joined_at TEXT NOT NULL,
    PRIMARY KEY (project_id, member_id)
);
CREATE TABLE IF NOT EXISTS project_todos (
    todo_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    parent_id TEXT DEFAULT '',
    title TEXT NOT NULL,
    detail TEXT DEFAULT '',
    assignee_id TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    due_at TEXT,
    created_by TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    kind TEXT DEFAULT 'task',
    depends_on TEXT DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_project_todos ON project_todos(project_id, status);
CREATE TABLE IF NOT EXISTS project_todo_events (
    rowid_ INTEGER PRIMARY KEY AUTOINCREMENT,
    todo_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT DEFAULT '',
    actor TEXT DEFAULT '',
    from_value TEXT DEFAULT '',
    to_value TEXT DEFAULT '',
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_project_todo_events ON project_todo_events(project_id, at);
"""

# 旧库增量列（刀4b 轻量化：projects 补 pm/watchdog 字段——列存在则跳过）
_MIGRATION_COLUMNS = {
    "projects": {
        "description": "TEXT DEFAULT ''",
        "pm_id": "TEXT DEFAULT ''",
        "watchdog_enabled": "INTEGER DEFAULT 0",
        "last_stall_ping_at": "TEXT",
        "customer_id": "TEXT DEFAULT ''",
    },
    "services": {
        "tools_json": "TEXT",
    },
    "project_todos": {
        "kind": "TEXT DEFAULT 'task'",
        "depends_on": "TEXT DEFAULT '[]'",
    },
}

_SCHEMA_READY = False


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _db_path() -> Path:
    override = os.environ.get("DIGITAL_LIFE_SERVICES_DB", "").strip()
    if override:
        path = Path(override).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    data_dir = _repo_root() / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "services.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _ensure_schema() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with _connect() as conn:
        conn.executescript(_SCHEMA_SQL)
        for table, cols in _MIGRATION_COLUMNS.items():
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for col, decl in cols.items():
                if col not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    _SCHEMA_READY = True


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    for col in ("capabilities_json", "subscriptions_json", "tools_json"):
        raw = data.pop(col, None)
        key = col.removesuffix("_json")
        try:
            if key == "tools":
                data[key] = json.loads(raw) if raw else None
            elif key == "subscriptions":
                data[key] = json.loads(raw) if raw else []
            else:
                data[key] = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            data[key] = None if key == "tools" else ([] if key == "subscriptions" else {})
    return data


def new_service_id() -> str:
    """生成服务 ID：svc-<12hex>。会进入文件路径，只含安全字符。"""
    return f"svc-{uuid.uuid4().hex[:12]}"


# ── projects（CustomerProject 注册表，刀 4）────────────────────────────


def new_project_id() -> str:
    """生成项目 ID：prj-<12hex>。"""
    return f"prj-{uuid.uuid4().hex[:12]}"


def create_project(name: str, template_id: str = "", description: str = "",
                   pm_id: str = "", watchdog_enabled: bool = False,
                   customer_id: str = "") -> dict:
    """注册一个客户项目（轻量：只有名分和开关，目录/成员/待办归 domain 层）。"""
    _ensure_schema()
    pid = new_project_id()
    now = _now_iso()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO projects (project_id, name, template_id, description, pm_id,"
            " watchdog_enabled, customer_id, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
            (pid, name, template_id, description, pm_id,
             1 if watchdog_enabled else 0, customer_id, now, now),
        )
    logger.info("PROJECT_CREATED project_id=%s name=%r template=%s pm=%s",
                pid, name, template_id, pm_id)
    row = lookup_project(pid)
    assert row is not None
    return row


def lookup_project(project_id: str) -> Optional[dict]:
    _ensure_schema()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()
    return dict(row) if row is not None else None


def list_projects(status: str | None = None) -> list[dict]:
    _ensure_schema()
    q = "SELECT * FROM projects"
    params: tuple = ()
    if status:
        q += " WHERE status = ?"
        params = (status,)
    q += " ORDER BY created_at"
    with _connect() as conn:
        rows = conn.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def update_project_fields(project_id: str, *, name: str | None = None,
                          status: str | None = None, stage: str | None = None,
                          description: str | None = None, pm_id: str | None = None,
                          watchdog_enabled: bool | None = None,
                          last_stall_ping_at: str | None = None) -> bool:
    _ensure_schema()
    sets = ["updated_at = ?"]
    params: list[Any] = [_now_iso()]
    if name is not None:
        sets.append("name = ?"); params.append(name)
    if status is not None:
        sets.append("status = ?"); params.append(status)
    if stage is not None:
        sets.append("stage = ?"); params.append(stage)
    if description is not None:
        sets.append("description = ?"); params.append(description)
    if pm_id is not None:
        sets.append("pm_id = ?"); params.append(pm_id)
    if watchdog_enabled is not None:
        sets.append("watchdog_enabled = ?"); params.append(1 if watchdog_enabled else 0)
    if last_stall_ping_at is not None:
        sets.append("last_stall_ping_at = ?"); params.append(last_stall_ping_at)
    params.append(project_id)
    with _connect() as conn:
        cur = conn.execute(
            f"UPDATE projects SET {', '.join(sets)} WHERE project_id = ?", params
        )
    return cur.rowcount > 0


# ── project members / todos（刀 4b 轻量化）────────────────────────────


def add_project_member(project_id: str, member_id: str, role: str = "") -> None:
    _ensure_schema()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO project_members (project_id, member_id, role, joined_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(project_id, member_id) DO UPDATE SET role = excluded.role",
            (project_id, member_id, role, _now_iso()),
        )


def remove_project_member(project_id: str, member_id: str) -> None:
    _ensure_schema()
    with _connect() as conn:
        conn.execute(
            "DELETE FROM project_members WHERE project_id = ? AND member_id = ?",
            (project_id, member_id),
        )


def list_project_members(project_id: str) -> list[dict]:
    _ensure_schema()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM project_members WHERE project_id = ? ORDER BY joined_at",
            (project_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_project_members_of(service_id: str) -> list[dict]:
    """某服务参加的全部项目成员关系（N:M）。"""
    _ensure_schema()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM project_members WHERE member_id = ? ORDER BY joined_at",
            (service_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def new_todo_id() -> str:
    return f"ptodo-{uuid.uuid4().hex[:10]}"


def create_project_todo(project_id: str, title: str, *, parent_id: str = "",
                        detail: str = "", assignee_id: str = "",
                        due_at: str | None = None, created_by: str = "",
                        kind: str = "task", depends_on: list | None = None,
                        status: str = "open") -> dict:
    _ensure_schema()
    tid = new_todo_id()
    now = _now_iso()
    st = status if status in ("open", "blocked") else "open"
    with _connect() as conn:
        conn.execute(
            "INSERT INTO project_todos (todo_id, project_id, parent_id, title, detail,"
            " assignee_id, status, due_at, created_by, created_at, updated_at, kind, depends_on)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tid, project_id, parent_id, title, detail, assignee_id, st, due_at,
             created_by, now, now,
             kind if kind in ("task", "milestone") else "task",
             json.dumps([str(x) for x in depends_on], ensure_ascii=False) if depends_on else "[]"),
        )
        _log_todo_event(conn, tid, project_id, "created", title,
                        actor=created_by or "system",
                        **{"to": st})
    row = get_project_todo(tid)
    assert row is not None
    return row


def get_project_todo(todo_id: str) -> Optional[dict]:
    _ensure_schema()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM project_todos WHERE todo_id = ?", (todo_id,)
        ).fetchone()
    return dict(row) if row is not None else None


def list_project_todos(project_id: str, *, status: str | None = None,
                       assignee_id: str | None = None) -> list[dict]:
    _ensure_schema()
    q = "SELECT * FROM project_todos WHERE project_id = ?"
    params: list[Any] = [project_id]
    if status is not None:
        q += " AND status = ?"
        params.append(status)
    if assignee_id is not None:
        q += " AND assignee_id = ?"
        params.append(assignee_id)
    q += " ORDER BY created_at"
    with _connect() as conn:
        rows = conn.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def update_project_todo(todo_id: str, *, title: str | None = None,
                        detail: str | None = None, assignee_id: str | None = None,
                        status: str | None = None, due_at: str | None = None,
                        actor: str = "") -> bool:
    _ensure_schema()
    before = get_project_todo(todo_id)
    sets = ["updated_at = ?"]
    params: list[Any] = [_now_iso()]
    if title is not None:
        sets.append("title = ?"); params.append(title)
    if detail is not None:
        sets.append("detail = ?"); params.append(detail)
    if assignee_id is not None:
        sets.append("assignee_id = ?"); params.append(assignee_id)
    if status is not None:
        sets.append("status = ?"); params.append(status)
    if due_at is not None:
        sets.append("due_at = ?"); params.append(due_at)
    params.append(todo_id)
    with _connect() as conn:
        cur = conn.execute(
            f"UPDATE project_todos SET {', '.join(sets)} WHERE todo_id = ?", params
        )
        ok = cur.rowcount > 0
        if ok and before is not None:
            # 变更日志：完成/重启/改派单独成事件，其余归 updated
            ev_kind = None
            if status is not None and status != before.get("status"):
                ev_kind = "completed" if status == "done" else "reopened"
            elif assignee_id is not None and assignee_id != before.get("assignee_id"):
                ev_kind = "reassigned"
            else:
                ev_kind = "updated"
            _log_todo_event(conn, todo_id, before["project_id"], ev_kind,
                            title or before["title"], actor=actor or "system",
                            **{"from": before.get("assignee_id") or "",
                               "to": assignee_id if assignee_id is not None
                                     else before.get("assignee_id") or ""})
        if ok and status == "done":
            _unlock_dependents(conn, before["project_id"],
                               done_title=(title or before["title"]))
    return ok


def _unlock_dependents(conn, project_id: str, done_title: str) -> None:
    """编排器：任务完成后解锁前置全部满足的 blocked 任务（标题引用）。"""
    rows = conn.execute(
        "SELECT todo_id, title, depends_on FROM project_todos"
        " WHERE project_id = ? AND status = 'blocked'",
        (project_id,),
    ).fetchall()
    if not rows:
        return
    done_titles = {
        r["title"] for r in conn.execute(
            "SELECT title FROM project_todos WHERE project_id = ? AND status = 'done'",
            (project_id,),
        ).fetchall()
    }
    done_titles.add(done_title)
    for r in rows:
        try:
            deps = json.loads(r["depends_on"] or "[]")
        except (TypeError, ValueError):
            deps = []
        if isinstance(deps, list) and deps and all(d in done_titles for d in deps):
            conn.execute(
                "UPDATE project_todos SET status = 'open', updated_at = ? WHERE todo_id = ?",
                (_now_iso(), r["todo_id"]),
            )
            _log_todo_event(conn, r["todo_id"], project_id, "unblocked",
                            r["title"], actor="orchestrator",
                            **{"to": "、".join(str(d) for d in deps)})


# ── todo 变更日志（任务过程时间线的审计真相） ────────────────────────────


def _log_todo_event(conn, todo_id: str, project_id: str, kind: str, title: str,
                     *, actor: str = "system", **extra) -> None:
    conn.execute(
        "INSERT INTO project_todo_events (todo_id, project_id, kind, title,"
        " actor, from_value, to_value, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (todo_id, project_id, kind, title, actor,
         str(extra.get("from", "")), str(extra.get("to", "")), _now_iso()),
    )


def list_project_todo_events(project_id: str, limit: int = 200) -> list[dict]:
    """任务变更事件（新→旧）：created/updated/completed/reopened/reassigned。"""
    _ensure_schema()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM project_todo_events WHERE project_id = ?"
            " ORDER BY at DESC, rowid DESC LIMIT ?",
            (project_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def list_services_by_project(project_id: str, status: str | None = None) -> list[dict]:
    """项目内服务清单（协作工具/管理面用）。"""
    _ensure_schema()
    q = "SELECT * FROM services WHERE project_id = ?"
    params: list[Any] = [project_id]
    if status:
        q += " AND status = ?"
        params.append(status)
    q += " ORDER BY created_at"
    with _connect() as conn:
        rows = conn.execute(q, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def create_service(
    agent_def_id: str,
    service_id: str | None = None,
    project_id: str = "",
    display_name: str = "",
    capabilities: dict | None = None,
    subscriptions: list | None = None,
    tools: list | None = None,
) -> dict:
    """注册一个服务型运行体并返回完整行。不创建目录（目录骨架归 domain 层）。

    tools：岗位工具面（模版角色 → 服务行，None=默认全量项目工具集）。
    """
    _ensure_schema()
    sid = service_id or new_service_id()
    now = _now_iso()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO services (service_id, agent_def_id, project_id, display_name,"
            " status, capabilities_json, subscriptions_json, tools_json, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sid,
                agent_def_id,
                project_id,
                display_name,
                SERVICE_STATUS_ACTIVE,
                json.dumps(capabilities or {}, ensure_ascii=False),
                json.dumps(subscriptions if subscriptions is not None else ["message"],
                           ensure_ascii=False),
                json.dumps(tools, ensure_ascii=False) if tools is not None else None,
                now,
                now,
            ),
        )
    logger.info("SERVICE_CREATED service_id=%s agent_def_id=%s project_id=%s",
                sid, agent_def_id, project_id)
    row = lookup_service(sid)
    assert row is not None
    return row


def lookup_service(service_id: str) -> Optional[dict]:
    _ensure_schema()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM services WHERE service_id = ?", (service_id,)
        ).fetchone()
    return _row_to_dict(row) if row is not None else None


def list_services(status: str | None = None) -> list[dict]:
    _ensure_schema()
    if status:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT * FROM services WHERE status = ? ORDER BY created_at",
                (status,),
            ).fetchall()
    else:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT * FROM services ORDER BY created_at"
            ).fetchall()
    return [_row_to_dict(r) for r in rows]


def set_service_status(service_id: str, status: str) -> bool:
    """active ↔ archived。archived 即设计文档的"服务开关"（归档/封存）。"""
    if status not in (SERVICE_STATUS_ACTIVE, SERVICE_STATUS_ARCHIVED):
        raise ValueError(f"unknown service status: {status!r}")
    _ensure_schema()
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE services SET status = ?, updated_at = ? WHERE service_id = ?",
            (status, _now_iso(), service_id),
        )
    return cur.rowcount > 0


def update_service_fields(service_id: str, *, project_id: str | None = None,
                          display_name: str | None = None,
                          capabilities: dict | None = None,
                          subscriptions: list | None = None,
                          tools: list | None = None) -> bool:
    """部分更新服务配置（刀 3 管理面的落点，先备好最小写入）。"""
    _ensure_schema()
    sets: list[str] = ["updated_at = ?"]
    params: list[Any] = [_now_iso()]
    if project_id is not None:
        sets.append("project_id = ?")
        params.append(project_id)
    if display_name is not None:
        sets.append("display_name = ?")
        params.append(display_name)
    if capabilities is not None:
        sets.append("capabilities_json = ?")
        params.append(json.dumps(capabilities, ensure_ascii=False))
    if subscriptions is not None:
        sets.append("subscriptions_json = ?")
        params.append(json.dumps(subscriptions, ensure_ascii=False))
    if tools is not None:
        sets.append("tools_json = ?")
        params.append(json.dumps(tools, ensure_ascii=False))
    params.append(service_id)
    with _connect() as conn:
        cur = conn.execute(
            f"UPDATE services SET {', '.join(sets)} WHERE service_id = ?", params
        )
    return cur.rowcount > 0


# ── id → def 二跳（路径中枢热路径，正命中缓存） ────────────────────────

# 只增不减：service_id → agent_def_id 在 create_service 后不可变（无删除/
# 改挂路径），正命中永不失效。mtime 缓存方案已被证伪：WAL 模式下写进
# -wal 文件时主库 mtime 不变，master 曾因此 3 分钟"看不见"新建的服务，
# has_due_events 把它解析到不存在的 apps/{sid}/ 而静默跳过（2026-09-29
# 双服务验收现场抓获）。
_def_cache: dict[str, str] = {}
# 负缓存（非服务 id → 判定时刻）：TTL 5s——新服务对其后 ≤5s 即可见
# （master 调度循环 10s 一扫，天然覆盖）；避免测试/陌生 id 每次路径解析
# 都打一次 DB（正命中缓存初版的性能坑，2026-09-29 全量抓到）
_neg_cache: dict[str, float] = {}
_NEG_TTL_S = 5.0


def resolve_service_def(service_id: str) -> Optional[str]:
    """service_id → agent_def_id；非服务（含表不存在）返回 None。

    三层快慢路径（热路径，每次路径解析都会过这里）：
      1. 正命中内存 dict（服务→定义映射创建后不可变，永不失效）；
      2. apps/{id}/config/app.yaml 存在 → 实例或 agent 定义目录，文件系统
         事实即"不是服务"，一次 stat 零 DB——恢复实例侧原有性能（正命中
         缓存初版所有实例 id 未命中都打一次 DB，全局变慢，曾让 e2e 测试
         泄漏的后台 wake 线程时序漂移引发跨测试消费，2026-09-29 全量抓到）；
      3. 陌生 id（服务）直查注册表一次并入缓存。
    """
    hit = _def_cache.get(service_id)
    if hit:
        return hit
    import time as _time

    neg = _neg_cache.get(service_id)
    if neg is not None and (_time.time() - neg) < _NEG_TTL_S:
        return None
    override = os.environ.get("DIGITAL_LIFE_SERVICES_DB", "").strip()
    apps_dir = (Path(override).parent.parent / "apps") if override else (_repo_root() / "apps")
    try:
        if (apps_dir / service_id / "config" / "app.yaml").exists():
            _neg_cache[service_id] = _time.time()
            return None
    except OSError:
        pass
    try:
        _ensure_schema()
        with _connect() as conn:
            row = conn.execute(
                "SELECT agent_def_id FROM services WHERE service_id = ?",
                (service_id,),
            ).fetchone()
    except Exception as exc:
        logger.warning("services registry lookup failed for %r: %s", service_id, exc)
        return None
    if row is None:
        _neg_cache[service_id] = _time.time()
        return None
    _def_cache[service_id] = row["agent_def_id"]
    return row["agent_def_id"]


def reset_cache_for_test() -> None:
    """测试专用：清掉映射缓存（测试内多次建/换库后调用）。"""
    global _def_cache, _neg_cache
    _def_cache = {}
    _neg_cache = {}


def reset_for_test() -> None:
    """测试专用：连 schema 就绪标记一起重置（切换 DIGITAL_LIFE_SERVICES_DB 后调用）。"""
    global _SCHEMA_READY, _def_cache, _neg_cache
    _SCHEMA_READY = False
    _def_cache = {}
    _neg_cache = {}


# ── 服务串行执行闸（DB lease） ─────────────────────────────────────────

def try_acquire_lease(service_id: str, holder: str, stale_seconds: float) -> bool:
    """原子抢租。同一 holder 重复 acquire = 续租；他人持有需超过 stale 才可抢。

    单条 upsert 完成判断与写入，天然并发安全（master 重启后的新循环、
    手工 worker 同时抢同一服务时只有一个 changes()==1）。
    """
    _ensure_schema()
    now = time.time()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO service_leases (service_id, holder, acquired_at)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(service_id) DO UPDATE SET"
            " holder = excluded.holder, acquired_at = excluded.acquired_at"
            " WHERE service_leases.holder = excluded.holder"
            " OR service_leases.acquired_at <= ?",
            (service_id, holder, now, now - stale_seconds),
        )
    return cur.rowcount > 0


def release_lease(service_id: str, holder: str) -> bool:
    """释放租约（只释放自己持有的）。"""
    _ensure_schema()
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM service_leases WHERE service_id = ? AND holder = ?",
            (service_id, holder),
        )
    return cur.rowcount > 0


def get_lease(service_id: str) -> Optional[dict]:
    _ensure_schema()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM service_leases WHERE service_id = ?", (service_id,)
        ).fetchone()
    return dict(row) if row is not None else None
