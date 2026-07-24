"""Entity index — inverted index mapping entities to associated memories.

Stored as entity_index.json in runtime/memories/.
Provides:
- String-match entity extraction (fast, for mid-session recall)
- Multi-dimensional ranked query (recency + authority + context_overlap +
  verification)
- Entity lifecycle: merge, heatmap, verification tracking
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_LOCAL_TZ = datetime.now().astimezone().tzinfo

_AUTHORITY_MAP: dict[str, float] = {
    "rule": 1.0,
    "lesson": 0.8,
    "todo": 0.5,
    "consciousness": 0.6,
    "scratchpad": 0.2,
}
_LOW_AUTHORITY_TAGS: tuple[str, ...] = ("trading_wait", "system_wait", "monitor", "final_status")


def _get_runtime_home() -> Path:
    try:
        from infrastructure.config import get_runtime_home
        return get_runtime_home()
    except Exception:
        # Fallback is **last resort**——不应在 fresh clone 触发。
        # 旧实现默认走 apps/zero/data：在 zero 实例未创建时会写到错误路径。
        # 这里改成抛错让 module load 失败、错误更明显。
        raise RuntimeError(
            "runtime_home 未配置：调 get_runtime_home 失败且无 fallback。"
            "请先 digital-life init 创建实例并设置 DIGITAL_LIFE_INSTANCE_ID。"
        )


def _entity_index_path() -> Path:
    return _get_runtime_home() / "memories" / "entity_index.json"


def load_entity_index() -> dict[str, Any]:
    """Load the full entity index, returning empty dict if not found or corrupt."""
    path = _entity_index_path()
    if not path.exists():
        return {"version": 1, "entities": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "entities" not in data:
            return {"version": 1, "entities": {}}
        return data
    except Exception:
        logger.debug("Failed to load entity_index.json", exc_info=True)
        return {"version": 1, "entities": {}}


def save_entity_index(data: dict[str, Any]) -> None:
    path = _entity_index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _now_iso() -> str:
    return datetime.now(_LOCAL_TZ).isoformat()


def update_entity_index(
    entities: list[str],
    *,
    memory_type: str,
    memory_id: str,
    snippet: str = "",
    linked_entities: list[str] | None = None,
    tag: str = "",
    replace_existing: bool = False,
) -> None:
    """Associate a memory with one or more entities in the index.

    Creates new entity entries as needed.

    When replace_existing=True, removes old entries of the same memory_type
    for each entity before adding the new one. Used for state-report
    consciousness entries that should only have one current snapshot.
    """
    if not entities:
        return

    data = load_entity_index()
    entities_dict: dict[str, dict[str, Any]] = data.setdefault("entities", {})

    memory_entry: dict[str, Any] = {
        "memory_type": memory_type,
        "memory_id": memory_id,
        "snippet": snippet[:200] if snippet else "",
        "timestamp": _now_iso(),
        "linked_entities": linked_entities or [],
        "verification_count": 0,
        "last_accessed": None,
    }
    if tag:
        memory_entry["tag"] = tag

    for entity_name in entities:
        if not entity_name or not entity_name.strip():
            continue
        name = entity_name.strip()
        if name not in entities_dict:
            entities_dict[name] = {"aliases": [], "type": None, "memories": []}
        entity = entities_dict[name]

        # State-report replacement: remove old entries of same type so only
        # the current snapshot remains.
        if replace_existing:
            entity["memories"] = [
                m for m in entity["memories"]
                if m.get("memory_type") != memory_type
            ]

        # Avoid inserting duplicate memory entries (same memory_id).
        # Deep-copy memory_entry so each entity gets its own dict — prevents
        # shared-reference bugs when bump_verification/touch_last_accessed
        # modify one entity's copy.
        existing_ids = {m.get("memory_id") for m in entity["memories"]}
        if memory_id not in existing_ids:
            entity["memories"].append(dict(memory_entry))

    save_entity_index(data)


def query_entities(entity_names: list[str]) -> list[dict[str, Any]]:
    """Return all memories associated with any of the given entity names.

    Results are sorted by timestamp descending.
    """
    if not entity_names:
        return []

    data = load_entity_index()
    entities_dict = data.get("entities", {})

    seen_ids: set[str] = set()
    results: list[dict[str, Any]] = []

    for name in entity_names:
        entity = entities_dict.get(name)
        if not entity:
            continue
        for memory in entity.get("memories", []):
            mid = memory.get("memory_id", "")
            if mid and mid not in seen_ids:
                seen_ids.add(mid)
                results.append(dict(memory, _matched_entity=name))

    results.sort(key=lambda m: str(m.get("timestamp", "")), reverse=True)
    return results


def _compute_authority(memory: dict[str, Any]) -> float:
    mtype = str(memory.get("memory_type", "")).lower()
    tag = str(memory.get("tag", "")).lower()

    if mtype == "rule":
        return _AUTHORITY_MAP["rule"]
    if mtype == "lesson":
        return _AUTHORITY_MAP["lesson"]
    if mtype == "scratchpad":
        return _AUTHORITY_MAP["scratchpad"]
    if mtype == "consciousness":
        for low_tag in _LOW_AUTHORITY_TAGS:
            if low_tag in tag:
                return 0.3
        return _AUTHORITY_MAP["consciousness"]
    return 0.4


def _compute_recency(timestamp: str | None) -> float:
    if not timestamp:
        return 0.0
    try:
        ts = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        now = datetime.now(_LOCAL_TZ)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_LOCAL_TZ)
        days = (now - ts).total_seconds() / 86400.0
        return max(0.0, 1.0 - days / 30.0)
    except Exception:
        return 0.0


def _compute_context_overlap(memory: dict[str, Any], current_entities: set[str]) -> float:
    linked = set(memory.get("linked_entities", []))
    if not current_entities or not linked:
        return 0.0
    return len(current_entities & linked) / max(len(current_entities), 1)


def _compute_verification_bonus(memory: dict[str, Any]) -> float:
    count = int(memory.get("verification_count", 0))
    return min(count / 5.0, 1.0)


def query_entities_ranked(
    entity_names: list[str],
    *,
    current_context: str = "",
    exclude_ids: set[str] | None = None,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Multi-dimensional ranked query for entity memories.

    Ranking formula: recency * 0.35 + authority * 0.25 +
                     context_overlap * 0.25 + verification * 0.15

    返回结构设计(2026): **profile 为主,碎片为辅**。
    每个命中实体若有 profile→先返回一张「概念卡」(memory_type="profile",
    snippet = summary + facts 合成);碎片仍按排名返回作补充细节。
    这样联想时模型读到的是「对实体的理解」而非散乱碎片——与 memory_hygiene
    的「消化碎片成 profile」步骤配套。没 profile 的实体退回纯碎片(不破坏旧行为)。

    ⚠ 重要:有 profile 但碎片已清空的实体(消化机制铺开后常态)不能被
    query_entities 的 "无碎片即空" 短路掉——profile 卡生成必须独立于碎片存在。
    """
    results = query_entities(entity_names)

    exclude = exclude_ids or set()
    # Use both passed entity_names AND entities extracted from the full context
    # text. In mid-session recall, entity_names may only contain NEW entities
    # (after dedup), but the context text spans the full session conversation
    # which includes entities injected earlier. Merging both gives the most
    # accurate context_overlap score.
    context_entities: set[str] = set()
    if current_context:
        context_entities = set(extract_entities_from_context(current_context))
    current_entities = set(entity_names) | context_entities

    scored: list[tuple[float, dict[str, Any]]] = []
    for mem in results:
        mid = str(mem.get("memory_id", ""))
        if mid and mid in exclude:
            continue

        score = (
            _compute_recency(mem.get("timestamp")) * 0.35
            + _compute_authority(mem) * 0.25
            + _compute_context_overlap(mem, current_entities) * 0.25
            + _compute_verification_bonus(mem) * 0.15
        )
        scored.append((score, mem))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:limit]
    top_mems = [mem for _, mem in top]

    # Profile-first: 为每个有 profile 的命中实体生成一张概念卡,排在碎片前面。
    # 必须独立于碎片结果——碎片可能是空的(消化机制铺开后 profile 实体常已清碎片)。
    profile_cards = _build_profile_cards(entity_names)
    if profile_cards:
        # 把 limit 拆分:profile 卡占一部分,碎片占余下。
        profile_quota = max(1, limit // 3)
        used_profile = profile_cards[:profile_quota]
        keep_fragments = max(0, limit - len(used_profile))
        top_mems = used_profile + top_mems[:keep_fragments]

    # Update last_accessed for returned fragments (profile 卡不是真实 memory,跳过)
    for mem in top_mems:
        mid = str(mem.get("memory_id", ""))
        if mem.get("memory_type") != "profile" and mid:
            touch_last_accessed(mid)

    return top_mems


def _build_profile_cards(entity_names: list[str]) -> list[dict[str, Any]]:
    """为命中的实体生成「概念卡」(memory_type=profile),用于联想注入时优先展示。

    无 profile 的实体跳过(调用方回退到纯碎片)。

    V3 (2026-07-23 #2): 渲染时 lazy 拉最新 cognition value 覆盖硬编码 derived_fields,
    避免用户反馈"profile 写完就没人维护,cognition 更新了 profile 不同步".
    """
    if not entity_names:
        return []
    data = load_entity_index()
    entities_dict = data.get("entities", {})
    cards: list[dict[str, Any]] = []
    for name in entity_names:
        entity = entities_dict.get(name)
        if not entity:
            continue
        profile = entity.get("profile")
        if not profile:
            continue
        # V3 #2: 先 lazy resolve derived_fields 用最新 cognition value
        resolved_profile = _resolve_profile_derived_fields(name, profile)
        summary = str(resolved_profile.get("summary", "")).strip()
        facts = resolved_profile.get("facts") or []
        derived_lines = resolved_profile.get("_derived_lines") or []
        if not summary and not facts and not derived_lines:
            continue
        parts = []
        if summary:
            parts.append(summary)
        if facts:
            joined = "; ".join(str(f) for f in facts if str(f).strip())
            if joined:
                parts.append(f"已知: {joined}")
        if derived_lines:
            parts.append("最新值: " + " | ".join(derived_lines))
        snippet = " | ".join(parts) if parts else summary
        cards.append({
            "memory_type": "profile",
            "memory_id": f"profile:{name}",
            "snippet": snippet,
            "timestamp": resolved_profile.get("last_updated"),
            "linked_entities": [],
            "verification_count": 0,
            "_matched_entity": name,
            "_entity_kind": entity.get("type"),
        })
    return cards


# ──────────────────── V3 (2026-07-23 #2): profile.derived_fields 衍生字段 ────────────────────


def sync_profile_derived_field(
    subject: str,
    predicate: str,
    *,
    value: Any,
    cog_key: str,
    source_chunk_id: int,
) -> None:
    """V3 #2: 把 cognition value 写入对应 entity 的 profile.derived_fields.

    被 cognition_store.supersede_one / add_cognition_direct 在 commit 后调用 (post-commit hook).
    失败不阻塞主路径(包在 try/except).

    实现: 读 entity_index.json → entities[subject].profile.derived_fields[predicate] 覆盖式更新.
    若 entity 不存在 → 自动空 profile 创建 (profile.summary 留空, 等 model 主动 set).
    """
    if not subject or not predicate:
        return
    try:
        data = load_entity_index()
        entities = data.setdefault("entities", {})
        ent = entities.get(subject)
        if not ent:
            # entity 不存在 → 创建一个空 profile 的占位 entity (summary 留给用户后续填)
            ent = {"aliases": [], "type": None, "memories": [], "profile": {}}
            entities[subject] = ent
        profile = ent.setdefault("profile", {})
        derived = profile.setdefault("derived_fields", {})
        # 覆盖式 update — 旧值被标到 history (最多保留 3 个旧值)
        existing = derived.get(predicate) or {}
        history = list(existing.get("history") or [])
        if existing.get("value") is not None:
            history.insert(0, {
                "value": existing.get("value"),
                "from_chunk_id": existing.get("source_chunk_id"),
                "synced_at": existing.get("synced_at"),
            })
            history = history[:3]  # cap history
        import time as _sync_time
        derived[predicate] = {
            "cog_key": cog_key,
            "value": value,
            "source_chunk_id": source_chunk_id,
            "synced_at": _sync_time.time(),
            "history": history,
        }
        save_entity_index(data)
    except Exception as e:
        logger.debug("sync_profile_derived_field failed for %s:%s: %s", subject, predicate, e)


def _resolve_profile_derived_fields(entity_name: str, profile: dict) -> dict:
    """V3 #2: 渲染时把 profile.derived_fields 转为可读行 + lazy 验证最新值.

    直接从 profile.derived_fields 取值(已经在 sync_profile_derived_field 写入时存了),
    生成 `· 最新值: <predicate> = <value>` 行, 让 model 看到最新的认知状态.
    """
    derived = profile.get("derived_fields") or {}
    if not derived:
        return profile
    import copy as _copy
    out = _copy.deepcopy(profile)
    lines: list[str] = []
    for predicate, ref in derived.items():
        if not isinstance(ref, dict):
            continue
        value = ref.get("value")
        if value is None:
            continue
        # 用 predicate 直接渲染: stop_loss_line → "止损线=-0.07"
        lines.append(f"{predicate} = {value}")
    out["_derived_lines"] = lines
    return out


def extract_entities_from_context(text: str) -> list[str]:
    """Fast string-match extraction of known entities from a text.

    Matches entity names and aliases against the text. O(entities * text_len)
    which is fine for hundreds of entities and thousands of chars (<5ms).

    Returns entity names in order of first appearance in the text.
    """
    if not text or not text.strip():
        return []

    data = load_entity_index()
    entities_dict = data.get("entities", {})
    if not entities_dict:
        return []

    text_lower = text.lower()
    hits: list[tuple[int, str]] = []  # (position, entity_name)

    for entity_name, entity_data in entities_dict.items():
        # 防污染 (review 2026-07-23):
        # 跳过纯工具/文件/内部模块名(它们会通过 substring in 在任何含 'rest'/'税'/'...' 的
        # query 上意外命中,把"工具调用过的页面"误识别为实体)。
        # - .py / .json 后缀 → 文件名, 跳过
        # - snake_case 全 ascii 小写且 ≥1 个下划线 → 工具/模块名, 跳过
        # - 单英文小写词(dream/sleep/rest...) → 通用词, 跳过
        if _is_pollutant_entity_name(entity_name):
            continue
        patterns = [entity_name] + list(entity_data.get("aliases", []))
        for pattern in patterns:
            if not pattern:
                continue
            # alias 也得防
            if _is_pollutant_entity_name(pattern):
                continue
            pos = text_lower.find(pattern.lower())
            if pos >= 0:
                hits.append((pos, entity_name))
                break  # One match is enough for this entity

    # Sort by position, remove duplicates while preserving order
    hits.sort(key=lambda x: x[0])
    seen: set[str] = set()
    result: list[str] = []
    for _, name in hits:
        if name not in seen:
            seen.add(name)
            result.append(name)
    return result


# 工具/文件/通用词污染 list —— 任何文本里 substring.in 都会命中, 不是真"实体提及"
_POLLUTANT_LITERALS = {
    # 工具名 / skill 名 / 内部模块名
    "dream", "rest", "sleep", "wakeup", "waking", "timer", "cron",
    "recall", "recall_entity", "self_review", "memory_hygiene",
    "emotion_engine", "entity_index", "personal_assistant",
    "circuit_breaker", "old_rule", "social_feed", "user_access_token",
    # 通用短中文词, 任何会话都高频命中
    "rest", "凌晨", "中午",
}


def _is_pollutant_entity_name(name: str) -> bool:
    """检测这个名字是否是不应作为 entity boost 的工具/文件/通用词。

    防止 'rest' / 'dream' / 'execute_xxx.py' 这种字符串在任何 query 上意外命中,
    把召回结果污染成与场景无关的工具/调试概念。
    """
    if not name:
        return True
    s = name.strip().lower()
    if s in _POLLUTANT_LITERALS:
        return True
    # snake_case (含 ascii 字母数字+下划线+点)
    if s.endswith((".py", ".json", ".yaml", ".md")):
        return True
    if "_" in s and all(c.isalnum() or c == "_" for c in s) and s.replace("_", "").isascii():
        return True
    return False


def merge_entities(primary: str, alias: str) -> dict[str, Any]:
    """Merge an aliased entity into the primary one.

    Moves all memories from alias to primary, adds alias to primary's
    aliases list, removes the alias entity. Returns the updated primary
    entity dict.
    """
    data = load_entity_index()
    entities = data.setdefault("entities", {})

    primary_entity = entities.get(primary)
    alias_entity = entities.get(alias)

    if not alias_entity:
        return primary_entity or {}

    if not primary_entity:
        primary_entity = {"aliases": [], "type": None, "memories": []}
        entities[primary] = primary_entity

    # Move memories (dedup by memory_id)
    existing_ids = {m.get("memory_id") for m in primary_entity["memories"]}
    for mem in alias_entity.get("memories", []):
        if mem.get("memory_id") not in existing_ids:
            primary_entity["memories"].append(mem)
            existing_ids.add(mem.get("memory_id"))

    # Merge aliases
    for a in alias_entity.get("aliases", []):
        if a not in primary_entity["aliases"] and a != primary:
            primary_entity["aliases"].append(a)
    if alias not in primary_entity["aliases"]:
        primary_entity["aliases"].append(alias)

    # Merge type if primary doesn't have one
    if not primary_entity.get("type") and alias_entity.get("type"):
        primary_entity["type"] = alias_entity["type"]

    # Remove alias entity
    del entities[alias]
    save_entity_index(data)

    return primary_entity


def get_entity_heatmap(days_back: int = 1) -> dict[str, int]:
    """Count memory entries per entity in the last N days. Returns {entity_name: count}."""
    data = load_entity_index()
    entities = data.get("entities", {})

    cutoff = datetime.now(_LOCAL_TZ) - timedelta(days=days_back)
    result: dict[str, int] = {}

    for entity_name, entity_data in entities.items():
        count = 0
        for mem in entity_data.get("memories", []):
            ts_str = str(mem.get("timestamp", ""))
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=_LOCAL_TZ)
                if ts >= cutoff:
                    count += 1
            except Exception:
                pass
        if count > 0:
            result[entity_name] = count

    return dict(sorted(result.items(), key=lambda x: x[1], reverse=True))


