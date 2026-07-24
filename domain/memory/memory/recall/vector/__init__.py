"""向量联想记忆召回 — 智谱 Embedding-3 + 扩散激活 + MMR 多样性采样。

架构：
- 记忆源文件 → 分块 → embedding → SQLite
- 召回：query → embedding → top candidates → 扩散激活 → MMR 采样
- 联想表：被同时召回的 chunks 建立关联，权重随时间衰减

分层源：
  identity  → CONSCIOUSNESS.md（权重 1.5，低阈值）
  journal   → DIARY.md（权重 1.0）
  notes     → SCRATCHPAD.md（权重 1.2）
  goals     → GOALS.md（权重 1.3）
  plans     → PLANS.md（权重 1.2）
  him       → HIM.md（权重 1.3）
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from typing import Dict, List, Optional, Tuple

import logging

from infrastructure.config import get_runtime_env_path, get_runtime_memories_dir
from infrastructure.persistence import sqlite

logger = logging.getLogger("domain.memory.recall.vector")

# Lazy: resolved on first call (after DIGITAL_LIFE_INSTANCE_ID is set by gateway).
_mem_dir_cache: Path | None = None


def _get_mem_dir() -> Path:
    global _mem_dir_cache
    if _mem_dir_cache is None:
        _mem_dir_cache = get_runtime_memories_dir()
    return _mem_dir_cache


def _get_db_path() -> Path:
    return _get_mem_dir() / "memory_vectors.db"


# Backward-compatible module-level aliases — resolve lazily.
# Internal code uses _get_mem_dir() / _get_db_path() to ensure instance isolation.

# 智谱 Embedding API（CodingPlan 不支持 embedding，失败时静默跳过）
_EMBEDDING_MODEL = "embedding-3"
_EMBEDDING_DIM = 2048
_EMBEDDING_API = "https://open.bigmodel.cn/api/paas/v4/embeddings"

# 文件源配置（从 memories 目录读取的静态文件）
# 阈值基于 GLM embedding-3 实测评测集:
#   应命中 cosine: 0.88-1.09 | 不应命中 cosine: 0.40-0.55
#   最佳分界线: 0.55-0.56 → 统一阈值用 0.55
#   cognitions (rules/lessons/knowledge) 略低 0.50 因为体积小且有 supersede 保护
_FILE_SOURCES = {
    "identity": {"path": "CONSCIOUSNESS.md", "max_chars": 600, "weight": 1.5, "threshold": 0.55},
    "journal": {"path": "DIARY.md", "max_chars": 400, "weight": 1.0, "threshold": 0.55},
    "notes": {"path": "SCRATCHPAD.md", "max_chars": 500, "weight": 1.2, "threshold": 0.55},
    "goals": {"path": "GOALS.md", "max_chars": 400, "weight": 1.3, "threshold": 0.55},
    "plans": {"path": "PLANS.md", "max_chars": 300, "weight": 1.2, "threshold": 0.55},
    "him": {"path": "HIM.md", "max_chars": 300, "weight": 1.3, "threshold": 0.55},
    "knowledge": {"path": "MEMORY.md", "max_chars": 800, "weight": 1.4, "threshold": 0.50},
    "context": {"path": "CONTEXT.md", "max_chars": 400, "weight": 1.3, "threshold": 0.55},
    "work": {"path": "WORK.md", "max_chars": 500, "weight": 0.8, "threshold": 0.60},
    # V6: rules/lessons 不再从 .md 文件索引 — 认知库是唯一真相源.
    # RULES.md / LESSONS.md 仍由 update_rules / add_lesson 写入(给人类看),
    # 但不参与 reindex → 不产生双路径重复.
    # source='rule'/'lesson' 的认知只通过 add_cognition / add_lesson / update_rules 写入.
}

# 扩展源配置（动态写入的源，非文件）
# weight: 召回时的相似度权重
# threshold: 最低阈值
# decay_hours: 时间衰减半衰期（小时），越高越持久
_DYNAMIC_SOURCES = {
    # add_cognition 的 source_category 值 — 需要在 _ALL_SOURCES 里注册才能被 recall_structured 查到
    "fact": {"weight": 1.0, "threshold": 0.55},
    "lesson": {"weight": 1.2, "threshold": 0.50},
    "rule": {"weight": 1.5, "threshold": 0.50},
    "insight": {"weight": 1.0, "threshold": 0.55},
    # conversation 降权: 太多低价值对话碎片噪声淹没高质量认知。
    # weight 1.6→0.5, threshold 0.12→0.28 (只召回高度相似的对话)。
    # decay 72h→48h (老对话更快衰减出候选池)
    "conversation": {"weight": 0.5, "threshold": 0.55, "decay_hours": 48},
    # digest 类: 旧 weight=2.0 让所有 digest 都排前面
    # 但 587/1159 条 digest 是 <150 字的骨架摘要(无 LLM 总结干货),
    # 高 weight + 低阈值 = 大量低质 digest 占据召回 top → 召回"有结果但没价值"
    # 调整: 降 weight + 提高阈值, 让高质量长 digest(有 LLM 总结) 排前面
    "digest_session": {"weight": 1.0, "threshold": 0.50, "decay_hours": 168},
    "digest_segment": {"weight": 1.0, "threshold": 0.50, "decay_hours": 168},
    "digest_day": {"weight": 0.8, "threshold": 0.55, "decay_hours": 336},
    "digest_week": {"weight": 0.6, "threshold": 0.55, "decay_hours": 720},
}

# 所有源配置合并
_SOURCES = _FILE_SOURCES  # 向后兼容（ensure_indexed 使用）
_ALL_SOURCES = {**_FILE_SOURCES, **_DYNAMIC_SOURCES}

# 扩散激活参数
_SPREAD_BOOST = 0.15       # 关联 chunk 的分数加成
_SPREAD_DECAY_DAYS = 30.0  # 关联权重衰减到 1/e 的天数
_MAX_SPREAD_PER_CHUNK = 3  # 每个 chunk 最多扩散激活几个关联

# MMR 多样性参数
_MMR_LAMBDA = 0.7  # 相关性 vs 多样性的权重 (1.0=纯相关性)


# ──────────────────── Embedding API ────────────────────

def _get_api_key() -> Optional[str]:
    key = os.environ.get("LLM_API_KEY") or os.environ.get("GLM_API_KEY")
    if key:
        return key
    env_path = get_runtime_env_path()
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("LLM_API_KEY=") or line.startswith("GLM_API_KEY="):
                return line.split("=", 1)[1].strip()
    return None


def _embed_texts(texts: List[str]) -> Optional[List[List[float]]]:
    """批量嵌入。按 spec FR-103 / Clarifications Q3:
       - 不重试(避免 429 风暴 + 不阻塞召回关键路径)
       - 部分成功时保留成功项(失败项以 None 占位),不再 all-or-nothing
       - 任何失败用 `logger.warning` 暴露(非 debug 静默)
       全部子项失败、且 key 存在时,返回的是 list-of-Nones(消费侧已逐项 `if emb is None: continue`)。
    """
    if not texts:
        return []
    api_key = _get_api_key()
    if not api_key:
        logger.warning("LLM API Key not found, skipping embedding")
        return None
    try:
        import urllib.request
        import urllib.error
        payload = json.dumps({
            "model": _EMBEDDING_MODEL,
            "input": texts,
        }).encode("utf-8")
        req = urllib.request.Request(
            _EMBEDDING_API,
            data=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        # 单次 HTTP timeout 8s — 为 P2 unified_recall 整体 5s 上限让出余地(FR-104)。
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        embeddings: List[Optional[List[float]]] = [None] * len(texts)
        for item in data.get("data", []):
            idx = item.get("index", 0)
            embeddings[idx] = item["embedding"]
        # 不再做 all-or-nothing。返回真实结果(可能含 None 占位),
        # 让 _index_source / index_conversations 等已逐项 `if emb is None: continue`
        # 的消费侧保留部分成功(spec FR-103 / Clarifications Q3)。
        missing = sum(1 for e in embeddings if e is None)
        if missing:
            logger.warning(
                "Embedding partial success: %d/%d returned null; keeping successful entries",
                missing, len(texts),
            )
        return embeddings
    except Exception as e:
        # 不重试(FR-001/FR-103: 召回不能阻塞)。warning 而非 debug,
        # 让 embedding-API 失败在控制台可见(SC-003)。
        logger.warning("Embedding API failed (no retry, will degrade): %s", e)
        return None


def _embed_single(text: str) -> Optional[List[float]]:
    """单条嵌入(转发 _embed_texts)。明确处理 [None] footgun:
       T010 改造后 _embed_texts 可能返回 [None] 形式(全 batch 都 null 但 key 在),
       原 `result[0] if result` 会返 None 但语义含糊;这里显式 None 检查第一项。
    """
    if not text:
        return None
    result = _embed_texts([text])
    if not result:
        return None
    first = result[0]
    return first if first is not None else None


# ──────────────────── SQLite 存储 ────────────────────

def _get_db() -> sqlite.Connection:
    db = sqlite.connect(str(_get_db_path()))
    db.row_factory = sqlite.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            chunk_hash TEXT NOT NULL,
            text TEXT NOT NULL,
            embedding BLOB,
            file_mtime REAL NOT NULL,
            created_at REAL NOT NULL,
            UNIQUE(source, chunk_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source);

        CREATE TABLE IF NOT EXISTS associations (
            chunk_a INTEGER NOT NULL,
            chunk_b INTEGER NOT NULL,
            weight REAL NOT NULL DEFAULT 1.0,
            last_activated REAL NOT NULL,
            PRIMARY KEY (chunk_a, chunk_b),
            FOREIGN KEY (chunk_a) REFERENCES chunks(id),
            FOREIGN KEY (chunk_b) REFERENCES chunks(id)
        );
        CREATE INDEX IF NOT EXISTS idx_assoc_a ON associations(chunk_a);
        CREATE INDEX IF NOT EXISTS idx_assoc_b ON associations(chunk_b);
    """)
    # P1 T012: 幂等加 `phase` 列(DEFAULT '')。P3 schema 会继续扩展更多字段。
    # 仿 consolidation_runtime._get_db 的幂等 ALTER 模式(try/except "duplicate column")。
    # phase 由 P1 阶段写入但不消费(数据预先就位,避免 P3 二改表 — 见 spec Clarifications Q1)。
    try:
        db.execute("ALTER TABLE chunks ADD COLUMN phase TEXT NOT NULL DEFAULT ''")
    except Exception:
        pass  # column already exists — idempotent

    # P3 T033: 统一切片层 schema 扩展。所有列加 DEFAULT,允许老行存在 NULL
    # (回填在 T034)。幂等 ALTER。
    _p3_columns: list[tuple[str, str]] = [
        # (col, DDL-fragment-after ADD COLUMN)
        ("source_kind",      "TEXT NOT NULL DEFAULT ''"),
        ("session_id",       "TEXT NOT NULL DEFAULT ''"),
        ("segment_index",    "INTEGER"),
        ("derived_from",     "TEXT NOT NULL DEFAULT '[]'"),  # JSON array of chunk_id
        ("derive_kind",      "TEXT NOT NULL DEFAULT ''"),
        ("authority",        "REAL NOT NULL DEFAULT 0.5"),
        ("permanence",       "REAL NOT NULL DEFAULT 0.3"),
        ("freshness",        "REAL NOT NULL DEFAULT 1.0"),
        ("activation",       "REAL NOT NULL DEFAULT 0.0"),
        ("verification",     "REAL NOT NULL DEFAULT 0.0"),
        ("evidence_count",   "INTEGER NOT NULL DEFAULT 0"),
        ("challenge_count",  "INTEGER NOT NULL DEFAULT 0"),
        ("cognition_state",  "TEXT"),  # NULL = 经历 slice
        ("supersede_by",     "INTEGER"),
        ("entity_links",     "TEXT NOT NULL DEFAULT '[]'"),  # JSON of names
        ("attention_tokens", "TEXT NOT NULL DEFAULT '[]'"),
        ("provenance",       "TEXT NOT NULL DEFAULT ''"),
    ]
    for col, ddl in _p3_columns:
        try:
            db.execute(f"ALTER TABLE chunks ADD COLUMN {col} {ddl}")
        except Exception:
            pass

    # 索引:phase 用于 cognitive filter;session_id+segment_index 用于时序邻居
    try:
        db.execute("CREATE INDEX IF NOT EXISTS idx_chunks_session ON chunks(session_id, segment_index)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_chunks_phase ON chunks(phase)")
    except Exception:
        pass

    # V2 (2026-07-23): 结构化认知主键 — payload (JSON) + cog_key (text).
    # 老行为不变(payload/cog_key NULL), 仅当 add_cognition 主动写入时启用精确去重/查询。
    try:
        db.execute("ALTER TABLE chunks ADD COLUMN payload TEXT")  # nullable JSON
    except Exception:
        pass
    try:
        db.execute("ALTER TABLE chunks ADD COLUMN cog_key TEXT")  # nullable text
    except Exception:
        pass
    try:
        # 仅在 cog_key 非空时建索引(节省空间, 避免老全表索引浪费)
        db.execute("CREATE INDEX IF NOT EXISTS idx_chunks_cog_key ON chunks(cog_key) WHERE cog_key IS NOT NULL")
    except Exception:
        pass

    # P3 T035: FTS5 虚拟表 + 触发器(若环境支持)。失败时静默降级(unified.fts 模块
    # 会日志提示,检索不会因此失败 — FR-001 检索非阻断点)。
    try:
        from domain.memory.memory.recall.unified.fts import ensure_fts5_schema
        ensure_fts5_schema(db)
    except Exception:
        pass  # FTS5 不可用或 import 失败 — facade 走 vector+entity 兜底

    return db


