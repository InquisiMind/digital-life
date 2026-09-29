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
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS service_leases (
    service_id TEXT PRIMARY KEY,
    holder TEXT NOT NULL,
    acquired_at REAL NOT NULL
);
"""

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
    _SCHEMA_READY = True


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    for col in ("capabilities_json", "subscriptions_json"):
        raw = data.pop(col, None)
        key = col.removesuffix("_json")
        try:
            data[key] = json.loads(raw) if raw else ([] if key == "subscriptions" else {})
        except (TypeError, ValueError):
            data[key] = [] if key == "subscriptions" else {}
    return data


def new_service_id() -> str:
    """生成服务 ID：svc-<12hex>。会进入文件路径，只含安全字符。"""
    return f"svc-{uuid.uuid4().hex[:12]}"


def create_service(
    agent_def_id: str,
    service_id: str | None = None,
    project_id: str = "",
    display_name: str = "",
    capabilities: dict | None = None,
    subscriptions: list | None = None,
) -> dict:
    """注册一个服务型运行体并返回完整行。不创建目录（目录骨架归 domain 层）。"""
    _ensure_schema()
    sid = service_id or new_service_id()
    now = _now_iso()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO services (service_id, agent_def_id, project_id, display_name,"
            " status, capabilities_json, subscriptions_json, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sid,
                agent_def_id,
                project_id,
                display_name,
                SERVICE_STATUS_ACTIVE,
                json.dumps(capabilities or {}, ensure_ascii=False),
                json.dumps(subscriptions if subscriptions is not None else ["message"],
                           ensure_ascii=False),
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
                          subscriptions: list | None = None) -> bool:
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
    params.append(service_id)
    with _connect() as conn:
        cur = conn.execute(
            f"UPDATE services SET {', '.join(sets)} WHERE service_id = ?", params
        )
    return cur.rowcount > 0


# ── id → def 二跳（路径中枢热路径，带 mtime 缓存） ──────────────────────

_def_cache: tuple[float, dict[str, str]] | None = None


def resolve_service_def(service_id: str) -> Optional[str]:
    """service_id → agent_def_id；非服务（含表不存在）返回 None。

    被 infrastructure/config 的 get_instance_dir 在每次路径解析时调用，
    用 services.db 的 mtime 做缓存失效——stat 一次换掉整表扫描。
    """
    global _def_cache
    path = _db_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    if _def_cache is None or _def_cache[0] != mtime:
        try:
            _ensure_schema()
            with _connect() as conn:
                rows = conn.execute(
                    "SELECT service_id, agent_def_id FROM services"
                ).fetchall()
            _def_cache = (mtime, {r["service_id"]: r["agent_def_id"] for r in rows})
        except Exception as exc:
            logger.warning("services registry read failed (treat as empty): %s", exc)
            _def_cache = (mtime, {})
    return _def_cache[1].get(service_id)


def reset_cache_for_test() -> None:
    """测试专用：清掉 mtime 缓存（测试内多次改库后调用）。"""
    global _def_cache
    _def_cache = None


def reset_for_test() -> None:
    """测试专用：连 schema 就绪标记一起重置（切换 DIGITAL_LIFE_SERVICES_DB 后调用）。"""
    global _SCHEMA_READY, _def_cache
    _SCHEMA_READY = False
    _def_cache = None


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