def bump_verification_for_entities(entity_names: list[str], memory_type: str) -> None:
    """Bump verification_count for the most recent memory of given type across entities.

    Used by add_lesson merge path — when a lesson is "confirmed again",
    the entity index verification_count should reflect that without creating
    a duplicate entry.
    """
    if not entity_names:
        return
    data = load_entity_index()
    for name in entity_names:
        entity = data.get("entities", {}).get(name)
        if not entity:
            continue
        best = None
        best_ts = ""
        for mem in entity.get("memories", []):
            if mem.get("memory_type") == memory_type:
                ts = str(mem.get("timestamp", ""))
                if ts > best_ts:
                    best_ts = ts
                    best = mem
        if best is not None:
            best["verification_count"] = int(best.get("verification_count", 0)) + 1
    save_entity_index(data)


def bump_verification(memory_id: str) -> None:
    """Increment verification_count for a memory entry."""
    if not memory_id:
        return
    data = load_entity_index()
    for entity_data in data.get("entities", {}).values():
        for mem in entity_data.get("memories", []):
            if mem.get("memory_id") == memory_id:
                mem["verification_count"] = int(mem.get("verification_count", 0)) + 1
                save_entity_index(data)
                return


def touch_last_accessed(memory_id: str) -> None:
    """Update last_accessed timestamp for a memory entry."""
    if not memory_id:
        return
    data = load_entity_index()
    for entity_data in data.get("entities", {}).values():
        for mem in entity_data.get("memories", []):
            if mem.get("memory_id") == memory_id:
                mem["last_accessed"] = _now_iso()
                save_entity_index(data)
                return


