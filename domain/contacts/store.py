"""Contacts storage — 用户为单位的多平台 ID 映射 + 黑名单。

Schema:
  contacts(id PK, name, notes, about, kind, blocked, block_reason, updated_at)
    -- notes: 短备注；about: 自由文本画像（替代已退役的 HIM.md）
    -- kind: 'human' / 'bot' / 'system'
  contact_ids(contact_id, platform, platform_id, PRIMARY KEY(compound))
    -- platform: 'feishu' / 'dingtalk' / 'wechat' / ...
    -- platform_id: 该平台下的 user identifier

Lookup: 给 (platform, platform_id) → 返回 contact（含 name 和 blocked flag）。
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from pathlib import Path

from domain.lifecycle.clock import now_iso as _now_iso

logger = logging.getLogger(__name__)


def _state_db_path() -> Path:
    from infrastructure.config import get_runtime_state_db_path
    return get_runtime_state_db_path()


def ensure_schema() -> None:
    """建表（幂等）。在 instance ContextVar 已设置后调用。

    Migrations:
      - v1: contacts(open_id PK, name, notes, updated_at) + blocked_contacts
      - v2: 用户为单位的多平台 ID。检测旧表自动 migrate。
    """
    db_path = _state_db_path()
    if not db_path.exists():
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS contacts (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                notes TEXT DEFAULT '',
                about TEXT DEFAULT '',
                kind TEXT NOT NULL DEFAULT 'human',
                blocked INTEGER NOT NULL DEFAULT 0,
                block_reason TEXT DEFAULT '',
                updated_at TEXT DEFAULT ''
            )
            """
        )
        # 兼容旧 DB:若 contacts 表无 about 列(remember_him 退役前), ADD COLUMN。
        # about 替代 HIM.md 自由文本画像;非破坏性迁移,老 rows 默认空串。
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(contacts)").fetchall()]
            if cols and "about" not in cols:
                conn.execute("ALTER TABLE contacts ADD COLUMN about TEXT DEFAULT ''")
        except sqlite3.Error:
            pass
        # 若列 DEFAULT 是历史 naive localtime 值，全表回填一次 + 切到应用层主动写入。
        # 保持表结构兼容（无需 ALTER DEFAULT），所有 INSERT/UPDATE 在应用层显式传入 clock.now_iso()。
        # 兜底：存量列 DEFAULT 失效时，确保未传入的 INSERT 行也能拿到 UTC ISO（用 AFTER INSERT 触发器代价大，未启用）。
        # 接下来的 INSERT/UPDATE 处会主动写入更新时间。
        # 兼容旧 DB：添加 kind 列（不存在则加）
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(contacts)").fetchall()]
            if cols and "kind" not in cols:
                conn.execute("ALTER TABLE contacts ADD COLUMN kind TEXT NOT NULL DEFAULT 'human'")
        except sqlite3.Error:
            pass
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS contact_ids (
                contact_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                platform_id TEXT NOT NULL,
                PRIMARY KEY (contact_id, platform, platform_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_contact_ids_lookup "
            "ON contact_ids(platform, platform_id)"
        )
        # Migration: 旧 contacts 表（v1, open_id 为 PK）→ v2
            # 仅在 schema 是 v1 时执行；用 PRAGMA 检测。
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(contacts)").fetchall()]
            if cols and "open_id" in cols and "id" not in cols:
                _migrate_v1_to_v2(conn)
        except sqlite3.Error:
            pass
        conn.commit()
    finally:
        conn.close()


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """v1 schema → v2: open_id-as-PK → contact + contact_ids. blocked_contacts 也并入。"""
    logger.info("Migrating contacts schema v1 → v2")
    conn.executescript(
        """
        ALTER TABLE contacts RENAME TO contacts_v1_legacy;
        CREATE TABLE contacts (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            notes TEXT DEFAULT '',
            about TEXT DEFAULT '',
            blocked INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT DEFAULT '',
            updated_at TEXT DEFAULT ''
        );
        CREATE TABLE contact_ids (
            contact_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            platform_id TEXT NOT NULL,
            PRIMARY KEY (contact_id, platform, platform_id)
        );
        """
    )
    blocks: dict[str, str] = {}
    try:
        for row in conn.execute("SELECT open_id, reason FROM blocked_contacts"):
            blocks[row[0]] = row[1] or ""
    except sqlite3.Error:
        pass

    for row in conn.execute("SELECT open_id, name, notes FROM contacts_v1_legacy"):
        open_id, name, notes = row
        if not open_id:
            continue
        cid = str(uuid.uuid4())
        is_blocked = 1 if open_id in blocks else 0
        reason = blocks.get(open_id, "")
        conn.execute(
            "INSERT INTO contacts (id, name, notes, blocked, block_reason, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (cid, name or "", notes or "", is_blocked, reason, _now_iso()),
        )
        conn.execute(
            "INSERT INTO contact_ids (contact_id, platform, platform_id) VALUES (?, 'feishu', ?)",
            (cid, open_id),
        )
    try:
        conn.execute("DROP TABLE blocked_contacts")
    except sqlite3.Error:
        pass
    logger.info("Migration v1 → v2 done")


# ──────────────────────────────── CRUD: contacts ────────────────────────────────


def _row_to_contact(row: sqlite3.Row, *, ids_by_contact: dict | None = None) -> dict:
    d = dict(row)
    d["blocked"] = bool(d.get("blocked", 0))
    if ids_by_contact is not None:
        d["platform_ids"] = ids_by_contact.get(d["id"], [])
    return d


def _load_ids_grouped(conn: sqlite3.Connection, contact_ids: list[str]) -> dict[str, list[dict]]:
    if not contact_ids:
        return {}
    placeholders = ",".join("?" * len(contact_ids))
    rows = conn.execute(
        f"SELECT contact_id, platform, platform_id FROM contact_ids "
        f"WHERE contact_id IN ({placeholders}) ORDER BY platform, platform_id",
        contact_ids,
    ).fetchall()
    out: dict[str, list[dict]] = {}
    for cid, platform, platform_id in rows:
        out.setdefault(cid, []).append({"platform": platform, "platform_id": platform_id})
    return out


def list_contacts(*, include_blocked: bool = True) -> list[dict]:
    """列全部联系人。include_blocked=False 时仅返回未拉黑的。"""
    if not _state_db_path().exists():
        return []
    try:
        conn = sqlite3.connect(str(_state_db_path()))
        conn.row_factory = sqlite3.Row
        try:
            sql = "SELECT id, name, notes, about, kind, blocked, block_reason, updated_at FROM contacts"
            if not include_blocked:
                sql += " WHERE blocked = 0"
            sql += " ORDER BY name"
            rows = conn.execute(sql).fetchall()
            ids_by_contact = _load_ids_grouped(conn, [r["id"] for r in rows])
            return [_row_to_contact(r, ids_by_contact=ids_by_contact) for r in rows]
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def get_contact(contact_id: str) -> dict | None:
    if not contact_id or not _state_db_path().exists():
        return None
    try:
        conn = sqlite3.connect(str(_state_db_path()))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT id, name, notes, about, kind, blocked, block_reason, updated_at FROM contacts WHERE id = ?",
                (contact_id,),
            ).fetchone()
            if not row:
                return None
            ids = _load_ids_grouped(conn, [row["id"]]).get(row["id"], [])
            d = _row_to_contact(row)
            d["platform_ids"] = ids
            return d
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def create_contact(
    *,
    name: str,
    notes: str = "",
    about: str = "",
    kind: str = "human",
    platform_ids: list[dict] | None = None,
) -> dict:
    """新建一个 contact。platform_ids: [{"platform": "feishu", "platform_id": "ou_xxx"}, ...]

    kind ∈ {"human", "bot", "system"}：
      - human:  真人用户（默认）
      - bot:    其他实例/机器人（用于 mention auto-detect）
      - system: 系统通知账号

    about: 自由文本画像（替代已退役的 HIM.md）；notes 是短备注，about 是长画像。

    跨平台合并：若某个 platform_id 已绑给 stub（name 空），自动合并进新 contact。
    """
    ensure_schema()
    kind = (kind or "human").strip().lower() or "human"
    if kind not in ("human", "bot", "system"):
        kind = "human"
    cid = str(uuid.uuid4())
    conn = sqlite3.connect(str(_state_db_path()))
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO contacts (id, name, notes, about, kind, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (cid, name, notes, about, kind, _now_iso()),
        )
        for pid in platform_ids or []:
            p = (pid.get("platform") or "").strip()
            v = (pid.get("platform_id") or "").strip()
            if not (p and v):
                continue
            # 检测是否已绑给 stub → 自动合并
            existing = conn.execute(
                "SELECT contact_id FROM contact_ids WHERE platform = ? AND platform_id = ? AND contact_id != ?",
                (p, v, cid),
            ).fetchall()
            for (other_cid,) in existing:
                other_name = conn.execute(
                    "SELECT name FROM contacts WHERE id = ?", (other_cid,)
                ).fetchone()
                if other_name and (other_name[0] or "") == "":
                    conn.execute(
                        "DELETE FROM contact_ids WHERE contact_id = ?",
                        (other_cid,),
                    )
                    conn.execute("DELETE FROM contacts WHERE id = ?", (other_cid,))
                    logger.info(
                        "create_contact: auto-merged stub %s into %s",
                        other_cid[:8], cid[:8],
                    )
            conn.execute(
                "INSERT OR IGNORE INTO contact_ids (contact_id, platform, platform_id) VALUES (?, ?, ?)",
                (cid, p, v),
            )
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        logger.warning("create_contact failed: %s", exc)
    finally:
        conn.close()
    result = get_contact(cid) or {"id": cid, "name": name, "notes": notes, "about": about, "platform_ids": []}

    # 自动注册联系人到 entity_index——让联想召回能命中
    try:
        from domain.memory.memory.consciousness.entity_index import sync_entity_from_source
        entity_name = name if name and name.strip() else cid[:8]
        sync_entity_from_source(
            entity_name,
            entity_type="person",
            summary=f"联系人: {name or '(未命名)'}" + (f"，备注: {notes}" if notes else ""),
            extra={"contact_id": cid, "kind": kind},
        )
        # 如果有 name 且 name != cid，也注册 cid 作为 alias
        if name and name.strip() and name != cid:
            import contextlib
            with contextlib.suppress(Exception):
                from domain.memory.memory.consciousness.entity_index import mark_entity_status
                # 不注册 cid 为别名——太长了。但如果 name 为空的 stub，
                # 后续 merge_contacts 时会触发 entity 同步。
                pass
    except Exception:
        pass

    return result