def _normalize_source_label(source: str) -> str:
    """V3 (2026-07-24): Canonical source name.

    Legacy plural ('rules'/'lessons') normalizes to singular ('rule'/'lesson')
    so that RULES.md file indexing and add_cognition(category='rule') write
    the same source column → UNIQUE(source, chunk_hash) dedups naturally,
    preventing the "duplicate rule chunks" issue resurfacing.
    """
    if source in {"rules", "lessons"}:
        return source[:-1]
    return source


def _chunk_hash(source: str, text: str) -> str:
    canonical_source = _normalize_source_label(source)
    return hashlib.md5(f"{canonical_source}:{text}".encode()).hexdigest()


def _embedding_to_blob(vec: List[float]) -> bytes:
    import struct
    return struct.pack(f"{len(vec)}d", *vec)


def _blob_to_embedding(blob: bytes) -> List[float]:
    import struct
    n = len(blob) // 8
    return list(struct.unpack(f"{n}d", blob))


def _chunk_by_delimiter(text: str, delimiter: str, max_chars: int) -> List[str]:
    """\u6309\u5206\u9694\u7b26\u5207\u5206\uff0c\u6bcf\u5757\u72ec\u7acb\u5904\u7406\u3002\u7528\u4e8eMEMORY.md\u7684\u00a7\u5206\u9694\u7b26\u3002"""
    chunks = []
    for block in text.split(delimiter):
        block = block.strip()
        if not block:
            continue
        if len(block) <= max_chars:
            chunks.append(block)
        else:
            # block too large, use sliding window
            start = 0
            while start < len(block):
                end = start + max_chars
                chunk = block[start:end]
                para_break = chunk.rfind("\n\n")
                if para_break > max_chars // 2:
                    chunk = chunk[:para_break]
                if chunk.strip():
                    chunks.append(chunk.strip())
                start += max_chars - 50
    return chunks


