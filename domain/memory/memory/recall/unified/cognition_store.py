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


def _cog_audit(action: str, chunk_id: int | None, **fields) -> None:
    """统一的认知演化日志条目 — 让 evidence/challenge/supersede/add/delete
    全可被 grep / `digital-life logs` 监控, 用户能判断"强化/质疑有没有在跑".

    设计: 单行紧凑格式, 含 [cognition_audit] tag 便于过滤; 把数字变化显式写出来
    (建议 evidence:0→1 这种, 用户直接读出来)。

    日志等级 policy (2026-07-24 复盘):
      · access 信号是热信号 (每 wake 多次, 噪音大) → DEBUG 级别 (不打 INFO)
      · verified/falsified (evidence/challenge 真正变化) → INFO
      · add/supersede/delete (生命周期事件) → INFO
    """
    if chunk_id is None:
        cid_repr = "?"
    else:
        cid_repr = f"#{chunk_id}"
    # 过滤 None, 控制长度
    parts = []
    for k, v in fields.items():
        if v is None:
            continue
        s = str(v)
        if len(s) > 80:
            s = s[:77] + "..."
        parts.append(f"{k}={s}")
    tail = " ".join(parts)

    # access 这类高频热信号 → DEBUG 不污染 INFO 日志
    if action == "signal" and fields.get("kind") == "access":
        logger.debug("[cognition_audit] %s %s %s", action, cid_repr, tail)
        return
    logger.info("[cognition_audit] %s %s %s", action, cid_repr, tail)


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
    before_evidence = slice.evidence_count
    before_challenge = slice.challenge_count
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
    # 全局审计日志 — 用户能直接 grep "[cognition_audit]" 看强化/质疑有没有在跑
    state_changed = before_state != slice.cognition_state
    _cog_audit("signal",
        chunk_id,
        kind=signal,
        state=f"{before_state or '∅'}→{slice.cognition_state or '∅'}" if state_changed else slice.cognition_state,
        evidence=f"{before_evidence}→{slice.evidence_count}" if before_evidence != slice.evidence_count else None,
        challenge=f"{before_challenge}→{slice.challenge_count}" if before_challenge != slice.challenge_count else None,
        authority=f"{slice.authority:.2f}" if signal in ("verified","falsified") else None,
        reason=reason[:50] if reason else None,
    )
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
    payload: dict | None = None,
) -> dict[str, Any]:
    """Hygiene 调:把某经历切片提纯为认知。
    返回 {ok, new_cognition_chunk_id, derived_from}。

    V3 (2026-07-23 review): 修了 3 个 BUG
      A) DEDUP_THRESHOLD 比的是 weighted score, 不是 raw cosine (1.5×weight 假阳)
      B) source 列表 `["rules","lessons","knowledge","self_knowledge"]` 用复数+废弃源
      C) catch-all 多主题规则 chunk (≈400字符, 多个编号/日期) 没特殊处理, 放大假阳

    新策略:
      · 提供 payload.cog_key → 走 V2 精确路径(短路 cos 路径)
      · 否则用 lookup_cognition_similarities() 拿 raw cosine 阈值分场景判 dup
      · catch-all chunk 永远不能判 duplicate (最多 weak_link)
      · dedup 失败也不返回 ok=False, 而是返回 ok=True + "potential_duplication_warning"
        让调用者继续 promote 而不被拦 (老路径误拦截是 Alpha 反馈的最大痛点)
    """
    exp = load_slice_by_id(experience_chunk_id)
    if exp is None:
        return {"ok": False, "error": "experience chunk not found"}
    if exp.phase != "experience":
        return {"ok": False, "error": f"chunk {experience_chunk_id} is not experience "
                                       f"(phase={exp.phase})"}

    # ── V3-A/V3-B: 改为 raw cosine phase-based 召回 ──
    # 不再用 recall_structured(weighted)+source 名单过滤, 直接 lookup_cognition_similarities
    # 拿 raw cos, 再分场景判 dup. 这是一次性结构性修复 BUG A/B.
    duplicate_warning: str | None = None
    similar_cognitions: list[dict] = []

    # 1. 先尝试 V3 精确 payload key path (BUG B short-circuit)
    cog_key: str | None = None
    if payload and isinstance(payload, dict):
        key = payload.get("key")
        if isinstance(key, str) and key.strip():
            cog_key = key.strip()
            # 精确 key 查重, 复用 add_cognition_direct 的精确路径
            db_pre = _get_db()
            try:
                dup_check = _check_cog_key_conflict(db_pre, cog_key, payload)
            finally:
                db_pre.close()
            if dup_check["skipped_exact_duplicate"]:
                # 同 key 同 value → 不 promote, 直接告知 caller
                return {
                    "ok": True,
                    "skipped_exact_duplicate": True,
                    "chunk_id": dup_check["chunk_id"],
                    "reason": f"已有同 key({cog_key}) 同 value 的活跃认知 #{dup_check['chunk_id']}, promote 跳过.",
                    "cog_key": cog_key,
                }
            # 否则保留 conflict_with 信息, 仍允许写入 (类似 add_cognition_direct 的语义)
            if dup_check.get("conflict_with"):
                duplicate_warning = (
                    f"⚠️ cog_key({cog_key}) 已有不同 value 的认知 — "
                    f"已 promote 但建议调 supersede_memory 处理冲突: {dup_check['conflict_with']}"
                )

    # 2. raw cosine dedup (BUG A + catch-all BUG C)
    if not cog_key:
        try:
            from domain.memory.memory.recall.vector import _embed_single
            q_emb = _embed_single(summary[:512])
        except Exception as e:
            logger.debug("promote embed failed (skip dedup): %s", e)
            q_emb = None
        if q_emb:
            try:
                from domain.memory.memory.recall.vector import lookup_cognition_similarities
                sim_list = lookup_cognition_similarities(q_emb, limit=5)
            except Exception as e:
                logger.debug("promote lookup_cognition_similarities failed: %s", e)
                sim_list = []
            for r in sim_list:
                cos = r.get("raw_cos", 0)
                is_ca = r.get("is_catch_all", False)
                # 分场景阈值: rule/lesson 严 (重复要求高), knowledge/insight 宽松
                source = r.get("source", "")
                if source in ("rule", "rule"):
                    threshold = 0.85
                elif source in ("lesson", "fact"):
                    threshold = 0.85
                elif source in ("knowledge", "project"):
                    threshold = 0.80
                else:  # insight / rule_variants
                    threshold = 0.80
                # catch-all 禁止判 duplicate (BUG C)
                if is_ca and cos < 0.95:
                    similar_cognitions.append({
                        "chunk_id": r["chunk_id"],
                        "raw_cos": round(cos, 3),
                        "preview": r["text"][:120],
                        "dedup_action": "weak_link",
                        "reason": f"catch-all 多主题规则 (len={len(r['text'])}, 即便 cos 高也不拦你)",
                    })
                    continue
                if cos >= 0.95:
                    # 极高 cos → 标 duplicate 但仍允许写入 (BUG 1 修正: 不再 ok=False 拦截)
                    similar_cognitions.append({
                        "chunk_id": r["chunk_id"],
                        "raw_cos": round(cos, 3),
                        "preview": r["text"][:120],
                        "dedup_action": "duplicate",
                    })
                    duplicate_warning = (
                        f"已有认知 #{r['chunk_id']} 与你的 promote 内容 raw_cos={cos:.3f} 极相似. "
                        f"仍写入 — 若是覆盖请调 supersede_memory({r['chunk_id']}, \"...\"). 若同义不新建, 用 signal_memory 加固它. "
                        f"若不相关, 忽略此提醒."
                    )
                elif cos >= threshold:
                    similar_cognitions.append({
                        "chunk_id": r["chunk_id"],
                        "raw_cos": round(cos, 3),
                        "preview": r["text"][:120],
                        "dedup_action": "weak_link",
                    })

    # ── V3 改动: dedup 仅作 warning, 不再拦截写入 (Alpha 反馈: 被误拦的痛点) ──
    entity_links = [entity_name] if entity_name else list(exp.entity_links)
    new_cog = promote(exp, summary=summary, derived_from_ids=[experience_chunk_id],
                      entity_links=entity_links)
    if cog_key:
        new_cog.cog_key = cog_key
    if payload:
        new_cog.payload = payload
    try:
        db = _get_db()
        try:
            new_id = _persist_slice(db, new_cog)
            # 写入 embedding (因为 _persist_slice 不写 embedding)
            from domain.memory.memory.recall.vector import _embed_single, _embedding_to_blob
            emb = _embed_single(summary[:512])
            if emb:
                db.execute("UPDATE chunks SET embedding=? WHERE id=?",
                           (_embedding_to_blob(emb), new_id))
            db.commit()
            # 同步到 entity_index(让 entity 卡更新为 concept)
            if entity_name:
                _sync_entity_index_for_promoted(entity_name, summary)
            # V3 #2: 如果带 payload key, sync profile derived field
            if cog_key and entity_name:
                try:
                    _sync_profile_derived_field_for_promote(entity_name, cog_key, payload, new_id)
                except Exception as e:
                    logger.debug("profile derived sync failed: %s", e)
            result: dict[str, Any] = {
                "ok": True,
                "new_cognition_chunk_id": new_id,
                "new_chunk_id": new_id,  # 统一 key (V2 一致性)
                "chunk_id": new_id,
                "derived_from": [experience_chunk_id],
            }
            _cog_audit("promote", new_id,
                source=new_cog.source,
                cog_key=new_cog.cog_key,
                state="nascent",  # promote 默认进入 nascent 态
                derived_from=experience_chunk_id,
            )
            if duplicate_warning:
                result["duplicate_warning"] = duplicate_warning
            if similar_cognitions:
                result["similar_cognitions"] = similar_cognitions[:5]
            if cog_key:
                result["cog_key"] = cog_key
            return result
        finally:
            db.close()
    except Exception as e:
        logger.warning("promote_one failed: %s", e)
        return {"ok": False, "error": str(e)}


