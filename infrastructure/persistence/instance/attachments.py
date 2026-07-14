"""Per-instance attachment registry.

Stores metadata about received/created media files (images, files, audio).
Binary data lives on the filesystem at ``apps/<id>/data/attachments/{sha}.{ext}``;
this table only stores references + sha256 dedup.

Design: source-agnostic — every channel (feishu/wechat/local) registers
attachments here; downstream tools (sense_image) only see attachment_id and
mime, never caring where the bytes came from.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from infrastructure.config import get_app_instance_id, get_instance_data_dir
from infrastructure.persistence.instance.base import InstanceDB

logger = logging.getLogger(__name__)


# ── MIME → ext 映射（覆盖常见类型；未知 mime 默认 bin）──
_MIME_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/bmp": "bmp",
    "audio/opus": "opus",
    "audio/mp3": "mp3",
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
    "video/mp4": "mp4",
    "application/pdf": "pdf",
}


def ext_from_mime(mime: str) -> str:
    """MIME → 文件扩展名，未知 fallback 'bin'。"""
    return _MIME_EXT.get((mime or "").lower(), "bin")


def attachments_dir(instance_id: str | None = None) -> Path:
    """附件存储目录：``apps/<id>/data/attachments/``。自动创建。"""
    iid = instance_id or get_app_instance_id() or ""
    if not iid:
        raise RuntimeError("attachments_dir 需要 instance_id（ContextVar 未设）")
    d = get_instance_data_dir(iid) / "attachments"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass(frozen=True)
class Attachment:
    """一条附件的不可变记录。"""

    attachment_id: str
    instance_id: str
    source: str          # "feishu" / "wechat" / "local"
    source_key: str      # 渠道原始 key（image_key / file_key / sha256 子串）
    mime: str            # "image/png" / "image/jpeg" / ...
    local_path: str      # 绝对路径
    sha256: str
    size_bytes: int
    created_at: str      # ISO8601

    @property
    def kind(self) -> str:
        """资源大类——`image` / `audio` / `video` / `file`。sense_image 只允许 image。"""
        m = (self.mime or "").lower()
        if m.startswith("image/"):
            return "image"
        if m.startswith("audio/"):
            return "audio"
        if m.startswith("video/"):
            return "video"
        return "file"

    def to_summary(self) -> dict:
        """给 events payload / chat_stream 用的轻量摘要——不含本地路径。"""
        return {
            "attachment_id": self.attachment_id,
            "source": self.source,
            "mime": self.mime,
            "kind": self.kind,
            "size_bytes": self.size_bytes,
        }


_SCHEMA = """
CREATE TABLE IF NOT EXISTS attachments (
    attachment_id TEXT PRIMARY KEY,
    instance_id TEXT NOT NULL,
    source TEXT NOT NULL,
    source_key TEXT NOT NULL,
    mime TEXT NOT NULL,
    local_path TEXT NOT NULL,
    sha256 TEXT,
    size_bytes INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE(source, source_key)
);
CREATE INDEX IF NOT EXISTS idx_attachments_recent ON attachments(instance_id, created_at DESC);
"""


class _AttachmentsDB(InstanceDB):
    """per-instance attachments SQL 包装（在 state.db 上建表）。"""

    SCHEMA_SQL = (_SCHEMA,)


# 全局 cache：每个iid 一个 _AttachmentsDB，避免每次调用都 open 同一份 state.db
_dbs: dict[str, _AttachmentsDB] = {}
_dbs_lock = threading.Lock()


def _get_db(instance_id: str) -> _AttachmentsDB:
    """获取/创建某 instance 的 attachments DB 包装。

    表建在 state.db 上（而非独立 attachments.db）——和 events / affairs 同库，
    schema 由本模块负责（CREATE IF NOT EXISTS），由 init_all_schemas 间接触发。
    """
    with _dbs_lock:
        db = _dbs.get(instance_id)
        if db is None:
            from infrastructure.config import get_runtime_state_db_path
            # 注意：get_runtime_state_db_path 依赖 ContextVar 已设；显式传 instance_id
            from infrastructure.config import set_current_instance_id, reset_current_instance_id
            token = set_current_instance_id(instance_id)
            try:
                db_path = get_runtime_state_db_path()
            finally:
                reset_current_instance_id(token)
            db = _AttachmentsDB(db_path)
            _dbs[instance_id] = db
        return db


def _row_to_attachment(row: sqlite3.Row) -> Attachment:
    return Attachment(
        attachment_id=row["attachment_id"],
        instance_id=row["instance_id"],
        source=row["source"],
        source_key=row["source_key"],
        mime=row["mime"],
        local_path=row["local_path"],
        sha256=row["sha256"] or "",
        size_bytes=row["size_bytes"] or 0,
        created_at=row["created_at"],
    )


def ensure_schema(instance_id: str | None = None) -> None:
    """建表（幂等）。被 init_all_schemas 调用。"""
    iid = instance_id or get_app_instance_id() or ""
    if not iid:
        return
    _get_db(iid)  # 触发 SCHEMA_SQL 应用


def register_attachment(
    *,
    instance_id: str,
    source: str,
    source_key: str,
    mime: str,
    local_path: str,
    size_bytes: int | None = None,
    sha256: str | None = None,
) -> Attachment:
    """登记一个附件，同 (source, source_key) 已存在则返回旧记录（去重）。

    Args:
        instance_id: 实例 ID
        source: 来源渠道（feishu / wechat / local）
        source_key: 在该渠道里的唯一 key
        mime: MIME 类型
        local_path: 本地落盘路径
        size_bytes: 文件大小（可选，自动从 path 读）
        sha256: sha256（可选，自动从 path 算）

    Returns:
        Attachment（新建或已存在）
    """
    db = _get_db(instance_id)
    attachment_id = f"{source}:{source_key}"

    # 先看是否已存在（去重路径）
    with db._lock:
        existing = db._conn.execute(
            "SELECT * FROM attachments WHERE source=? AND source_key=?",
            (source, source_key),
        ).fetchone()
        if existing:
            return _row_to_attachment(existing)

    # 新建——补 size_bytes / sha256
    p = Path(local_path)
    if size_bytes is None:
        try:
            size_bytes = p.stat().st_size
        except OSError:
            size_bytes = 0
    if sha256 is None:
        try:
            sha256 = hashlib.sha256(p.read_bytes()).hexdigest()
        except Exception as exc:
            logger.warning("register_attachment sha256 failed for %s: %s", local_path, exc)
            sha256 = ""

    created_at = time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime())
    with db._lock:
        try:
            db._conn.execute(
                """INSERT OR IGNORE INTO attachments
                   (attachment_id, instance_id, source, source_key, mime,
                    local_path, sha256, size_bytes, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (attachment_id, instance_id, source, source_key, mime,
                 local_path, sha256, size_bytes, created_at),
            )
            db._conn.commit()
        except sqlite3.IntegrityError:
            pass  # race：另一个 thread 先建了——SELECT 兜底

        row = db._conn.execute(
            "SELECT * FROM attachments WHERE attachment_id=?",
            (attachment_id,),
        ).fetchone()
    return _row_to_attachment(row) if row else Attachment(
        attachment_id=attachment_id, instance_id=instance_id,
        source=source, source_key=source_key, mime=mime,
        local_path=local_path, sha256=sha256 or "",
        size_bytes=size_bytes or 0, created_at=created_at,
    )