def get_entity_summary(entity_name: str) -> dict[str, Any] | None:
    """Get summary of an entity including its memories and metadata."""
    data = load_entity_index()
    return data.get("entities", {}).get(entity_name)


def list_entity_names() -> list[str]:
    """Return all entity names sorted alphabetically."""
    data = load_entity_index()
    return sorted(data.get("entities", {}).keys())


# ─────────────────────────────────────────────────────────────────────────
# Concept memory (profile + facts)
# ─────────────────────────────────────────────────────────────────────────
# A "concept memory" is the conceptual distillation of an entity — its
# profile. Distinct from fragment memories (consciousness / lesson snippets),
# a profile is a synthesized statement of "what we know about X" extracted
# from the fragments. Model writes these manually via the curate_entity tool,
# or writes via set_entity_profile. Frontend human can also edit.

# Concept-related entity types: these deserve a structured profile.
# Other entities (verbs / runtime strings) stay as pure association indices.
_CONCEPTUAL_TYPES: set[str] = {
    "person", "instance", "human",
    "project", "thesis", "strategy", "decision",
    "stock", "asset", "instrument",
    "concept", "skill", "tool",
    "todo",
}


def set_entity_profile(
    name: str,
    *,
    kind: str | None = None,
    aliases: list[str] | None = None,
    summary: str = "",
    facts: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Set or replace the structured profile of an entity.

    The profile is the conceptual distillation:
      - summary: 1-2 sentence "what is this thing" description
      - facts: list of bullet-form factual statements
      - extra: project-y specific fields (stop_loss / current_position etc.)

    Idempotent: re-running replaces prior profile field for the entity,
    fragments (memories) are unchanged.
    """
    if not name:
        return {"ok": False, "reason": "name required"}
    data = load_entity_index()
    entities_dict = data.setdefault("entities", {})
    entity = entities_dict.setdefault(name, {"aliases": [], "type": None, "memories": []})

    if kind:
        entity["type"] = kind
    if aliases is not None:
        entity["aliases"] = list(aliases)

    profile = entity.setdefault("profile", {})
    profile["summary"] = summary
    profile["facts"] = list(facts) if facts is not None else profile.get("facts", [])
    if extra:
        profile.setdefault("extra", {}).update(extra)
    profile["last_updated"] = _now_iso()

    save_entity_index(data)
    return {"ok": True, "entity": name, "profile": profile}


def get_entity_profile(name: str) -> dict[str, Any] | None:
    """Read profile field of an entity (without loading fragments)."""
    entity = get_entity_summary(name)
    if not entity:
        return None
    return entity.get("profile")


def index_health_check() -> dict[str, Any]:
    """Audit report on entity index health: missing profile/type/aliases.

    Returns:
      {
        "total_entities": N,
        "with_profile": [...],
        "missing_profile_high_value": [...],  # entities with many memories
                                              # but no profile (low signal)
        "missing_aliases": [...],             # entities with no aliases
                                              # (dedup candidates)
        "missing_type": [...],
        "suggested_merges": [                 # detected by common snippet prefix
          {"primary": X, "alias": Y, "shared_memory_count": N},
        ],
      }
    """
    data = load_entity_index()
    entities = data.get("entities", {})
    report = {
        "total_entities": len(entities),
        "with_profile": [],
        "missing_profile_high_value": [],
        "missing_aliases": [],
        "missing_type": [],
        "suggested_merges": [],
    }
    # Build snippet prefix map for merge detection
    snippet_index: dict[str, list[str]] = {}
    for name, info in entities.items():
        mems = info.get("memories", [])
        has_profile = bool(info.get("profile"))
        has_aliases = bool(info.get("aliases"))
        has_type = info.get("type")
        if has_profile:
            report["with_profile"].append(name)
        elif len(mems) >= 5:
            report["missing_profile_high_value"].append({
                "name": name,
                "fragment_count": len(mems),
                "kind": has_type,
            })
        if not has_aliases and len(mems) >= 3 and name.isdigit():
            # All-numeric names are likely codes/IDs — probably aliased elsewhere
            report["missing_aliases"].append(name)
        if not has_type and len(mems) >= 3:
            report["missing_type"].append(name)
        # Track snippet prefixes for dedup detection
        for m in mems:
            snip = (m.get("snippet") or "")[:80]
            if snip:
                snippet_index.setdefault(snip, []).append(name)
    # Cross-entity shared snippets ≥1 mean those entities point at SAME memory
    # → likely alias candidates.
    seen: set[tuple[str, str]] = set()
    for snip, names in snippet_index.items():
        unique_names = set(names)
        if len(unique_names) >= 2:
            primary = sorted(unique_names)[0]
            for alias in unique_names - {primary}:
                pair = tuple(sorted((primary, alias)))
                if pair in seen:
                    continue
                seen.add(pair)
                report["suggested_merges"].append({
                    "primary": primary,
                    "alias": alias,
                    "shared_memory_count": len(snippet_index[snip]),
                })
    return report


def prune_fragments_for_entity(name: str, keep: int = 5) -> dict[str, Any]:
    """Trim an entity's fragment memories to the most recent `keep` items.

    Use after ``set_entity_profile`` extracts the conceptual truth — keeping
    100+ consciousness fragments provides no signal at that point. Profile +
    a small tail of recent fragments is enough.
    """
    data = load_entity_index()
    entities = data.get("entities", {})
    if name not in entities:
        return {"ok": False, "reason": f"entity {name} not found"}
    mems = entities[name].get("memories", [])
    if len(mems) <= keep:
        return {"ok": True, "removed": 0, "kept": len(mems)}
    mems.sort(key=lambda m: m.get("timestamp", ""), reverse=True)
    kept = mems[:keep]
    removed = len(mems) - keep
    entities[name]["memories"] = kept
    save_entity_index(data)
    return {"ok": True, "removed": removed, "kept": len(kept)}


def sync_entity_from_source(
    name: str,
    *,
    entity_type: str = "",
    summary: str = "",
    extra: dict[str, Any] | None = None,
    aliases: list[str] | None = None,
) -> None:
    """源系统变更时调用——自动创建或更新 entity。

    与 set_entity_profile 的区别：
      - set_entity_profile 是模型主动调（完整覆盖 profile.summary/facts）
      - sync_entity_from_source 是 contacts/projects/skills 等源系统自动调
      - **不覆盖模型已写的 profile.summary**——只在 entity 不存在或 summary 为空时写
      - **追加 extra 字段**（merge，不覆盖已有 extra 的 key 如果新值和旧值同）
      - 更新 type 和 aliases（只在 entity 还没 type 或 type 为空时设）

    设计意图：保持"模型写的 > 系统生成的"优先级。系统只负责"注册身份"，
    不影响模型对实体的深度理解。
    """
    if not name or not name.strip():
        return
    name = name.strip()
    try:
        data = load_entity_index()
        entities_dict = data.setdefault("entities", {})
        entity = entities_dict.setdefault(name, {"aliases": [], "type": None, "memories": []})

        # type：只在空时设（模型可能已手动设了更精确的 type）
        if entity_type and not entity.get("type"):
            entity["type"] = entity_type

        # aliases：追加新 alias（不覆盖已有的）
        if aliases:
            existing_aliases = set(entity.get("aliases", []))
            for a in aliases:
                a = a.strip()
                if a and a != name and a not in existing_aliases:
                    entity.setdefault("aliases", []).append(a)
                    existing_aliases.add(a)

        # profile：摘要只在空时写，extra 追加合并
        profile = entity.setdefault("profile", {})
        if summary and not profile.get("summary"):
            profile["summary"] = summary
        if extra:
            pe = profile.setdefault("extra", {})
            for k, v in extra.items():
                if k not in pe:  # 不覆盖已有 key
                    pe[k] = v
        profile["last_updated"] = _now_iso()
        profile["last_source_sync"] = _now_iso()

        save_entity_index(data)
    except Exception as exc:
        logger.warning("sync_entity_from_source failed for '%s': %s", name[:20], exc)


def mark_entity_status(name: str, **flags: Any) -> None:
    """在 entity profile.extra 里标记状态标志（deleted/archived/blocked 等）。

    不删除实体、不删碎片——只标记。entity_recall 打分时可以读取这些标志
    做降权（比如 archived 的项目碎片 recency 权重可以降低）。
    """
    if not name or not name.strip():
        return
    name = name.strip()
    try:
        data = load_entity_index()
        entities_dict = data.setdefault("entities", {})
        entity = entities_dict.setdefault(name, {"aliases": [], "type": None, "memories": []})
        profile = entity.setdefault("profile", {})
        extra = profile.setdefault("extra", {})
        extra.update(flags)
        extra["last_status_update"] = _now_iso()
        save_entity_index(data)
    except Exception as exc:
        logger.warning("mark_entity_status failed for '%s': %s", name[:20], exc)


__all__ = [
    "load_entity_index",
    "save_entity_index",
    "update_entity_index",
    "query_entities",
    "query_entities_ranked",
    "extract_entities_from_context",
    "merge_entities",
    "get_entity_heatmap",
    "bump_verification",
    "bump_verification_for_entities",
    "touch_last_accessed",
    "get_entity_summary",
    # Concept memory API:
    "set_entity_profile",
    "get_entity_profile",
    "index_health_check",
    "prune_fragments_for_entity",
    "list_entity_names",
    # Auto-sync API:
    "sync_entity_from_source",
    "mark_entity_status",
]
