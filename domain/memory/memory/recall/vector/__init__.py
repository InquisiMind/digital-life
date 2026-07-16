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
_FILE_SOURCES = {
    "identity": {"path": "CONSCIOUSNESS.md", "max_chars": 600, "weight": 1.5, "threshold": 0.15},
    "journal": {"path": "DIARY.md", "max_chars": 400, "weight": 1.0, "threshold": 0.20},
    "notes": {"path": "SCRATCHPAD.md", "max_chars": 500, "weight": 1.2, "threshold": 0.18},
    "goals": {"path": "GOALS.md", "max_chars": 400, "weight": 1.3, "threshold": 0.15},
    "plans": {"path": "PLANS.md", "max_chars": 300, "weight": 1.2, "threshold": 0.15},
    "him": {"path": "HIM.md", "max_chars": 300, "weight": 1.3, "threshold": 0.15},
    "knowledge": {"path": "MEMORY.md", "max_chars": 800, "weight": 1.4, "threshold": 0.15},
    "rules": {"path": "RULES.md", "max_chars": 500, "weight": 1.5, "threshold": 0.15},
    "context": {"path": "CONTEXT.md", "max_chars": 400, "weight": 1.3, "threshold": 0.15},
    "lessons": {"path": "LESSONS.md", "max_chars": 400, "weight": 1.2, "threshold": 0.15},
    "work": {"path": "WORK.md", "max_chars": 500, "weight": 0.8, "threshold": 0.20},
}

# 扩展源配置（动态写入的源，非文件）
# weight: 召回时的相似度权重
# threshold: 最低阈值
# decay_hours: 时间衰减半衰期（小时），越高越持久
_DYNAMIC_SOURCES = {
    "conversation": {"weight": 1.6, "threshold": 0.12, "decay_hours": 72},
    "digest_session": {"weight": 2.0, "threshold": 0.10, "decay_hours": 168},
    # P1 T014 新增:segment narrative 摘要需进检索池。
    # 若无此条目,recall() 的 _ALL_SOURCES.get("digest_segment") 为 None →
    # 已写入的 segment chunks 也被跳过(recon 已确认:_index_digest_to_vectors 用
    # source=f"digest_{layer}",代码 L1199 ⇒ segment 写出来的 source 是 'digest_segment')。
    "digest_segment": {"weight": 2.0, "threshold": 0.10, "decay_hours": 168},
    "digest_day": {"weight": 1.5, "threshold": 0.12, "decay_hours": 336},
    "digest_week": {"weight": 1.2, "threshold": 0.14, "decay_hours": 720},
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

    # P3 T035: FTS5 虚拟表 + 触发器(若环境支持)。失败时静默降级(unified.fts 模块
    # 会日志提示,检索不会因此失败 — FR-001 检索非阻断点)。
    try:
        from domain.memory.memory.recall.unified.fts import ensure_fts5_schema
        ensure_fts5_schema(db)
    except Exception:
        pass  # FTS5 不可用或 import 失败 — facade 走 vector+entity 兜底

    return db


def _chunk_hash(source: str, text: str) -> str:
    return hashlib.md5(f"{source}:{text}".encode()).hexdigest()


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
    # 先删除该source的所有旧chunks，避免累积
    db.execute("DELETE FROM chunks WHERE source=?", (label,))
    
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
        ch = _chunk_hash(label, chunk)
        row = db.execute(
            "SELECT chunk_hash, file_mtime FROM chunks WHERE source=? AND chunk_hash=?",
            (label, ch),
        ).fetchone()
        if not row or row["file_mtime"] < mtime:
            new_chunks.append((ch, chunk, mtime))
    if not new_chunks:
        return 0
    texts = [c[1] for c in new_chunks]
    embeddings = _embed_texts(texts)
    if not embeddings:
        logger.debug("Embedding failed for %s, skipping index", label)
        return 0
    count = 0
    # T037: P3 让 _index_source 在写入时填 phase + source_kind(走 baseline 表)。
    # 其它字段(authority/permanence/...)留靠 backfill_slice_fields_if_needed 懒补,
    # 避免每条 INSERT 都查表(可接受,因为这层调用不频繁)。
    from domain.memory.memory.recall.unified.slice import baselines_for_source
    baseline = baselines_for_source(label)
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
            (label, ch, text, blob, mt, time.time(), insert_phase, insert_source_kind),
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
        # 1. 计算所有 chunk 的基础相似度
        if sources:
            placeholders = ",".join("?" * len(sources))
            rows = db.execute(
                f"SELECT id, source, text, embedding, created_at FROM chunks WHERE embedding IS NOT NULL AND source IN ({placeholders})",
                sources
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT id, source, text, embedding, created_at FROM chunks WHERE embedding IS NOT NULL"
            ).fetchall()

        now = time.time()
        chunk_data: Dict[int, Tuple[str, str, List[float]]] = {}  # id → (source, text, emb)
        base_scores: Dict[int, float] = {}

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
                chunk_data[cid] = (source, row["text"], chunk_emb)
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
                            chunk_data[cid] = (source, row["text"], chunk_emb)
                            final_scores[cid] = sim + boost

        # 3. 构建候选列表并按源类型分组，分层 MMR（保证对话类记忆不被摘要淹没）
        candidates = []
        for cid, score in final_scores.items():
            if cid in chunk_data:
                source, text, emb = chunk_data[cid]
                candidates.append((cid, score, source, text, emb))

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
        for cid, score, source, text, emb in candidates:
            group = _source_group(source)
            group_candidates.setdefault(group, []).append((cid, score, source, text, emb))

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


__all__ = ["recall", "ensure_indexed"]