def _check_cog_key_conflict(db, cog_key: str, payload: dict) -> dict:
    """V3 helper: 同 cog_key 已有认知时, 比较 value —
    同 value 标 skipped_exact_duplicate, 异 value 标 conflict_with。

    被 promote_one 用, 与 add_cognition_direct 内部逻辑保持一致。
    """
    import json as _json
    existing = db.execute(
        "SELECT id, text, payload FROM chunks "
        "WHERE cog_key=? AND phase='cognition' "
        "AND (cognition_state IS NULL OR cognition_state NOT IN ('replaced','archived'))",
        (cog_key,),
    ).fetchall()
    if not existing:
        return {"skipped_exact_duplicate": False, "conflict_with": []}
    new_value_repr = _json.dumps(payload.get("value"),
                                  ensure_ascii=False, sort_keys=True, default=str)
    same_value_ids: list[int] = []
    diff_value_rows: list[dict] = []
    for row in existing:
        try:
            row_payload = _json.loads(row["payload"]) if row["payload"] else {}
        except Exception:
            row_payload = {}
        row_value_repr = _json.dumps(row_payload.get("value"),
                                      ensure_ascii=False, sort_keys=True, default=str)
        if row_value_repr == new_value_repr and row_value_repr != "null":
            same_value_ids.append(row["id"])
        else:
            diff_value_rows.append({
                "chunk_id": row["id"],
                "text": (row["text"] or "")[:80],
                "value": row_payload.get("value"),
            })
    if same_value_ids:
        return {"skipped_exact_duplicate": True, "chunk_id": same_value_ids[0],
                "conflict_with": []}
    return {"skipped_exact_duplicate": False, "conflict_with": diff_value_rows}


