"""P4 — 认知演化生命周期的持久层 / hygiene-facing API。

把 cognition.py 里的纯内存跃迁(on_*/promote/supersede/cluster_born/revise)
持久化到 chunks 表 + 同时维护 entity_index。

调用方:
  - memory_hygiene skill 调本模块的 API(promote_one / supersede_one /
    cluster_born_persist / apply_signal_batch)做"模型驱动"操作,
    skill SKILL.md 自己负责告诉模型"调哪个工具、怎么调"
  - 自动驱动器(写入 access/reference 命中时调 apply_signal_batch 的简单形式)

不直接修改 memory_hygiene/SKILL.md — 那是 prompt 模板, 用户的范畴。
本模块的能力将来由 action_tools 或 skill 工具暴露给模型。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Literal

from domain.memory.memory.recall.unified.cognition import (
    CognitionState,
    cluster_born,
    on_access,
    on_falsified,
    on_reference,
    on_verified,
    promote,
    supersede,
    revise,
)
from domain.memory.memory.recall.unified.slice import Slice, baselines_for_source
from domain.memory.memory.recall.vector import _get_db

logger = logging.getLogger("domain.memory.recall.unified.cognition_store")

SignalKind = Literal["access", "reference", "verified", "falsified"]


# ───────────────────── slice 持久化基础 ─────────────────────

def _persist_slice(db, slice: Slice) -> int | None:
    """把 Slice 写回 chunks,优先 UPDATE 已存在行(保留 id 不变),
    否则 INSERT 新行。返回 chunk_id。

    用 INSERT OR REPLACE 会因为 UNIQUE(source, chunk_hash) 替换行并重新分配 id,
    对认知演化 / supersede / derived_from 链是破坏性的(id 会变 → 链断)。
    所以这里显式 UPDATE 优先。
    """
    if not slice.chunk_hash:
        slice.chunk_hash = hashlib.md5(
            f"{slice.source}:{slice.body[:200]}".encode("utf-8")
        ).hexdigest()
    row = slice.to_row()
    # 先看行存不存在
    existing = db.execute(
        "SELECT id FROM chunks WHERE source=? AND chunk_hash=?",
        (slice.source, slice.chunk_hash),
    ).fetchone()
    try:
        if existing:
            # UPDATE 保留 id
            cid = existing["id"]
            slice.id = cid  # 回填到对象
            cols = list(row.keys())
            set_clause = ", ".join([f"{c}=?" for c in cols])
            db.execute(
                f"UPDATE chunks SET {set_clause} WHERE id=?",
                [row[c] for c in cols] + [cid],
            )
            return cid
        else:
            # INSERT 新行
            cols = list(row.keys())
            placeholders = ",".join("?" * len(cols))
            col_list = ",".join(cols)
            cur = db.execute(
                f"INSERT INTO chunks ({col_list}) VALUES ({placeholders})",
                [row[c] for c in cols],
            )
            return cur.lastrowid
    except Exception as e:
        logger.warning("persist_slice failed (%s/%s): %s", slice.source, slice.chunk_hash, e)
        return None


def load_slice_by_id(chunk_id: int) -> Slice | None:
    """根据 chunk_id 加载 Slice。失败时返回 None。"""
    try:
        db = _get_db()
        try:
            row = db.execute(
                "SELECT * FROM chunks WHERE id=?", (chunk_id,)
            ).fetchone()
            if not row:
                return None
            return Slice.from_row(row)
        finally:
            db.close()
    except Exception as e:
        logger.warning("load_slice_by_id(%s) failed: %s", chunk_id, e)
        return None


# ───────────────────── Hygiene-facing API ─────────────────────

def apply_signal(
    chunk_id: int,
    signal: SignalKind,
    *,
    reason: str = "",
) -> dict[str, Any]:
    """把一条信号应用到某个 chunk(自动驱动器入口)。
    返回 {ok, chunk_id, changes}。
    """
    slice = load_slice_by_id(chunk_id)
    if slice is None:
        return {"ok": False, "error": "chunk not found"}
    before_state = slice.cognition_state
    if signal == "access":
        on_access(slice)
    elif signal == "reference":
        on_reference(slice)
    elif signal == "verified":
        on_verified(slice)
    elif signal == "falsified":
        on_falsified(slice, reason=reason)
    else:
        return {"ok": False, "error": f"unknown signal {signal}"}
    # 持久化
    try:
        db = _get_db()
        try:
            _persist_slice(db, slice)
            db.commit()
        finally:
            db.close()
    except Exception as e:
        return {"ok": False, "error": f"persist failed: {e}"}
    return {
        "ok": True,
        "chunk_id": chunk_id,
        "changes": {"cognition_state": (before_state, slice.cognition_state)},
    }


def promote_one(
    experience_chunk_id: int,
    *,
    summary: str,
    entity_name: str | None = None,
) -> dict[str, Any]:
    """Hygiene 调:把某经历切片提纯为认知。
    返回 {ok, new_cognition_chunk_id, derived_from}。

    闸门二(2026-07-17): promote 前查重 — 用 summary 做语义召回,
    看已有 cognition 里有没有高度相似的。有的话不无脑新建 — 返回提示给模型,
    让它自己决定是 supersede 还是 verified。
    """
    exp = load_slice_by_id(experience_chunk_id)
    if exp is None:
        return {"ok": False, "error": "experience chunk not found"}
    if exp.phase != "experience":
        return {"ok": False, "error": f"chunk {experience_chunk_id} is not experience "
                                       f"(phase={exp.phase})"}

    # ── 闸门二: 查已有认知相似度 ──
    # 用 recall_structured 限定 cognition source, 看前 5 条里有没有 > 阈的。
    try:
        from domain.memory.memory.recall.vector import recall_structured
        existing = recall_structured(
            summary,
            max_total_chars=600,
            sources=["rules", "lessons", "knowledge", "self_knowledge"],
            include_obsolete=False,  # 只查 active 认知; 死信的已经不该复用
        )
    except Exception as e:
        logger.debug("promote dedup check failed (will proceed): %s", e)
        existing = []

    # 判相似度: 直接用 recall 返回的 score(cosine × weight ≥ threshold 已过滤过)
    # score >= 0.7 我们认为"语义相似",值得让模型判断
    DEDUP_THRESHOLD = 0.7
    similar: list[dict] = []
    for r in existing:
        if r.get("score", 0.0) >= DEDUP_THRESHOLD:
            similar.append(r)

    if similar:
        # 不直接写新认知 — 告诉调用者已有相似认知, 建议走 supersede 或 verified
        return {
            "ok": False,
            "skip_reason": "duplicate_cognition",
            "similar_cognitions": [
                {
                    "chunk_id": r.get("chunk_id"),
                    "source": r.get("source"),
                    "score": round(r.get("score", 0), 3),
                    "preview": (r.get("text") or "")[:120],
                }
                for r in similar[:5]
            ],
            "advice": (
                "已有高度相似的认知。如果是同一意思 → 用 signal_memory "
                "给已有认知打 verified 强化它(不新建)。如果需要更新措辞 → "
                "用 supersede_memory 取代已有认知。如果确实是完全不相关的新领域 → "
                "修改 summary 重新调 promote_memory。"
            ),
        }

    # ── 无重复: 正常走 promote 路径 ──
    entity_links = [entity_name] if entity_name else list(exp.entity_links)
    new_cog = promote(exp, summary=summary, derived_from_ids=[experience_chunk_id],
                      entity_links=entity_links)
    try:
        db = _get_db()
        try:
            new_id = _persist_slice(db, new_cog)
            db.commit()
            # 同步到 entity_index(让 entity 卡更新为 concept)
            if entity_name:
                _sync_entity_index_for_promoted(entity_name, summary)
            return {"ok": True, "new_cognition_chunk_id": new_id,
                    "derived_from": [experience_chunk_id]}
        finally:
            db.close()
    except Exception as e:
        logger.warning("promote_one failed: %s", e)
        return {"ok": False, "error": str(e)}


def supersede_one(
    old_chunk_id: int,
    new_body: str,
    *,
    new_authority: float = 0.8,
    entity_name: str | None = None,
) -> dict[str, Any]:
    """Hygiene 调:用一份新 body 取代某个老认知。
    返回 {ok, old_id, new_id}。老 row 保留,带 supersede_by + replaced state。
    """
    old = load_slice_by_id(old_chunk_id)
    if old is None:
        return {"ok": False, "error": "old chunk not found"}
    if old.phase != "cognition":
        return {"ok": False, "error": "only cognition slices can be superseded"}

    new = Slice(
        source=old.source,
        body=new_body,
        phase="cognition",
        source_kind=old.source_kind or "rule",
        authority=new_authority,
        permanence=max(old.permanence, 0.85),
        freshness=1.0,
        activation=0.0,
        verification=0.0,
        cognition_state=CognitionState.ACTIVE.value,
        entity_links=([entity_name] if entity_name else list(old.entity_links)),
        attention_tokens=([entity_name] if entity_name else list(old.attention_tokens)),
        provenance=f"supersede from {old_chunk_id}",
    )
    try:
        db = _get_db()
        try:
            new_id = _persist_slice(db, new)
            new.id = new_id
            supersede(old, new)
            _persist_slice(db, old)
            _persist_slice(db, new)  # 再写一次带双向链的 new
            db.commit()
            return {"ok": True, "old_id": old_chunk_id, "new_id": new_id}
        finally:
            db.close()
    except Exception as e:
        logger.warning("supersede_one failed: %s", e)
        return {"ok": False, "error": str(e)}


def revise_one(
    chunk_id: int,
    *,
    new_body: str,
    version_log: str = "",
) -> dict[str, Any]:
    """Hygiene 调:原地修订某认知 body。"""
    slice = load_slice_by_id(chunk_id)
    if slice is None:
        return {"ok": False, "error": "chunk not found"}
    revise(slice, new_body=new_body, version_log=version_log)
    try:
        db = _get_db()
        try:
            _persist_slice(db, slice)
            db.commit()
        finally:
            db.close()
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "chunk_id": chunk_id}


def cluster_born_persist(
    member_chunk_ids: list[int],
    *,
    summary: str,
    entity_name: str | None = None,
) -> dict[str, Any]:
    """Hygiene 调:从多认知聚出一个更高阶新认知。返回 {ok, new_chunk_id, derived_from}。
    若 summary 为空 → 返回 ok=False,调用方应保留原样 + 标记待复审(§FR-406)。
    """
    members = [load_slice_by_id(cid) for cid in member_chunk_ids]
    members = [m for m in members if m is not None]
    if not members:
        return {"ok": False, "error": "no valid members"}
    higher = cluster_born(members, summary=summary,
                          entity_links=[entity_name] if entity_name else None)
    if higher is None:
        return {"ok": False, "error": "cluster_born returned None (empty summary/members)"}
    try:
        db = _get_db()
        try:
            new_id = _persist_slice(db, higher)
            db.commit()
            if entity_name:
                _sync_entity_index_for_promoted(entity_name, summary,
                                                concept_type="higher_cognition")
            return {"ok": True, "new_chunk_id": new_id,
                    "derived_from": [m.id for m in members if m.id is not None]}
        finally:
            db.close()
    except Exception as e:
        logger.warning("cluster_born_persist failed: %s", e)
        return {"ok": False, "error": str(e)}


# ───────────────────── 辅助 ─────────────────────

def _sync_entity_index_for_promoted(
    entity_name: str,
    summary: str,
    *,
    concept_type: str = "concept",
) -> None:
    """promote / cluster_born 后,把新认知同步到 entity_index(让模型在 sense_entity 时
    能看到。失败不阻塞 hygiene 流程。)
    """
    try:
        from domain.memory.memory.consciousness.entity_index import sync_entity_from_source
        sync_entity_from_source(
            entity_name,
            entity_type=concept_type,
            summary=summary[:200],
        )
    except Exception as e:
        logger.debug("sync entity_index failed for %r: %s", entity_name, e)


__all__ = [
    "apply_signal",
    "promote_one",
    "supersede_one",
    "revise_one",
    "cluster_born_persist",
    "load_slice_by_id",
]