def _sliding_chunks(text: str, max_chars: int = 300, overlap: int = 50) -> List[str]:
    if len(text) <= max_chars:
        return [text] if text.strip() else []
    
    # split by special delimiter first (for MEMORY.md and CONSCIOUSNESS.md)
    if "\u00a7" in text:
        return _chunk_by_delimiter(text, "\u00a7", max_chars)
    if "\n---\n" in text:
        return _chunk_by_delimiter(text, "\n---\n", max_chars)
    
    chunks = []
    start = 0
    while start < len(text):
        end = start + max_chars
        chunk = text[start:end]
        para_break = chunk.rfind("\n\n")
        if para_break > max_chars // 2:
            chunk = chunk[:para_break]
        if chunk.strip():
            chunks.append(chunk.strip())
        start += max_chars - overlap
    return chunks


# ──────────────────── 索引构建 ────────────────────

def _index_source(db: sqlite.Connection, label: str, cfg: dict) -> int:
    # V3 (2026-07-24): canonical source for DB writes — legacy plural
    # ('rules'/'lessons' from _FILE_SOURCES dict keys) normalizes to singular
    # so add_cognition rule/lesson chunks 与 file 索引的 rule/lesson 不重复.
    canonical_label = _normalize_source_label(label)
    # 删除该 source 的旧 chunks, 但保护 cognition 阶段的 chunk (保留认知演化状态)
    # 旧 bug: 无条件 DELETE FROM chunks WHERE source=? 会擦掉 supersede_by / verification / evidence_count
    db.execute(
        "DELETE FROM chunks WHERE source=? AND (phase IS NULL OR phase != 'cognition')",
        (canonical_label,),
    )

    fpath = _get_mem_dir() / cfg["path"]
    if not fpath.exists():
        return 0
    mtime = fpath.stat().st_mtime
    content = fpath.read_text(encoding="utf-8")
    if not content.strip():
        return 0
    # knowledge and identity sources: index full content; others: only tail
    if label in ("knowledge", "identity"):
        tail = content
    else:
        tail = content[-(cfg["max_chars"] * 3):]
    chunks = _sliding_chunks(tail, max_chars=cfg["max_chars"])
    if not chunks:
        return 0
    new_chunks = []
    for chunk in chunks:
        ch = _chunk_hash(canonical_label, chunk)
        row = db.execute(
            "SELECT chunk_hash, file_mtime FROM chunks WHERE source=? AND chunk_hash=?",
            (canonical_label, ch),
        ).fetchone()
        if not row or row["file_mtime"] < mtime:
            new_chunks.append((ch, chunk, mtime))
    if not new_chunks:
        return 0
    texts = [c[1] for c in new_chunks]
    embeddings = _embed_texts(texts)
    if not embeddings:
        logger.debug("Embedding failed for %s, skipping index", canonical_label)
        return 0
    count = 0
    # T037: P3 让 _index_source 在写入时填 phase + source_kind(走 baseline 表)。
    # 其它字段(authority/permanence/...)留靠 backfill_slice_fields_if_needed 懒补,
    # 避免每条 INSERT 都查表(可接受,因为这层调用不频繁)。
    from domain.memory.memory.recall.unified.slice import baselines_for_source
    baseline = baselines_for_source(canonical_label)
    insert_phase = baseline["phase"]
    insert_source_kind = baseline["source_kind"]
    for (ch, text, mt), emb in zip(new_chunks, embeddings):
        if emb is None:
            continue
        blob = _embedding_to_blob(emb)
        db.execute(
            "INSERT OR REPLACE INTO chunks "
            "(source, chunk_hash, text, embedding, file_mtime, created_at, phase, source_kind) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (canonical_label, ch, text, blob, mt, time.time(), insert_phase, insert_source_kind),
        )
        count += 1
    db.commit()
    return count