def _sync_profile_derived_field_for_promote(
    subject: str, cog_key: str, payload: dict | None, source_chunk_id: int
) -> None:
    """V3 #2: promote 之后的 hook — 把 payload.value 同步到 entity profile.derived_fields."""
    if not payload:
        return
    predicate_full = cog_key.split(":", 1)
    predicate = predicate_full[1] if len(predicate_full) == 2 else cog_key
    value = payload.get("value")
    if value is None:
        return
    try:
        from domain.memory.memory.consciousness.entity_index import sync_profile_derived_field
        sync_profile_derived_field(
            subject=subject, predicate=predicate,
            value=value, cog_key=cog_key, source_chunk_id=source_chunk_id,
        )
    except Exception as e:
        logger.debug("sync_profile_derived_field_for_promote: %s", e)




def supersede_one(
    old_chunk_id: int,
    new_body: str,
    *,
    new_authority: float = 0.8,
    entity_name: str | None = None,
    new_payload: dict | None = None,
) -> dict[str, Any]:
    """Hygiene 调:用一份新 body 取代某个老认知。
    返回 {ok, old_id, new_id}。老 row 保留,带 supersede_by + replaced state。

    V3 (2026-07-23 #2): new_payload 覆盖老 payload.value (用于参数类认知变化).
    """
    old = load_slice_by_id(old_chunk_id)
    if old is None:
        return {"ok": False, "error": "old chunk not found"}
    if old.phase != "cognition":
        return {"ok": False, "error": "only cognition slices can be superseded"}

    # V3 #2: 计算 new payload — 默认继承老的, new_payload 覆盖 value
    merged_payload = dict(old.payload) if old.payload else {}
    if new_payload:
        for k, v in new_payload.items():
            merged_payload[k] = v

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
        # V3 #2: 继承老 cog_key/payload (新版基于老认知演变), new_payload 覆盖 value
        cog_key=old.cog_key,
        payload=merged_payload if merged_payload else None,
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
            # V3 #2: supersede 触发 profile derived field 同步 (用新 chunk_id + 沿用 cog_key/payload)
            # Zero 反馈核心修复: profile 与 cognition 联动同步
            if new.cog_key and new.payload:
                try:
                    subject_pred = new.cog_key.split(":", 1)
                    subject = subject_pred[0].strip() if len(subject_pred) == 2 else ""
                    predicate = subject_pred[1].strip() if len(subject_pred) == 2 else ""
                    if subject and predicate:
                        # 从 new_body 里截取作为 value (老的 payload.value 可能是旧的)
                        # 留旧 payload (它代表本次 supersede 的语义) - 如果是版本号变更, 模型应该
                        # 在调 supersede_memory 时也更新 payload. 这里按现状同步, 不强求模型.
                        from domain.memory.memory.consciousness.entity_index import sync_profile_derived_field
                        sync_profile_derived_field(
                            subject=subject, predicate=predicate,
                            value=new.payload.get("value"),
                            cog_key=new.cog_key, source_chunk_id=new_id,
                        )
                except Exception as e:
                    logger.debug("supersede_one sync_profile_derived_field: %s", e)
            # V3 #4: supersede 邻居检查 — 提醒模型哪些同实体认知可能受影响
            stale_neighbors = []
            try:
                stale_neighbors = check_supersede_neighbors_for_stale_links(old_chunk_id, new_id)
            except Exception as e:
                logger.debug("supersede_one neighbor check: %s", e)
            new_value = new.payload.get("value") if new.payload else None
            _cog_audit("supersede", new_id,
                old_id=old_chunk_id,
                cog_key=new.cog_key,
                new_value=new_value,
                entity_name=entity_name,
                # 关键: 让用户一眼看出"质疑 + 替代"实际发生了
                state_migration=f"#{old_chunk_id}→replaced  #{new_id}→active",
            )
            result = {"ok": True, "old_id": old_chunk_id, "new_id": new_id}
            if stale_neighbors:
                result["stale_neighbors"] = stale_neighbors[:5]
                result["neighbors_hint"] = (
                    f"这次覆盖 #{old_chunk_id} 时, 同 entity_links 还有 {len(stale_neighbors)} 条关联认知. "
                    f"建议: 是否它们也需要 supersede / scope 区分? 看一眼它们的 text 判断."
                )
            return result
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