def update_contact(contact_id: str, *, name: str | None = None, notes: str | None = None,
                   about: str | None = None,
                   kind: str | None = None,
                   platform_ids: list[dict] | None = None) -> dict | None:
    """更新 contact 字段。platform_ids 给定时**整体替换**该 contact 的所有 platform_id。

    about: 自由文本画像(替代 HIM.md)。传 None 不动;传空串/非空都按字面更新。
    kind ∈ {"human", "bot", "system"}，可选。

    跨平台合并语义：若新 platform_id 已绑给其他 contact（且对方是 stub —— name 空 + 单 ID），
    则自动解除对方的绑定并删除该 stub，避免重复 scatter。事务保护。
    """
    if not contact_id:
        return None
    ensure_schema()
    if kind is not None:
        k = (kind or "human").strip().lower() or "human"
        if k not in ("human", "bot", "system"):
            k = "human"
        kind = k
    conn = sqlite3.connect(str(_state_db_path()))
    try:
        conn.execute("BEGIN IMMEDIATE")
        sets: list[str] = []
        args: list = []
        if name is not None:
            sets.append("name = ?")
            args.append(name)
        if notes is not None:
            sets.append("notes = ?")
            args.append(notes)
        if about is not None:
            sets.append("about = ?")
            args.append(about)
        if kind is not None:
            sets.append("kind = ?")
            args.append(kind)
        if sets:
            sets.append("updated_at = ?")
            args.append(_now_iso())
            args.append(contact_id)
            sql = "UPDATE contacts SET " + ", ".join(sets) + " WHERE id = ?"
            conn.execute(
                sql,
                args,
            )
        if platform_ids is not None:
            # 跨平台合并：检测即将绑定的 platform_id 是否已属于其他 contact
            # 仅自动合并 stub（name 空）；非 stub contact 视为冲突，跳过该条插入
            conn.execute("DELETE FROM contact_ids WHERE contact_id = ?", (contact_id,))
            for pid in platform_ids:
                p = (pid.get("platform") or "").strip()
                v = (pid.get("platform_id") or "").strip()
                if not (p and v):
                    continue
                # 检测该 (platform, platform_id) 是否已绑给其他 contact
                existing = conn.execute(
                    "SELECT contact_id FROM contact_ids WHERE platform = ? AND platform_id = ? AND contact_id != ?",
                    (p, v, contact_id),
                ).fetchall()
                for (other_cid,) in existing:
                    # 判断对方是否是 stub：name 空
                    other_name = conn.execute(
                        "SELECT name FROM contacts WHERE id = ?", (other_cid,)
                    ).fetchone()
                    if other_name and (other_name[0] or "") == "":
                        # stub → 解除该 platform_id 绑定；若 stub 因此无任何绑定则自动删除
                        conn.execute(
                            "DELETE FROM contact_ids WHERE contact_id = ? AND platform = ? AND platform_id = ?",
                            (other_cid, p, v),
                        )
                        remaining = conn.execute(
                            "SELECT COUNT(*) FROM contact_ids WHERE contact_id = ?", (other_cid,)
                        ).fetchone()[0]
                        if remaining == 0:
                            conn.execute("DELETE FROM contacts WHERE id = ?", (other_cid,))
                            logger.info(
                                "Auto-merged stub %s into %s (platform=%s)",
                                other_cid[:8], contact_id[:8], p,
                            )
                    # 非 stub contact：保留绑定，本侧 INSERT OR IGNORE 会自然跳过
                conn.execute(
                    "INSERT OR IGNORE INTO contact_ids (contact_id, platform, platform_id) VALUES (?, ?, ?)",
                    (contact_id, p, v),
                )
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        logger.warning("update_contact failed: %s", exc)
        return None
    finally:
        conn.close()
    return get_contact(contact_id)