def get_attachment(attachment_id: str) -> Attachment | None:
    """按 attachment_id 查（自动用 prefix 推导 instance_id 或跨实例扫）。"""
    iid = get_app_instance_id() or ""
    if iid:
        try:
            db = _get_db(iid)
            with db._lock:
                row = db._conn.execute(
                    "SELECT * FROM attachments WHERE attachment_id=?",
                    (attachment_id,),
                ).fetchone()
            if row:
                return _row_to_attachment(row)
        except Exception:
            pass

    # attachment_id 不在当前实例——可能跨实例调用（少见但仍允许）
    # fallback：扫所有 active 实例的 attachments（best-effort）
    try:
        from infrastructure.config import discover_instances
        for iid in discover_instances():
            try:
                db = _get_db(iid)
                with db._lock:
                    row = db._conn.execute(
                        "SELECT * FROM attachments WHERE attachment_id=?",
                        (attachment_id,),
                    ).fetchone()
                if row:
                    return _row_to_attachment(row)
            except Exception:
                continue
    except Exception:
        pass
    return None


def list_recent_attachments(instance_id: str, limit: int = 20) -> list[Attachment]:
    """列某实例最近的附件（按时间倒序）。"""
    db = _get_db(instance_id)
    with db._lock:
        rows = db._conn.execute(
            "SELECT * FROM attachments WHERE instance_id=? ORDER BY created_at DESC LIMIT ?",
            (instance_id, limit),
        ).fetchall()
    return [_row_to_attachment(r) for r in rows]


def save_bytes_as_attachment(
    *,
    instance_id: str,
    source: str,
    source_key: str,
    data: bytes,
    mime: str,
) -> Attachment:
    """便利函数：把 bytes 落盘 + register 一步到位。

    落盘路径：``apps/<id>/data/attachments/{sha256[:16]}.{ext}``。同 sha 自动去重。
    """
    sha = hashlib.sha256(data).hexdigest()
    ext = ext_from_mime(mime)
    dest = attachments_dir(instance_id) / f"{sha[:16]}.{ext}"
    if not dest.exists():
        dest.write_bytes(data)
    return register_attachment(
        instance_id=instance_id,
        source=source,
        source_key=source_key,
        mime=mime,
        local_path=str(dest),
        size_bytes=len(data),
        sha256=sha,
    )


__all__ = [
    "Attachment",
    "attachments_dir",
    "ensure_schema",
    "register_attachment",
    "get_attachment",
    "list_recent_attachments",
    "save_bytes_as_attachment",
    "ext_from_mime",
]