def add_cognition_direct(
    text: str,
    entity_links: list[str] | None = None,
    source: str = "knowledge",
    payload: dict | None = None,
) -> dict[str, Any]:
    """直接写入一条新认知(不需要从经历 chunk promote)。

    统一入口: add_lesson / update_rules / add_cognition 全部走这里。
    内部:
      1. **V2: 如果 payload 含 key, 先用 cog_key 精确查重/查冲突**
         (高优先级, 比 embedding 路径更早返回)
      2. 用 text 的 embedding 写一条新 chunk(phase=cognition, source=source)
      3. 搜索已存在 cognition → 自动关联 + 重复检测 (V1 路径, 加除 payload 仍执行)
      4. entity_links 持久化到 chunk; payload + cog_key 也持久化

    V2 payload 形态 (可选, 不传走 V1):
      {"key": "subject:predicate", "value": ..., "value_type": "number|text|...",
       "condition": {...}, "scope": "...", "ttl_h": 24}
    相同 key + 相同 value → 直接 skip (纯重复, 不写)
    相同 key + 不同 value → 写入但返回 conflict_with=[...] (让模型自己决定 supersede)

    返回:
      {ok, chunk_id, new_chunk_id, entity_links, similar_cognitions: [...],
       duplicate_warning: str|None,
       cog_key: str|None,
       conflict_with: [{chunk_id, text, value}|...] | None}
    """
    import json as _json
    import time as _time
    from domain.memory.memory.recall.unified.slice import Slice
    from domain.memory.memory.recall.vector import (
        _get_db, _blob_to_embedding, _embedding_to_blob,
        _embed_single, _cosine_sim,
    )

    entity_links = entity_links or []
    now = _time.time()

    # ─── V2: 解析 cog_key + 精确查重 (优先级最高, 比 embedding 路径早) ───
    cog_key: str | None = None
    payload_json: str | None = None
    conflict_with: list[dict] | None = None
    exact_dup_skipped: bool = False
    skipped_existing_id: int | None = None

    if payload and isinstance(payload, dict):
        key = payload.get("key")
        # payload 必须始终序列化(pre + rationale 也要存), 不强求 key
        payload_json = _json.dumps(payload, ensure_ascii=False)
        if isinstance(key, str) and key.strip():
            cog_key = key.strip()
            # 精确查同 key 的活跃 cognition
            db_pre = _get_db()
            try:
                existing = db_pre.execute(
                    "SELECT id, text, payload, cognition_state FROM chunks "
                    "WHERE cog_key=? AND phase='cognition' "
                    "AND (cognition_state IS NULL OR cognition_state NOT IN ('replaced','archived'))",
                    (cog_key,),
                ).fetchall()
                if existing:
                    # 比对 value 字段
                    same_value_rows: list[dict] = []
                    diff_value_rows: list[dict] = []
                    for row in existing:
                        try:
                            row_payload = _json.loads(row["payload"]) if row["payload"] else {}
                        except Exception:
                            row_payload = {}
                        row_value_repr = _json.dumps(row_payload.get("value"),
                                                     ensure_ascii=False, sort_keys=True, default=str)
                        my_value_repr = _json.dumps(payload.get("value"),
                                                    ensure_ascii=False, sort_keys=True, default=str)
                        entry = {
                            "chunk_id": row["id"],
                            "text": row["text"][:80] if row["text"] else "",
                            "value": row_payload.get("value"),
                            "state": row["cognition_state"] or "active",
                        }
                        if row_value_repr == my_value_repr and row_value_repr != "null":
                            same_value_rows.append(entry)
                        else:
                            diff_value_rows.append(entry)

                    if same_value_rows:
                        # 同 key 同 value → 纯重复, 不写入
                        exact_dup_skipped = True
                        skipped_existing_id = same_value_rows[0]["chunk_id"]
                    elif diff_value_rows:
                        # 同 key 不同 value → 是冲突/特例, 写入但提示冲突
                        conflict_with = diff_value_rows
            finally:
                db_pre.close()

    if exact_dup_skipped:
        _cog_audit("add.skip_dup", skipped_existing_id, cog_key=cog_key, reason="same_key_same_value")
        return {
            "ok": True,
            "chunk_id": skipped_existing_id,
            "new_chunk_id": skipped_existing_id,
            "skipped_exact_duplicate": True,
            "reason": f"已有同 key({cog_key}) 同 value 的活跃认知 #{skipped_existing_id}, 跳过写入。",
            "cog_key": cog_key,
            "entity_links": entity_links,
        }

    # 生成 embedding
    try:
        from domain.memory.memory.recall.vector import _embed_single
        emb = _embed_single(text[:512])
    except Exception:
        emb = None
    if not emb:
        return {"ok": False, "reason": "无法生成 embedding(文本太短或 API 失败)"}

    # 构造 Slice + 持久化
    db = _get_db()
    import sqlite3 as _sqlite3
    chunk_hash = f"cog:{hash(text[:100]) & 0xFFFFFFFF:08x}"
    s = Slice(
        id=-1,  # _persist_slice 会 INSERT 新行
        body=text,
        source=source,
        chunk_hash=chunk_hash,
        phase="cognition",
        source_kind="cognition",
        created_at=now,
        session_id="",
        segment_index=0,
        derived_from="[]",
        derive_kind="direct",
        authority=0.7,
        permanence=0.5,
        freshness=1.0,
        activation=0.0,
        verification=0.0,
        evidence_count=0,
        challenge_count=0,
        cognition_state="nascent",
        supersede_by=None,
        entity_links=entity_links,  # Slice.to_row() 会做 json.dumps; 这里给 list, 不要预 dump
        attention_tokens=[],
        provenance=f"direct:{now:.0f}",
        payload=payload,        # V2: dict | None
        cog_key=cog_key,        # V2: str | None
    )
    try:
        new_id = _persist_slice(db, s)
        db.commit()
        if new_id is None:
            db.close()
            return {"ok": False, "reason": "INSERT 认知失败"}
    except Exception:
        db.close()
        raise
    db.close()

    # 写入 embedding (单独 UPDATE, 因为 _persist_slice 不写 embedding)
    db = _get_db()
    try:
        db.execute(
            "UPDATE chunks SET embedding=? WHERE id=?",
            (_embedding_to_blob(emb), new_id),
        )
        db.commit()
    except Exception:
        db.close()
        raise

    # 自动关联: 搜索已存在 cognition
    similar = []
    duplicate_warning = None
    try:
        rows = db.execute(
            "SELECT id, text, entity_links, authority, cognition_state, embedding FROM chunks "
            "WHERE phase='cognition' AND id != ? "
            "AND (cognition_state IS NULL OR cognition_state NOT IN ('replaced','archived')) "
            "AND embedding IS NOT NULL LIMIT 200",
            (new_id,),
        ).fetchall()

        derived_ids = []
        narrative_overlap_candidates: list[dict] = []  # V3 #4: 叙述类冲突候选

        for r in rows:
            r_emb = _blob_to_embedding(r[5]) if len(r) > 5 else None
            if not r_emb:
                continue
            cos = _cosine_sim(emb, r_emb)
            if cos < 0.55:
                continue
            # entity_links 交集
            try:
                r_links = set(_json.loads(r[2] or "[]"))
            except Exception:
                r_links = set()
            q_links = set(entity_links)
            shared_links = list(r_links & q_links)
            links_overlap = bool(shared_links)

            entry = {
                "chunk_id": r[0],
                "cos": round(cos, 4),
                "text": (r[1] or "")[:80],
                "action": None,
                "shared_links": shared_links,  # V3 #4
            }

            if cos > 0.92:
                entry["action"] = "duplicate"
                duplicate_warning = entry
            elif cos > 0.75:
                entry["action"] = "strong_link"
                derived_ids.append(r[0])
            elif links_overlap:
                entry["action"] = "weak_link"
                derived_ids.append(r[0])

            # V3 #4: 叙述类 conflict 候选 ≥2 共享 entity_links + cos 不极端
            # 即使 cos 没到 0.92 dup 阈值, 但 shared_links ≥2 时也可能叙述冲突
            if len(shared_links) >= 2 and entry["action"] != "duplicate":
                narrative_overlap_candidates.append(entry)

            similar.append(entry)

        # 持久化 derived_from
        if derived_ids:
            derived_json = _json.dumps(derived_ids)
            db.execute(
                "UPDATE chunks SET derived_from=? WHERE id=?",
                (derived_json, new_id),
            )
            db.commit()
            similar = similar[:5]  # 只返 top 5

    except Exception as e:
        import logging
        logging.getLogger(__name__).debug("auto-associate failed: %s", e)
    finally:
        db.close()

    result: dict[str, Any] = {
        "ok": True,
        "chunk_id": new_id,  # 统一 key, 与 delete_cognition/supersede_memory 入参对齐
        "new_chunk_id": new_id,  # 兼容旧调用方
        "entity_links": entity_links,
        "similar_cognitions": similar[:5],
    }
    _cog_audit("add", new_id,
        source=source,
        cog_key=cog_key,
        entity_links=",".join(entity_links),
        text=text[:50],
    )
    # V3 #4: 叙述类冲突候选 (≥2 共享 entity_links) — 写入时无 LLM, 仅 tag, Dream 阶段批量处理
    if narrative_overlap_candidates:
        result["narrative_overlap_candidates"] = narrative_overlap_candidates[:3]
        result["narrative_hint"] = (
            f"检测到 {len(narrative_overlap_candidates)} 条共享 ≥2 entity_links 的相似认知. "
            f"Dream 阶段会做语义级冲突判断; 若你已知是真冲突 → 立即调 supersede_memory / 在 payload 注明 scope 区分."
        )
    # V2: 暴露 cog_key + 同 key 的不同值(冲突候选)给上层
    if cog_key:
        result["cog_key"] = cog_key
        # V3 (2026-07-23 #2): post-commit hook — 把 payload.value 同步到 entity profile.derived_fields
        # 让 sense_entity 看到的 profile 卡显示最新认知值 (Zero 反馈: profile 写完就过时).
        try:
            subject_predicate = cog_key.split(":", 1)
            subject = subject_predicate[0].strip() if len(subject_predicate) == 2 else ""
            predicate = subject_predicate[1].strip() if len(subject_predicate) == 2 else ""
            if subject and predicate and payload and payload.get("value") is not None:
                from domain.memory.memory.consciousness.entity_index import sync_profile_derived_field
                sync_profile_derived_field(
                    subject=subject, predicate=predicate,
                    value=payload.get("value"),
                    cog_key=cog_key, source_chunk_id=new_id,
                )
        except Exception as e:
            logger.debug("add_cognition_direct: sync_profile_derived_field failed: %s", e)
    if conflict_with:
        result["conflict_with"] = conflict_with
        result["conflict_warning"] = (
            f"⚠️ 检测到你对同一个 key({cog_key}) 写入了不同 value 值的认知。"
            f"已写入但建议决策:"
            f"  · 是覆盖/参数更新 → 调 supersede_memory({conflict_with[0]['chunk_id']}, \"新内容\")"
            f"  · 是不同 scope 的特例(eg 一般规律 vs 特定场景) → 在新 payload 加 `scope` 字段区分"
            f"  · 是真冲突 → Dream 阶段会做高精度规则冲突检测"
        )
    if duplicate_warning:
        result["duplicate_warning"] = (
            f"⚠️ 与已有认知 #{duplicate_warning['chunk_id']} 高度语义相似(cos={duplicate_warning['cos']}, 用词相近)。"
            f"**语义相似 ≠ 逻辑等价** — 请先核对是否真的同一回事:"
            f"  · 是同一条规则的不同表述/旧版本 → 调 supersede_memory({duplicate_warning['chunk_id']}, \"新内容\") 覆盖"
            f"  · 是补充/特例/不同方面 → 忽略此提醒, 自由保留"
            f"  · 拿不准 → 让两条都留着, Dream 阶段会做精确的冲突检测"
        )
    return result


