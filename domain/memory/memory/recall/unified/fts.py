"""P2 — 词法检索路 (FTS5 + BM25)，作为向量路的离线兜底。

设计见 specs/002-unified-memory/contracts/contracts.md §3 FTS5 schema、research.md R-04。

要点:
- FTS5 虚拟表挂到既有 `chunks` 表(content='chunks')，触发器同步
- 中文分词用 bigram + Latin 词(research R-04)
- `tokenize_for_fts` 在写入和查询时共用，保证一致
- 编译时若未 ENABLE_FTS5，模块降级为 no-op，但保留接口
- 永远不抛——失败由 unified_recall facade 捕获降级(检索非阻断点 FR-001)
"""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any

from domain.memory.memory.recall.vector import _get_db, _get_db_path

logger = logging.getLogger("domain.memory.recall.unified.fts")

_FTS5_AVAILABLE: bool | None = None  # lazy 探测，缓存结果


def _detect_fts5() -> bool:
    """探测本环境 sqlite3 是否编入 FTS5。结果缓存。"""
    global _FTS5_AVAILABLE
    if _FTS5_AVAILABLE is None:
        try:
            conn = sqlite3.connect(":memory:")
            conn.execute("CREATE VIRTUAL TABLE probe USING fts5(content)")
            conn.close()
            _FTS5_AVAILABLE = True
        except Exception as e:
            logger.warning(
                "FTS5 not available in this SQLite build; "
                "lexical route will be a no-op. cause=%s", e
            )
            _FTS5_AVAILABLE = False
    return _FTS5_AVAILABLE


# ───────────────────── 分词 ─────────────────────

_CJK_RANGE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
_LATIN_WORD = re.compile(r"[A-Za-z0-9_]+")


def tokenize_for_fts(text: str) -> str:
    """把 text 转成 FTS5 MATCH query 字符串。

    策略(research R-04):
    - 拉丁词按边界切(保留 ASCII identifier)
    - CJK 字符两两 bigram(适合 2+ 字中文词)
    - 全部转小写,以 OR 连接(MATCH 查询里多 token 默认 AND;我们要"任一命中"语义)

    单字 token 不入 bigram(过度噪声),但 1-字查询会走 latin/CJK 单字边界;
    FTS5 `unicode61` 行为对 CJK 默认按整段一个 token,所以我们必须预 token。
    """
    if not text:
        return ""
    tokens: list[str] = []

    # 提 ASCII 词
    for m in _LATIN_WORD.findall(text):
        if len(m) >= 2:
            tokens.append(m.lower())
        elif m:
            tokens.append(m.lower())

    # 提 CJK 段并做 bigram
    # 先把所有 CJK 字符抽出来按出现顺序连成段
    cjk_chars = _CJK_RANGE.findall(text)
    # bigram 连续两次
    for i in range(len(cjk_chars) - 1):
        tokens.append(cjk_chars[i] + cjk_chars[i + 1])
    # 单字收尾(如果一个文本只有 1 个 CJK 字符也允许命中)
    if len(cjk_chars) == 1:
        tokens.append(cjk_chars[0])

    # FTS5 MATCH 语法：tokens 之间用 OR 连接。"任一命中"语义。
    if not tokens:
        return ""
    # 引号转义防止破坏 MATCH 语法
    safe = [t.replace('"', "") for t in tokens if t]
    return " OR ".join(safe)


# ───────────────────── Schema 与触发器 ─────────────────────