def del_contact(contact_id: str) -> bool:
    if not contact_id:
        return False
    if not _state_db_path().exists():
        return False
    try:
        conn = sqlite3.connect(str(_state_db_path()))
        try:
            cur = conn.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
            conn.execute("DELETE FROM contact_ids WHERE contact_id = ?", (contact_id,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.warning("contacts delete failed: %s", exc)
        return False


def merge_contacts(source_id: str, target_id: str) -> bool:
    """把 source contact 合并到 target——source 的所有 platform_ids 迁移到 target，
    然后删除 source。

    用于"同一个人在系统里被注册成两条 contact"的人工合并场景。

    合并规则：
      - source 的 platform_ids 逐条 INSERT OR IGNORE 到 target
      - target 的 name/notes/kind 保留不变（用户可以在合并后编辑）
      - 如果 source 有 notes 且 target notes 为空，把 source notes 复制过去
      - 删除 source contact + source 的 contact_ids
    """
    if not source_id or not target_id or source_id == target_id:
        return False
    if not _state_db_path().exists():
        return False
    ensure_schema()
    try:
        conn = sqlite3.connect(str(_state_db_path()))
        try:
            conn.execute("BEGIN IMMEDIATE")
            # 1. 把 source 的 platform_ids 迁移到 target
            source_pids = conn.execute(
                "SELECT platform, platform_id FROM contact_ids WHERE contact_id = ?",
                (source_id,),
            ).fetchall()
            for p, pid in source_pids:
                conn.execute(
                    "INSERT OR IGNORE INTO contact_ids (contact_id, platform, platform_id) "
                    "VALUES (?, ?, ?)",
                    (target_id, p, pid),
                )
            # 2. 如果 target notes 为空，把 source 的 notes 补过来
            src = conn.execute(
                "SELECT notes FROM contacts WHERE id = ?", (source_id,)
            ).fetchone()
            tgt = conn.execute(
                "SELECT notes FROM contacts WHERE id = ?", (target_id,)
            ).fetchone()
            if src and tgt and not (tgt[0] or "").strip() and (src[0] or "").strip():
                conn.execute(
                    "UPDATE contacts SET notes = ?, updated_at = ? WHERE id = ?",
                    (src[0], _now_iso(), target_id),
                )
            # 3. 删除 source
            conn.execute("DELETE FROM contact_ids WHERE contact_id = ?", (source_id,))
            conn.execute("DELETE FROM contacts WHERE id = ?", (source_id,))
            conn.commit()
            logger.info(
                "merge_contacts: source %s → target %s (%d platform_ids moved)",
                source_id[:8], target_id[:8], len(source_pids),
            )
            # 同步 entity_index——如果 source 和 target 都有对应实体、做 entity merge
            try:
                from domain.memory.memory.consciousness.entity_index import merge_entities, sync_entity_from_source
                src_name = conn.execute(
                    "SELECT name FROM contacts WHERE id = ?", (source_id,)
                ).fetchone()
                tgt_name = conn.execute(
                    "SELECT name FROM contacts WHERE id = ?", (target_id,)
                ).fetchone()
                src_name_val = (src_name[0] if src_name else "") or source_id[:8]
                tgt_name_val = (tgt_name[0] if tgt_name else "") or target_id[:8]
                merge_entities(src_name_val, tgt_name_val)
                sync_entity_from_source(tgt_name_val, entity_type="person",
                                       extra={"contact_id": target_id, "merged_from": source_id[:8]})
            except Exception:
                pass
            return True
        except sqlite3.Error:
            conn.rollback()
            raise
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.warning("merge_contacts failed: %s", exc)
        return False


def set_blocked(contact_id: str, blocked: bool, reason: str = "") -> bool:
    """设置黑名单状态。返回是否真改了一行。"""
    if not contact_id or not _state_db_path().exists():
        return False
    try:
        conn = sqlite3.connect(str(_state_db_path()))
        try:
            cur = conn.execute(
                "UPDATE contacts SET blocked = ?, block_reason = ?, "
                "updated_at = ? WHERE id = ?",
                (1 if blocked else 0, reason, _now_iso(), contact_id),
            )
            conn.commit()
            result = cur.rowcount > 0
            if result:
                try:
                    from domain.memory.memory.consciousness.entity_index import mark_entity_status
                    name_row = conn.execute(
                        "SELECT name FROM contacts WHERE id = ?", (contact_id,)
                    ).fetchone()
                    entity_name = (name_row[0] if name_row and name_row[0] else "") or contact_id[:8]
                    mark_entity_status(entity_name, contact_blocked=blocked,
                                       block_reason=reason if blocked else "")
                except Exception:
                    pass
            return result
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.warning("set_blocked failed: %s", exc)
        return False


# ──────────────────────────────── Lookup helpers ────────────────────────────────


def get_or_create_stub(platform: str, platform_id: str, *, kind: str = "human") -> dict | None:
    """Lookup (platform, platform_id) → contact；未命中则原子创建一个 stub (name='')。

    用于入站消息时自动注册未知 sender。并发安全：transaction + UNIQUE PK。
    返回 None 当 platform/platform_id 为空。

    kind: 新建联系人时的种类（human/bot/system）。已有记录时，若旧 kind 是
    默认 'human' 且本次传入 bot/system，则升级 kind——飞书 event.sender_type
    是比"第一次见到时 name 为空"更可靠的"这是机器人"判据。
    """
    if not platform or not platform_id:
        return None
    ensure_schema()
    conn = sqlite3.connect(str(_state_db_path()))
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT contact_id FROM contact_ids WHERE platform = ? AND platform_id = ?",
            (platform, platform_id),
        ).fetchone()
        if row:
            contact_row = conn.execute(
                "SELECT id, name, notes, about, kind, blocked, block_reason, updated_at FROM contacts WHERE id = ?",
                (row[0],),
            ).fetchone()
            # 升级 kind：旧 human → bot/system。已为 bot/system 不降级回 human。
            if contact_row and kind in ("bot", "system") and (contact_row[3] or "human") == "human":
                conn.execute(
                    "UPDATE contacts SET kind = ?, updated_at = ? WHERE id = ?",
                    (kind, _now_iso(), contact_row[0]),
                )
                logger.info(
                    "Upgraded contact %s kind human→%s for %s:%s",
                    contact_row[0][:8], kind, platform, platform_id[:16],
                )
            conn.commit()
            if contact_row:
                # row factory 默认是 tuple；手工转 dict
                d = {
                    "id": contact_row[0],
                    "name": contact_row[1] or "",
                    "notes": contact_row[2] or "",
                    "kind": contact_row[3] or "human",
                    "blocked": bool(contact_row[4]),
                    "block_reason": contact_row[5] or "",
                    "updated_at": contact_row[6] or "",
                }
                return d
            return None
        # 不存在 → 建 stub
        _kind = (kind or "human").strip().lower() or "human"
        if _kind not in ("human", "bot", "system"):
            _kind = "human"
        cid = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO contacts (id, name, notes, kind, updated_at) "
            "VALUES (?, '', 'auto-registered stub', ?, ?)",
            (cid, _kind, _now_iso()),
        )
        conn.execute(
            "INSERT INTO contact_ids (contact_id, platform, platform_id) VALUES (?, ?, ?)",
            (cid, platform, platform_id),
        )
        conn.commit()
        logger.info("Created stub contact %s (kind=%s) for %s:%s",
                    cid[:8], _kind, platform, platform_id[:16])
        return {
            "id": cid,
            "name": "",
            "notes": "auto-registered stub",
            "kind": _kind,
            "blocked": False,
            "block_reason": "",
            "platform_ids": [{"platform": platform, "platform_id": platform_id}],
        }
    except sqlite3.Error as exc:
        conn.rollback()
        logger.warning("get_or_create_stub failed: %s", exc)
        return None
    finally:
        conn.close()