def delete_cognition(chunk_id: int) -> dict[str, Any]:
    """删除一条认知。检查是否被其它认知 derived_from 引用。
    有引用 → 返回警告不删。无引用 → DELETE。
    """
    import json as _json
    import sqlite3 as _sqlite3
    db = _get_db()
    db.row_factory = _sqlite3.Row

    # 检查引用
    referrers = db.execute(
        "SELECT id FROM chunks WHERE derived_from LIKE ? AND id != ?",
        (f'%{chunk_id}%', chunk_id),
    ).fetchall()
    if referrers:
        ref_ids = [str(r["id"]) for r in referrers]
        db.close()
        return {
            "ok": False,
            "reason": f"该认知被 {len(referrers)} 条认知的 derived_from 引用(ids: {', '.join(ref_ids[:5])})。"
                      "建议用 supersede_memory 替代删除(保留历史链)。",
        }

    # 检查存在
    row = db.execute("SELECT id, phase, source, cog_key FROM chunks WHERE id=? AND phase='cognition'", (chunk_id,)).fetchone()
    if not row:
        db.close()
        return {"ok": False, "reason": f"chunk_id={chunk_id} 不存在或不是认知"}

    db.execute("DELETE FROM chunks WHERE id=?", (chunk_id,))
    db.commit()
    db.close()
    _cog_audit("delete", chunk_id, source=row["source"], cog_key=row["cog_key"])
    return {"ok": True, "deleted_chunk_id": chunk_id}