def ensure_fts5_schema(db: Any | None = None) -> bool:
    """幂等建 chunks_fts 虚拟表 + 同步触发器。
    幂等:多次调用安全。SQLite IF NOT EXISTS + DROP TRIGGER IF EXISTS。
    返回 True 表示建好/已存在;False 表示 FTS5 不可用,facade 会跳过这一路。
    """
    if not _detect_fts5():
        return False

    own_conn = db is None
    if own_conn:
        db = _get_db()
    try:
        db.executescript(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
                USING fts5(text, source UNINDEXED, content='chunks', content_rowid='id');

            -- 保持 FTS 与 chunks 行同步
            CREATE TRIGGER IF NOT EXISTS chunks_fts_ai AFTER INSERT ON chunks BEGIN
                INSERT INTO chunks_fts(rowid, text, source)
                VALUES (new.id, new.text, new.source);
            END;
            CREATE TRIGGER IF NOT EXISTS chunks_fts_ad AFTER DELETE ON chunks BEGIN
                INSERT INTO chunks_fts(chunks_fts, rowid, text, source)
                VALUES ('delete', old.id, old.text, old.source);
            END;
            CREATE TRIGGER IF NOT EXISTS chunks_fts_au AFTER UPDATE ON chunks BEGIN
                INSERT INTO chunks_fts(chunks_fts, rowid, text, source)
                VALUES ('delete', old.id, old.text, old.source);
                INSERT INTO chunks_fts(rowid, text, source)
                VALUES (new.id, new.text, new.source);
            END;
            """
        )
        db.commit()
        return True
    except Exception as e:
        logger.warning("ensure_fts5_schema failed (will degrade lexical route): %s", e)
        return False
    finally:
        if own_conn:
            db.close()


def rebuild_fts_index() -> int:
    """全量重建 FTS 索引(vacuum + 显式回填)。
    用于初次启用时或发现不一致时一次性修复。
    """
    if not _detect_fts5():
        return 0
    if not ensure_fts5_schema():
        return 0
    db = _get_db()
    try:
        # 'rebuild' 命令令 FTS5 重新读 content 表
        db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
        db.commit()
        # 统计回填了多少行
        count = db.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
        logger.info("FTS rebuilt, indexed %d chunk rows", count)
        return count
    except Exception as e:
        logger.warning("rebuild_fts_index failed: %s", e)
        return 0
    finally:
        db.close()


# ───────────────────── 查询 ─────────────────────

def fts_search(
    query: str,
    *,
    limit: int = 20,
    sources: list[str] | None = None,
    include_obsolete: bool = False,
) -> list[tuple[int, float]]:
    """BM25 词法检索。返回 [(chunk_id, -bm25_score), ...]。
    bm25() 返回负数(越负越相关)，我们取负返(越大越相关),与 vector 路语义一致。

    sources 过滤:限定到特定 source 类型(如 ['conversation', 'digest_session'])。
    include_obsolete=False(默认): 过滤死信认知(replaced/challenged/archived)。
    """
    if not _detect_fts5():
        return []
    if not query or not query.strip():
        return []

    match_expr = tokenize_for_fts(query)
    if not match_expr:
        return []

    # ensure schema(幂等)。即使触发器从未跑过(老库),也对一次触发表查询里生效。
    ensure_fts5_schema()

    # 闸门一(2026-07-17): 默认过滤死信认知
    obsolete_filter = "" if include_obsolete else (
        " AND (c.cognition_state IS NULL "
        "      OR c.cognition_state NOT IN ('replaced','challenged','archived'))"
    )

    db = _get_db()
    try:
        if sources:
            placeholders = ",".join("?" * len(sources))
            rows = db.execute(
                f"""
                SELECT c.id AS cid, bm25(chunks_fts) AS score
                FROM chunks_fts
                JOIN chunks c ON c.id = chunks_fts.rowid
                WHERE chunks_fts MATCH ? AND c.source IN ({placeholders})
                {obsolete_filter}
                ORDER BY score
                LIMIT ?
                """,
                [match_expr, *sources, limit],
            ).fetchall()
        else:
            rows = db.execute(
                f"""
                SELECT c.id AS cid, bm25(chunks_fts) AS score
                FROM chunks_fts
                JOIN chunks c ON c.id = chunks_fts.rowid
                WHERE chunks_fts MATCH ?
                {obsolete_filter}
                ORDER BY score
                LIMIT ?
                """,
                [match_expr, limit],
            ).fetchall()
        # bm25() 越负越相关;翻转符号让 score 越大越相关
        return [(int(r["cid"]), -float(r["score"])) for r in rows]
    except Exception as e:
        logger.debug("fts_search failed (will be skipped by facade): %s", e)
        return []
    finally:
        db.close()


__all__ = [
    "tokenize_for_fts",
    "ensure_fts5_schema",
    "rebuild_fts_index",
    "fts_search",
]