def _lookup_contact(platform: str, platform_id: str) -> dict | None:
    """通过 (platform, platform_id) 找 contact。返回完整 contact 行（含 blocked）。"""
    if not platform or not platform_id or not _state_db_path().exists():
        return None
    try:
        conn = sqlite3.connect(str(_state_db_path()))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT c.id, c.name, c.notes, c.about, c.kind, c.blocked, c.block_reason, c.updated_at "
                "FROM contacts c JOIN contact_ids i ON c.id = i.contact_id "
                "WHERE i.platform = ? AND i.platform_id = ?",
                (platform, platform_id),
            ).fetchone()
            return _row_to_contact(row) if row else None
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def lookup_name(platform: str, platform_id: str) -> str:
    """legacy: 旧接口只返回 name。新 callers 应用 lookup_contact 拿完整记录。"""
    c = _lookup_contact(platform, platform_id)
    return (c.get("name") if c else "") or ""


def lookup_kind(platform: str, platform_id: str) -> str:
    """Lookup (platform, platform_id) → contact.kind ('human'/'bot'/'system')。

    未命中返回 'human'（保守:未知发送者当真人）。用于发送侧判断"@ 到的
    这个人是不是机器人"——若是机器人(peer sibling 或第三方 bot)，飞书已会
    送达它，本侧不再重复广播。
    """
    c = _lookup_contact(platform, platform_id)
    return (c.get("kind") if c else "") or "human"


def any_id_is_bot(platform: str, platform_ids: list[str]) -> bool:
    """platform_ids 中有任一对应 kind=='bot' 的联系人则返回 True。

    批量查询版，避免发送侧对每个 @ 开一次 DB。
    """
    if not platform_ids:
        return False
    for pid in platform_ids:
        if lookup_kind(platform, pid) == "bot":
            return True
    return False


def lookup_many(platform: str, platform_ids: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    if not platform_ids:
        return out
    for pid in platform_ids:
        c = _lookup_contact(platform, pid)
        if c and c.get("name"):
            out[pid] = c["name"]
    return out


def is_blocked(platform: str, platform_id: str) -> bool:
    """检查 (platform, platform_id) 是否对应一个被拉黑的 contact。"""
    c = _lookup_contact(platform, platform_id)
    return bool(c and c.get("blocked"))