def mark_obsolete(
    chunk_id: int, *, reason: str = ""
) -> dict[str, Any]:
    """V6 (2026-07-24 用户反馈): 把认知软标为 archived (过时).

    与 delete_cognition 区别:
      · delete: 物理从 chunks 表删行 (硬剥夺, 溯源链断)
      · mark_obsolete: 保留行, 但 cognition_state='archived'
        → 任何召回路径 (vector / lexical / spread / fts / entity_links) 都 SQL 过滤掉
        → 记忆库不再"看见"它, 但溯源链 (derived_from) 完好保留

    与 supersede_one 区别:
      · supersede: 有新认知接班 (老的标 replaced + 新的 active)
      · mark_obsolete: 没有接班认知, 就是单纯"这事不准确/过时, 别再联想到了"

    使用场景 (Zero/Alpha 7/24 反馈):
      · 历史 lesson 已废, 但没有新 lesson 替代
      · 错误认知确认过时 (如"金开新能 -7% SL"那种 bug 产物, 没有接班规则)
      · 单纯过时的项目记忆 (项目已结束)
    """
    db = _get_db()
    try:
        row = db.execute(
            "SELECT id, phase, cognition_state FROM chunks WHERE id=? AND phase='cognition'",
            (chunk_id,)
        ).fetchone()
        if not row:
            return {"ok": False, "reason": f"chunk_id={chunk_id} 不存在或不是认知"}
        if row["cognition_state"] in ("archived", "replaced"):
            return {"ok": True, "already_obsolete": True,
                    "state": row["cognition_state"], "reason": "已是过时/被替换状态"}
        before_state = row["cognition_state"] or "active"
        db.execute(
            "UPDATE chunks SET cognition_state='archived', challenge_count=challenge_count+1 "
            "WHERE id=?",
            (chunk_id,)
        )
        db.commit()
        _cog_audit("mark_obsolete", chunk_id,
                   state=f"{before_state}→archived",
                   reason=reason[:80] if reason else None)
        return {"ok": True, "archived_chunk_id": chunk_id,
                "previous_state": before_state,
                "note": "已标记为 archived. 默认召回全部 7 条 SQL 路径过滤, 联想命中时不再返回. 仍可用 recall_cognition_by_key(include_history=True) 看."}
    finally:
        db.close()