def ensure_indexed(max_age_hours: float = 1.0) -> None:
    db = _get_db()
    try:
        cutoff = time.time() - max_age_hours * 3600
        for label, cfg in _SOURCES.items():
            fpath = _get_mem_dir() / cfg["path"]
            if not fpath.exists():
                continue
            recent = db.execute(
                "SELECT MAX(created_at) as last FROM chunks WHERE source=?",
                (label,),
            ).fetchone()
            if recent and recent["last"] and recent["last"] > cutoff:
                continue
            count = _index_source(db, label, cfg)
            if count:
                logger.info("Indexed %d chunks from %s", count, label)
    finally:
        db.close()


# ──────────────────── 相似度计算 ────────────────────

def _cosine_sim(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ──────────────────── V3 raw cosine dedup helper ────────────────────


def lookup_cognition_similarities(
    query_emb: List[float],
    *,
    max_chunk_ids: list[int] | None = None,
    limit: int = 10,
) -> list[dict]:
    """V3 (2026-07-23): 在 cognition phase 里找与 query_emb 最相近的 chunk,
    返回原始 cosine 而非 weighted score。

    与 recall_structured 区别:
    - 不应用 weight × cos 的加权 (那是召回场景的 boost)
    - 不应用 time_decay
    - 仅 phase='cognition' (不按 source 名单过滤, 避免漏同源认知)
    - 排除 archived/replaced

    用于 promote_memory 写入时的精准去重 (V3 #1)。

    返回 list[{
        "chunk_id": int, "source": str, "text": str, "raw_cos": float,
        "is_catch_all": bool, "payload": dict|None, "cog_key": str|None,
    }]
    `is_catch_all` 是 catch-all 多主题规则检测 (length >= 350 且 ≥3 个编号/日期 标记),
    命中 catch-all 时禁止 promote dedup 拦截 (修 BUG C).
    """
    import re as _re
    if not query_emb:
        return []
    db = _get_db()
    try:
        if max_chunk_ids:
            placeholders = ",".join("?" * len(max_chunk_ids))
            rows = db.execute(
                f"SELECT id, source, text, embedding, payload, cog_key FROM chunks "
                f"WHERE phase='cognition' AND id IN ({placeholders}) "
                f"AND (cognition_state IS NULL OR cognition_state NOT IN ('replaced','archived')) "
                f"AND embedding IS NOT NULL "
                f"ORDER BY id DESC LIMIT ?",
                list(max_chunk_ids) + [limit]
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT id, source, text, embedding, payload, cog_key FROM chunks "
                "WHERE phase='cognition' "
                "AND (cognition_state IS NULL OR cognition_state NOT IN ('replaced','archived')) "
                "AND embedding IS NOT NULL "
                "ORDER BY id DESC LIMIT 300"  # 限扫 300 行 (避免大库全扫)
            ).fetchall()
        results: list[dict] = []
        for r in rows:
            chunk_emb = _blob_to_embedding(r["embedding"])
            cos = _cosine_sim(query_emb, chunk_emb)
            text = r["text"] or ""
            is_ca = _is_catch_all_chunk(text)
            payload = None
            if r["payload"]:
                try:
                    import json as _j
                    payload = _j.loads(r["payload"])
                except Exception:
                    payload = None
            results.append({
                "chunk_id": r["id"],
                "source": r["source"],
                "raw_cos": cos,
                "text": text,
                "is_catch_all": is_ca,
                "payload": payload,
                "cog_key": r["cog_key"],
            })
        # 按 cos 降序
        results.sort(key=lambda x: x["raw_cos"], reverse=True)
        return results[:limit]
    finally:
        db.close()


# catch-all chunk 启发式 (见 lookup_cognition_similarities BUG C)
import re as _catchall_re
_CATCH_ALL_LEN = 200  # chars; ≥200 + 多主题标记 = catch-all candidate
_CATCH_ALL_NUM_DATES = 3  # 至少 3 个 `\d+\.` 或 `[YYYY-MM-...]` 标记才视为多主题


def _is_catch_all_chunk(text: str) -> bool:
    """检测 chunk 是否 catch-all 多主题规则 — 这些 chunk 词面宽泛会假阳性匹配很多 query,
    在 promote dedup 时应该被降权到 weak_link 而非 duplicate。"""
    if not text or len(text) < _CATCH_ALL_LEN:
        return False
    # 多个独立编号或日期标记 → 多主题
    n_num = len(_catchall_re.findall(r"\n\d+\.|^\d+\.", text))
    n_date = len(_catchall_re.findall(r"\[\d{4}-\d{2}-\d{2}|7/\d+|8/\d+", text))
    return (n_num + n_date) >= _CATCH_ALL_NUM_DATES


# ──────────────────── 扩散激活 ────────────────────

def _load_associations(db: sqlite.Connection, chunk_ids: List[int]) -> Dict[int, float]:
    """加载候选 chunks 的关联 chunk 及其衰减后的权重。"""
    if not chunk_ids:
        return {}
    placeholders = ",".join("?" * len(chunk_ids))
    rows = db.execute(
        f"SELECT chunk_a, chunk_b, weight, last_activated FROM associations "
        f"WHERE chunk_a IN ({placeholders}) OR chunk_b IN ({placeholders})",
        chunk_ids + chunk_ids,
    ).fetchall()

    now = time.time()
    spread_scores: Dict[int, float] = {}
    for row in rows:
        partner = row["chunk_b"] if row["chunk_a"] in chunk_ids else row["chunk_a"]
        if partner in chunk_ids:
            continue
        # 时间衰减
        age_days = (now - row["last_activated"]) / 86400
        decayed = row["weight"] * math.exp(-age_days / _SPREAD_DECAY_DAYS)
        boost = _SPREAD_BOOST * decayed
        spread_scores[partner] = max(spread_scores.get(partner, 0), boost)
    return spread_scores


def _update_associations(db: sqlite.Connection, selected_ids: List[int]) -> None:
    """被同时召回的 chunks 之间建立/增强关联。"""
    now = time.time()
    for i in range(len(selected_ids)):
        for j in range(i + 1, len(selected_ids)):
            a, b = selected_ids[i], selected_ids[j]
            if a > b:
                a, b = b, a
            existing = db.execute(
                "SELECT weight FROM associations WHERE chunk_a=? AND chunk_b=?",
                (a, b),
            ).fetchone()
            if existing:
                db.execute(
                    "UPDATE associations SET weight=weight+1, last_activated=? WHERE chunk_a=? AND chunk_b=?",
                    (now, a, b),
                )
            else:
                db.execute(
                    "INSERT INTO associations (chunk_a, chunk_b, weight, last_activated) VALUES (?, ?, 1, ?)",
                    (a, b, now),
                )
    db.commit()


# ──────────────────── MMR 多样性采样 ────────────────────

def _mmr_select(
    candidates: List[Tuple[int, float, str, str, List[float]]],
    query_emb: List[float],
    max_chars: int,
    existing_embs: Optional[List[List[float]]] = None,
) -> List[Tuple[int, float, str, str]]:
    """MMR 采样：平衡相关性和多样性。

    candidates: [(chunk_id, score, source, text, embedding), ...]
    existing_embs: 已从其他组选出的 chunk embeddings（跨组去重）
    Returns: [(chunk_id, final_score, source, text), ...]
    """
    if not candidates:
        return []

    selected: List[Tuple[int, float, str, str]] = []
    selected_embs: List[List[float]] = list(existing_embs or [])
    remaining = list(candidates)
    total_chars = 0

    while remaining:
        best_idx = -1
        best_mmr = -float("inf")
        best_score = 0.0

        for i, (cid, score, source, text, emb) in enumerate(remaining):
            # 与 query 的相关性
            relevance = score
            # 与已选 chunks 的最大相似度（冗余度）
            redundancy = 0.0
            if selected_embs:
                redundancy = max(_cosine_sim(emb, s) for s in selected_embs)
            # MMR 公式
            mmr = _MMR_LAMBDA * relevance - (1 - _MMR_LAMBDA) * redundancy

            if mmr > best_mmr:
                best_mmr = mmr
                best_idx = i
                best_score = score

        if best_idx < 0:
            break

        cid, score, source, text, emb = remaining.pop(best_idx)
        entry_chars = len(f"\n[{source.upper()} score={best_score:.2f}] {text[:200]}")
        if total_chars + entry_chars > max_chars:
            break

        selected.append((cid, best_score, source, text))
        selected_embs.append(emb)
        total_chars += entry_chars

    return selected


# ──────────────────── 主召回逻辑 ────────────────────

def recall(
    query: str,
    extra_context: str = "",
    max_total_chars: int = 800,
    sources: Optional[List[str]] = None,
    include_obsolete: bool = False,
) -> str:
    """向量联想召回 + 扩散激活 + MMR 多样性采样。

    流程：
    1. query → embedding → 计算所有 chunk 的相似度
    2. top candidates 的关联 chunk 获得扩散加成
    3. MMR 采样选出最终结果（多样性）
    4. 更新联想关联表
    """
    full_query = f"{query} {extra_context}".strip()
    if not full_query:
        return ""

    ensure_indexed(max_age_hours=2.0)

    query_emb = _embed_single(full_query)
    if not query_emb:
        try:
            from domain.memory.memory.recall import recall as keyword_recall
            return keyword_recall(query, extra_context, max_total_chars)
        except Exception:
            return ""

    db = _get_db()
    try:
        # 默认过滤死信认知(用户 2026-07-17 闸门一: replaced/challenged/archived
        # 不进默认召回 — 避免旧矛盾认知投递给模型)
        obsolete_filter = "" if include_obsolete else (
            " AND (cognition_state IS NULL "
            "      OR cognition_state NOT IN ('replaced','challenged','archived'))"
        )
        # 1. 计算所有 chunk 的基础相似度
        if sources:
            placeholders = ",".join("?" * len(sources))
            rows = db.execute(
                f"SELECT id, source, text, embedding, created_at, payload, phase FROM chunks "
                f"WHERE embedding IS NOT NULL AND source IN ({placeholders})"
                f"{obsolete_filter}",
                sources
            ).fetchall()
        else:
            rows = db.execute(
                f"SELECT id, source, text, embedding, created_at, payload, phase FROM chunks "
                f"WHERE embedding IS NOT NULL{obsolete_filter}"
            ).fetchall()

        # chunk_data 扩到 5 元组 (source, text, emb, payload, phase) — V3 #3 让 payload
        # 透传到 render_breadcrumbs, premise/rationale 可以被渲染
        chunk_data: Dict[int, Tuple[str, str, List[float], dict | None, str]] = {}
        base_scores: Dict[int, float] = {}
        now = time.time()

        for row in rows:
            source = row["source"]
            cfg = _ALL_SOURCES.get(source)
            if not cfg:
                continue
            chunk_emb = _blob_to_embedding(row["embedding"])
            sim = _cosine_sim(query_emb, chunk_emb)
            weighted = sim * cfg["weight"]

            # 时间衰减（仅动态源）
            decay_hours = cfg.get("decay_hours")
            if decay_hours and row["created_at"]:
                age_hours = (now - row["created_at"]) / 3600
                time_factor = math.exp(-age_hours / decay_hours)
                weighted *= max(time_factor, 0.1)  # 最低保留 10%

            threshold = cfg.get("threshold", 0.15)
            if weighted >= threshold:
                cid = row["id"]
                # V3 #3: 透传 payload + phase 到 render_breadcrumbs (premise/rationale 用)
                row_payload = None
                if row["payload"]:
                    try:
                        import json as _j_p
                        row_payload = _j_p.loads(row["payload"])
                    except Exception:
                        row_payload = None
                chunk_data[cid] = (source, row["text"], chunk_emb, row_payload, row["phase"] or "experience")
                base_scores[cid] = weighted

        if not base_scores:
            return ""

        # 2. 扩散激活：top candidates 的关联 chunk 获得加成
        top_ids = sorted(base_scores, key=base_scores.get, reverse=True)[:5]
        spread_scores = _load_associations(db, top_ids)

        # 合并扩散分数到基础分数
        final_scores = dict(base_scores)
        for cid, boost in spread_scores.items():
            if cid in chunk_data:
                final_scores[cid] = final_scores.get(cid, 0) + boost
            elif cid not in final_scores:
                # 关联 chunk 可能低于基础阈值但被扩散激活
                row = db.execute(
                    "SELECT source, text, embedding FROM chunks WHERE id=?", (cid,)
                ).fetchone()
                if row and row["embedding"]:
                    source = row["source"]
                    cfg = _ALL_SOURCES.get(source)
                    if cfg:
                        chunk_emb = _blob_to_embedding(row["embedding"])
                        sim = _cosine_sim(query_emb, chunk_emb) * cfg["weight"]
                        if sim + boost >= cfg.get("threshold", 0.15):
                            # V3 #3: 关联 chunk 也能透传 payload(查不到的 fallback None/experience)
                            chunk_data[cid] = (source, row["text"], chunk_emb, None, "experience")
                            final_scores[cid] = sim + boost

        # 3. 构建候选列表并按源类型分组，分层 MMR（保证对话类记忆不被摘要淹没）
        candidates = []
        for cid, score in final_scores.items():
            if cid in chunk_data:
                source, text, emb, payload_v3, phase_v3 = chunk_data[cid]
                candidates.append((cid, score, source, text, emb, payload_v3, phase_v3))

        candidates.sort(key=lambda x: x[1], reverse=True)

        _MMR_GROUPS = {
            "conversation": 0.20,     # 20% 预算给对话
            "digest": 0.25,           # 25% 给 digest（session/day/week）
            "file": 0.55,             # 55% 给文件源（identity/journal/notes 等）
        }

        def _source_group(source: str) -> str:
            if source == "conversation":
                return "conversation"
            if source.startswith("digest_"):
                return "digest"
            return "file"

        group_budgets = {}
        group_candidates = {}
        for cid, score, source, text, emb, payload_v3, phase_v3 in candidates:
            group = _source_group(source)
            group_candidates.setdefault(group, []).append(
                (cid, score, source, text, emb, payload_v3, phase_v3)
            )

        for group, ratio in _MMR_GROUPS.items():
            group_budgets[group] = int(max_total_chars * ratio)

        selected = []
        selected_ids = []
        # 按优先级排序：conversation > digest > file
        for group in ["conversation", "digest", "file"]:
            gc = group_candidates.get(group, [])
            if not gc:
                continue
            # 已选 chunk 的 embedding 用于 MMR 冗余过滤
            existing_embs = [chunk_data[cid][2] for cid, _, _, _ in selected if cid in chunk_data]
            group_selected = _mmr_select(
                gc, query_emb, group_budgets[group],
                existing_embs=existing_embs,
            )
            selected.extend(group_selected)
            selected_ids.extend(cid for cid, _, _, _ in group_selected)

        if not selected:
            return ""

        # 5. 格式化输出
        source_labels = {
            "identity": "自我", "journal": "日记", "notes": "笔记",
            "goals": "目标", "plans": "计划", "him": "用户记忆",
            "conversation": "对话", "digest_session": "经历摘要",
            "digest_day": "日摘要", "digest_week": "周摘要",
            "rules": "行为规则", "context": "上下文", "lessons": "教训",
        }
        lines = ["[联想记忆 — 向量召回的相关片段]"]
        total_chars = 0
        for cid, score, source, text in selected:
            tag = source_labels.get(source, source.upper())
            entry = f"\n[{tag} score={score:.2f}] {text[:200]}"
            lines.append(entry)
            total_chars += len(entry)

        if len(lines) <= 1:
            return ""

        lines.append("\n[/联想记忆]")

        # 6. 更新联想关联
        selected_ids = [cid for cid, _, _, _ in selected]
        if len(selected_ids) >= 2:
            _update_associations(db, selected_ids)

        return "".join(lines)
    finally:
        db.close()


def recall_structured(
    query: str,
    extra_context: str = "",
    max_total_chars: int = 800,
    sources: Optional[List[str]] = None,
    include_obsolete: bool = False,
) -> List[Dict[str, Any]]:
    """同 recall() 但返回结构化 list, 带 chunk_id / source / text / score。

    facade unified_recall 用这个版本, 避免 P2 之前 facade 只能走文本解析、
    chunk_id 全是 -1 导致 RRF 对应不上真实 chunk 的设计漏洞。
    内部逻辑与 recall() 一致, 只是把组装 cut 改 list[dict]。
    特殊打分: cognition 类(source=rules/lessons/self_knowledge/knowledge)
    加 +0.5 boost, 让它们排到 experience 经历之上, 对齐设计 §6.6。
    """
    import json as _json
    full_query = f"{query} {extra_context}".strip()
    if not full_query:
        return []

    ensure_indexed(max_age_hours=2.0)
    query_emb = _embed_single(full_query)
    if not query_emb:
        return []

    db = _get_db()
    try:
        # 闸门一(2026-07-17): 默认过滤死信认知 replaced/challenged/archived
        obsolete_filter = "" if include_obsolete else (
            " AND (cognition_state IS NULL "
            "      OR cognition_state NOT IN ('replaced','challenged','archived'))"
        )
        if sources:
            placeholders = ",".join("?" * len(sources))
            rows = db.execute(
                f"SELECT id, source, text, embedding, created_at, phase, payload FROM chunks "
                f"WHERE embedding IS NOT NULL AND source IN ({placeholders})"
                f"{obsolete_filter}",
                sources
            ).fetchall()
        else:
            rows = db.execute(
                f"SELECT id, source, text, embedding, created_at, phase, payload FROM chunks "
                f"WHERE embedding IS NOT NULL{obsolete_filter}"
            ).fetchall()

        now = time.time()
        chunk_data: Dict[int, Tuple[str, str, List[float], str]] = {}
        final_scores: Dict[int, float] = {}

        for row in rows:
            source = row["source"]
            cfg = _ALL_SOURCES.get(source)
            if not cfg:
                continue
            chunk_emb = _blob_to_embedding(row["embedding"])
            sim = _cosine_sim(query_emb, chunk_emb) * cfg["weight"]
            decay_hours = cfg.get("decay_hours")
            if decay_hours and row["created_at"]:
                age_hours = (now - row["created_at"]) / 3600
                time_factor = math.exp(-age_hours / decay_hours)
                sim *= max(time_factor, 0.1)
            # Cognition 微提权: +0.05 打破 tie, 不会把 conversation 挤掉
            # 阈值不再特殊放宽 — cognition 必须真实相似≥ threshold 才入选
            phase = row["phase"] if "phase" in row.keys() else ""
            if phase == "cognition":
                sim += 0.05
            # 统一阈值: 认知类不再 -0.5 放水(旧 bug 让所有认知无条件通过)
            if sim >= cfg.get("threshold", 0.55):
                cid = row["id"]
                chunk_data[cid] = (source, row["text"], chunk_emb, phase)
                final_scores[cid] = sim

        if not final_scores:
            return []

        # 简化:不带 MMR, 按 score 排序 + 字符预算
        sorted_cands = sorted(final_scores.items(), key=lambda x: x[1], reverse=True)
        out: List[Dict[str, Any]] = []
        total = 0
        import json as _recall_json
        for cid, score in sorted_cands:
            if total >= max_total_chars:
                break
            source, text, _, phase = chunk_data[cid]
            text_slice = text[:200]
            # V3 #3: 透传 payload (premise/rationale/key/value) 让 render_breadcrumbs 能渲染推论链
            row_payload = None
            try:
                raw_row = next((r for r in rows if r["id"] == cid), None)
                if raw_row and raw_row["payload"]:
                    row_payload = _recall_json.loads(raw_row["payload"])
            except Exception:
                row_payload = None
            # V6 #6: 透传 created_at 给 render_breadcrumbs 显示认知年龄
            row_created = 0
            try:
                raw_row = next((r for r in rows if r["id"] == cid), None)
                if raw_row and raw_row["created_at"]:
                    row_created = float(raw_row["created_at"])
            except Exception:
                pass
            out.append({
                "chunk_id": int(cid),
                "source": source,
                "text": text_slice,
                "score": float(score),
                "phase": phase,
                "payload": row_payload,
                "created_at": row_created,
            })
            total += len(text_slice)
        return out
    finally:
        db.close()


__all__ = ["recall", "ensure_indexed"]