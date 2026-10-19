"""Tool visibility grants — 授予记录（全局共享 DB）。

可见 = 默认名单 ∪ 通行证池（domain/tool_visibility.py）。本模块只承担
"长期授予"的持久层：按 (runtime_id, source) 一行，upsert 幂等、按 source
整行收回。本轮对话（wake 级）授予是进程内存，不经过这里。

存储位置：<repo>/data/tool_visibility.db（跨实例共享——发放侧在建项目/
master 进程，消费侧在服务 worker 独立进程，必须走共享库）。测试可用环境
变量 DIGITAL_LIFE_TOOL_VISIBILITY_DB 重定向。

runtime_id 是普通列不是外键：实例型与服务型运行体都可被授予（实例型不建
services 行），职责边界与 services.db 同款——只管"路由/授权"，不管运行
数据。
"""

from __future__ import annotations
from contextlib import closing

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("digital_life.infrastructure.persistence.tool_visibility")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tool_grants (
    runtime_id TEXT NOT NULL,
    source TEXT NOT NULL,
    tools_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (runtime_id, source)
);
"""

_SCHEMA_READY = False


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _db_path() -> Path:
    override = os.environ.get("DIGITAL_LIFE_TOOL_VISIBILITY_DB", "").strip()
    if override:
        path = Path(override).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    data_dir = _repo_root() / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "tool_visibility.db"


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
    with closing(_connect()) as conn, conn:
        conn.executescript(_SCHEMA_SQL)
    _SCHEMA_READY = True


def upsert_grant(runtime_id: str, source: str, tools: list[str]) -> None:
    """发放/覆盖一条长期授予（同 (runtime_id, source) 整行替换，幂等）。"""
    if not runtime_id or not source:
        return
    _ensure_schema()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with closing(_connect()) as conn, conn:
        conn.execute(
            "INSERT INTO tool_grants (runtime_id, source, tools_json, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(runtime_id, source) DO UPDATE SET "
            "tools_json=excluded.tools_json, updated_at=excluded.updated_at",
            (runtime_id, source, json.dumps(sorted(tools), ensure_ascii=False), now),
        )


def delete_grant(runtime_id: str, source: str) -> bool:
    """收回一条长期授予。返回是否存在过（测试断言用）。"""
    if not runtime_id or not source:
        return False
    _ensure_schema()
    with closing(_connect()) as conn, conn:
        cur = conn.execute(
            "DELETE FROM tool_grants WHERE runtime_id=? AND source=?",
            (runtime_id, source),
        )
        return cur.rowcount > 0


def list_grants(runtime_id: str) -> dict[str, list[str]]:
    """该运行体全部长期授予 {source: [tools]}。

    读失败按空处理：装配层不得因授予库故障而崩（缺授予只是面收窄，
    与"没发通行证"同语义，warning 留痕即可）。
    """
    if not runtime_id:
        return {}
    try:
        _ensure_schema()
        with closing(_connect()) as conn:
            rows = conn.execute(
                "SELECT source, tools_json FROM tool_grants WHERE runtime_id=? ORDER BY source",
                (runtime_id,),
            ).fetchall()
        return {r["source"]: json.loads(r["tools_json"]) for r in rows}
    except Exception:
        logger.warning("tool_visibility read failed; grants treated empty", exc_info=True)
        return {}


def reset_for_test() -> None:
    global _SCHEMA_READY
    _SCHEMA_READY = False
    path = _db_path()
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            p.unlink()