# ──────────────────── V2 (2026-07-23): 结构化精确查询 ────────────────────


def recall_cognition_by_key(
    subject: str,
    predicate: str | None = None,
    *,
    include_history: bool = False,
) -> dict[str, Any]:
    """V2: 按 cog_key 精确召回某(主, 谓)的所有认知。

    `key` 形式 `${subject}:${predicate}`, predicate 可省略 → 返回该 subject 所有谓词。

    比 recall_entity / unified_recall 精确 — 那些是 embedding+词法的模糊匹配,
    本函数是 1:1 主键查询, 适合"我对 X 的 Y 的认知现在是什么".

    include_history=True: 返回包括 replaced/archived 的旧版本(supersede 链)
    """
    import json as _json
    import sqlite3 as _sqlite3
    from domain.memory.memory.recall.vector import _get_db

    if predicate:
        key = f"{subject}:{predicate}"
    else:
        # 只给 subject: 模糊匹配 subject:*
        key = f"{subject}"

    db = _get_db()
    try:
        if predicate:
            rows = db.execute(
                "SELECT id, text, source, cognition_state, payload, created_at, supersede_by "
                "FROM chunks WHERE cog_key=? AND phase='cognition' "
                + ("" if include_history else
                   "AND (cognition_state IS NULL OR cognition_state NOT IN ('replaced','archived')) ")
                + "ORDER BY created_at DESC",
                (key,),
            ).fetchall()
        else:
            # 无 predicate → 模糊匹配 'subject%' 或 'subject:%', 都接受
            # 这样 'regression-test-12345' 这种 subject 里含 timestamp 的 key 也能被找到
            rows = db.execute(
                "SELECT id, text, source, cognition_state, payload, created_at, supersede_by, cog_key "
                "FROM chunks WHERE (cog_key=? OR cog_key LIKE ? OR cog_key LIKE ?) AND phase='cognition' "
                + ("" if include_history else
                   "AND (cognition_state IS NULL OR cognition_state NOT IN ('replaced','archived')) ")
                + "ORDER BY cog_key, created_at DESC",
                (subject, subject + ":%", subject + "%"),
            ).fetchall()

        items: list[dict] = []
        for r in rows:
            try:
                p = _json.loads(r["payload"]) if r["payload"] else None
            except Exception:
                p = None
            items.append({
                "chunk_id": r["id"],
                "text": r["text"][:200] if r["text"] else "",
                "source": r["source"],
                "cog_key": r["cog_key"] if "cog_key" in r.keys() else key,
                "payload": p,
                "state": r["cognition_state"] or "active",
                "supersede_by": r["supersede_by"],
                "created_at": r["created_at"],
            })
        return {
            "ok": True,
            "key": key,
            "subject": subject,
            "predicate": predicate,
            "count": len(items),
            "items": items,
        }
    finally:
        db.close()


def list_cognition_keys(subject: str | None = None) -> dict[str, Any]:
    """V2: 列出所有 cog_key (可选过滤 subject), 让模型知道"自己知道什么"。

    返回 list[{"subject": str, "predicate": str, "count": int, "states": [...]}]
    """
    import sqlite3 as _sqlite3
    from domain.memory.memory.recall.vector import _get_db

    db = _get_db()
    try:
        if subject:
            rows = db.execute(
                "SELECT cog_key, count(*) as cnt, "
                "GROUP_CONCAT(DISTINCT COALESCE(cognition_state,'active')) as states "
                "FROM chunks WHERE cog_key LIKE ? AND phase='cognition' "
                "AND (cognition_state IS NULL OR cognition_state NOT IN ('replaced','archived')) "
                "GROUP BY cog_key ORDER BY cog_key",
                (subject + ":%",),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT cog_key, count(*) as cnt, "
                "GROUP_CONCAT(DISTINCT COALESCE(cognition_state,'active')) as states "
                "FROM chunks WHERE cog_key IS NOT NULL AND phase='cognition' "
                "AND (cognition_state IS NULL OR cognition_state NOT IN ('replaced','archived')) "
                "GROUP BY cog_key ORDER BY cog_key"
            ).fetchall()

        items = []
        for r in rows:
            key = r["cog_key"] or ""
            parts = key.split(":", 1)
            items.append({
                "subject": parts[0] if len(parts) >= 1 else "",
                "predicate": parts[1] if len(parts) >= 2 else "",
                "key": key,
                "count": r["cnt"],
                "states": (r["states"] or "").split(","),
            })
        return {"ok": True, "count": len(items), "items": items}
    finally:
        db.close()


def find_conflicting_keys() -> list[dict]:
    """V2 helper for Dream: 返回所有"同 key 多条认知"的桶。

    用于 Dream 阶段批量调 LLM 做精确冲突检测。
    """
    import json as _json
    from domain.memory.memory.recall.vector import _get_db

    db = _get_db()
    try:
        rows = db.execute(
            "SELECT cog_key, GROUP_CONCAT(id) as ids, count(*) as cnt "
            "FROM chunks WHERE cog_key IS NOT NULL AND phase='cognition' "
            "AND (cognition_state IS NULL OR cognition_state NOT IN ('replaced','archived')) "
            "GROUP BY cog_key HAVING cnt > 1 ORDER BY cnt DESC"
        ).fetchall()
        out = []
        for r in rows:
            ids = [int(x) for x in (r["ids"] or "").split(",") if x]
            if len(ids) < 2:
                continue
            out.append({"key": r["cog_key"], "ids": ids, "count": r["cnt"]})
        return out
    finally:
        db.close()


def find_narrative_conflict_buckets(
    *, min_shared_links: int = 2, max_buckets: int = 20
) -> list[dict]:
    """V3 #4: 找共享 entity_links ≥ N 的认知桶 (叙述类冲突检测).

    与 find_conflicting_keys 区别: 那个是精确 cog_key 桶 (参数类);
    这个按 entity_links jsonString overlap 分桶 (叙述类, 无 cog_key).

    用于 Dream 阶段把整个桶包给 LLM 做 subsume/supersede/split/keep 决策.

    返回 list[{
        shared_links: ["金开新能", "止损"...],
        ids: [chunk_id1, ...],
        count: int,
        grouped_by: "entity_links_shared"
    }]
    """
    import json as _json
    from collections import defaultdict
    from domain.memory.memory.recall.vector import _get_db

    db = _get_db()
    try:
        # 拉所有有 entity_links 的 active 认知
        rows = db.execute(
            "SELECT id, entity_links, text FROM chunks "
            "WHERE phase='cognition' "
            "AND (cognition_state IS NULL OR cognition_state NOT IN ('replaced','archived')) "
            "AND entity_links IS NOT NULL AND entity_links != '[]' AND entity_links != '' "
            "AND cog_key IS NULL "  # 排除已有精确 key 的 (避免与 find_conflicting_keys 重复)
        ).fetchall()
        if not rows:
            return []

        # 每个 (link-pair-tuple) → list of chunk_ids
        link_bucket = defaultdict(list)
        for r in rows:
            try:
                links = _json.loads(r["entity_links"] or "[]")
            except Exception:
                continue
            if not isinstance(links, list) or len(links) < min_shared_links:
                continue
            # 用一个排列键: 链接集合前 N 大
            link_set = frozenset(str(l) for l in links[:6])
            if len(link_set) >= min_shared_links:
                # 用 top-2 link-pair 做 key
                pair_keys = []
                link_list = sorted(link_set)
                for i in range(len(link_list)):
                    for j in range(i + 1, len(link_list)):
                        pair_keys.append((link_list[i], link_list[j]))
                for pk in pair_keys:
                    link_bucket[pk].append({
                        "id": r["id"],
                        "links": link_list,
                        "text": (r["text"] or "")[:120],
                    })

        # 只保留桶中 >= 2 条 chunk 的 (有冲突可能)
        candidates = []
        seen_chunk_sets: set = set()
        for pair, members in link_bucket.items():
            if len(members) < 2:
                continue
            ids = tuple(sorted(m["id"] for m in members))
            if ids in seen_chunk_sets:
                continue
            seen_chunk_sets.add(ids)
            shared = sorted(set.intersection(*[set(m["links"]) for m in members]))
            if len(shared) < min_shared_links:
                continue
            candidates.append({
                "shared_links": shared,
                "ids": [m["id"] for m in members],
                "count": len(members),
                "grouped_by": "entity_links_shared",
            })
            if len(candidates) >= max_buckets:
                break

        # 按桶大小排序
        candidates.sort(key=lambda x: x["count"], reverse=True)
        return candidates
    finally:
        db.close()


def check_supersede_neighbors_for_stale_links(
    old_chunk_id: int, new_chunk_id: int
) -> list[dict]:
    """V3 #4: supersede 后检查"邻居"是否需要同步更新.

    场景: 把 #A (links=[金开新能,止损线]) supersede 成 #A2,
          但同 entity_links 还有 #B (另一只股票的止损线). 模型应被告知 #B 也提到
          [止损线], 可能本次 supersede 影响到对它的语义解读.

    返回 list[{chunk_id, text, shared_links}] — 调用方/工具把它放在返回里给 model 看到.
    """
    from domain.memory.memory.recall.unified.spread import fetch_entity_neighbors
    try:
        nbrs = fetch_entity_neighbors(new_chunk_id, limit=10)
        # 排除被 supersede 的老 chunk 自己
        return [n for n in nbrs if n.get("chunk_id") != old_chunk_id]
    except Exception as e:
        logger.debug("check_supersede_neighbors_for_stale_links failed: %s", e)
        return []


__all__ = [
    "apply_signal",
    "promote_one",
    "supersede_one",
    "revise_one",
    "cluster_born_persist",
    "load_slice_by_id",
    "add_cognition_direct",
    "delete_cognition",
    "recall_cognition_by_key",
    "list_cognition_keys",
    "find_conflicting_keys",
    "find_narrative_conflict_buckets",       # V3 #4
    "check_supersede_neighbors_for_stale_links",  # V3 #4
]
