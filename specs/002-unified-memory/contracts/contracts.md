# Contracts: 统一记忆体系

本 feature 暴露给内部 Python 模块的工具函数契约和数据库 schema 契约。
不暴露给外部 REST/事件（design doc 已明示排除这些改动）。

## 1. 检索 Facade 契约（P2 上线，P3/P4 沿用扩充）

### `unified_recall`

```python
def unified_recall(
    query: str,
    *,
    extra_context: str = "",
    attention_tokens: list[str] | None = None,
    exclude_chunk_ids: set[int] | None = None,
    budget_kind: Literal["resident", "passive", "on_demand"] = "passive",
    max_total_chars: int | None = None,  # None = 按 budget_kind 默认查表
    timeout_seconds: float = 5.0,
) -> list[dict]:
    """
    统一记忆检索入口（模板规定的 single facade, 替代三路径直连）。

    内部三路融合：
      - Route V: 向量语义（import domain.memory.recall.vector.recall，复用）
      - Route K: FTS5/BM25 词法兜底（domain.memory.recall.unified.fts）
      - Route E: attention_tokens 提权，只加权不产独立候选
    + 时序邻居候选（chunks.session_id/segment_index）—P2 部分，P3 全启用
    + 诞生链候选（chunks.derived_from）—P4 启用

    融合：RRF score = Σ 1/(60+rank_in_each_route)
    时间上限：concurrent.futures.wait(timeout=timeout_seconds)，超时返回已完成的部分
    失败降级：vector 失败 → 仅 K + E；K 失败 → 仅 V；全失败 → 返回 [] 让常驻层兜底

    返回：list of dict，每项：
      {
        "chunk_id": int,
        "body": str,           # 截 200 字符
        "source": str,
        "source_kind": str,    # P3 起填
        "phase": str,          # P1 起填
        "score": float,        # 融合后归一化
        "matched_attention_token": str | None,
        "kind_label": str,     # 用于面包屑渲染（"🎯"认知卡 / "🔍"语义 / "📅"时序 / "🔗"诞生链）
      }
    """
```

**契约不变量**：
- 任一内部路失败 → 不抛异常、内部 log，返回退化结果。
- `len(return) == 0` 是合法结果（消费侧常驻层兜底，FR-001）。
- 幂等：同 query + 同 chunks 内容 → 同排序（不含 activation 这种动态字段时）。

### `_render_breadcrumbs`

```python
def _render_breadcrumbs(results: list[dict], *, new_entities: list[str]) -> str:
    """统一面包屑渲染，替代 _inject_entity_recall 中现有 🎯/🔍 拼接逻辑。
    返回形如：
      [联想命中 — 统一召回 N 条]
      - 🎯[实体:华能蒙电 · 概念] ...
      - 🔍[语义 score=0.42] ...
      - 📅[时序 session=...] ...
      (命中 N 实体: ...; 如需更多调 recall_entity)
    """
```

## 2. 工具入口契约（sense_tools.py，签名不变、内部走 facade）

| 工具 | 现有签名 | facade 调用点 |
|---|---|---|
| `recall_memory(query, depth, limit)` | 不变 | handler 内 `results = unified_recall(query, budget_kind="on_demand", max_total_chars=limit*200)` |
| `recall_entity(entity)` | 不变 | handler 内 `unified_recall(query=entity_profile.summary, attention_tokens=[entity], budget_kind="on_demand")` |
| `sense_entity(entity)` | 不变 | 不走 facade（直接 list entity_index） |

## 3. 数据库 Schema 契约（P3 全量；P1 部分提前）

### P1 schema 增量（幂等）

```sql
-- chunks 加 phase 列（幂等：try/except sqlite OperationalError）
ALTER TABLE chunks ADD COLUMN phase TEXT DEFAULT '';

-- digest_segment 加入动态源字典（_DYNAMIC_SOURCES）
INSERT INTO 配置: "digest_segment": {"weight": 2.0, "threshold": 0.10, "decay_hours": 168}
```

### P3 schema 全量

```sql
ALTER TABLE chunks ADD COLUMN source_kind TEXT DEFAULT '';
ALTER TABLE chunks ADD COLUMN source_md5 TEXT;  -- 可选，备份
ALTER TABLE chunks ADD COLUMN session_id TEXT DEFAULT '';
ALTER TABLE chunks ADD COLUMN segment_index INTEGER;
ALTER TABLE chunks ADD COLUMN derived_from TEXT DEFAULT '[]';  -- JSON array of chunk_id
ALTER TABLE chunks ADD COLUMN derive_kind TEXT DEFAULT '';  -- promote|cluster|supersede|manual
ALTER TABLE chunks ADD COLUMN authority REAL DEFAULT 0.5;
ALTER TABLE chunks ADD COLUMN permanence REAL DEFAULT 0.3;
ALTER TABLE chunks ADD COLUMN freshness REAL DEFAULT 1.0;
ALTER TABLE chunks ADD COLUMN activation REAL DEFAULT 0.0;
ALTER TABLE chunks ADD COLUMN verification REAL DEFAULT 0.0;
ALTER TABLE chunks ADD COLUMN evidence_count INTEGER DEFAULT 0;
ALTER TABLE chunks ADD COLUMN challenge_count INTEGER DEFAULT 0;
ALTER TABLE chunks ADD COLUMN cognition_state TEXT;  -- NULL = experience
ALTER TABLE chunks ADD COLUMN supersede_by INTEGER;
ALTER TABLE chunks ADD COLUMN entity_links TEXT DEFAULT '[]';  -- JSON of names
ALTER TABLE chunks ADD COLUMN attention_tokens TEXT DEFAULT '[]';  -- JSON of names
ALTER TABLE chunks ADD COLUMN provenance TEXT DEFAULT '';

-- 历史 chunks 回填（幂等：WHERE field IS NULL/''）
UPDATE chunks SET source_kind=? WHERE source_kind='';
UPDATE chunks SET phase=? WHERE phase='';
-- (见 data-model.md 相位映射表)

-- 索引
CREATE INDEX IF NOT EXISTS idx_chunks_session ON chunks(session_id, segment_index);
CREATE INDEX IF NOT EXISTS idx_chunks_phase ON chunks(phase);
CREATE INDEX IF NOT EXISTS idx_chunks_entity_links ON chunks(entity_links);  -- LIKE 友好
```

### FTS5 schema（P2）

```sql
-- 创建前探测: PRAGMA compile_options LIKE '%ENABLE_FTS5%'
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
  USING fts5(text, source UNINDEXED, content='chunks', content_rowid='id');

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
  INSERT INTO chunks_fts(rowid, text, source) VALUES (new.id, new.text, new.source);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, text, source) VALUES('delete', old.id, old.text, old.source);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, text, source) VALUES('delete', old.id, old.text, old.source);
  INSERT INTO chunks_fts(rowid, text, source) VALUES (new.id, new.text, new.source);
END;
```

## 4. 注入点契约（agent.py / heartbeat.py）

| 调用点 | 现有内部逻辑 | 替换为 |
|---|---|---|
| `agent._inject_entity_recall` L1890 | `memories = query_entities_ranked(...)` + `_vec_recall(...)` + 30 字符去重拼接 | `results = unified_recall(combined, attention_tokens=new_entities, exclude_chunk_ids=..., budget_kind="passive")` + `_render_breadcrumbs(results, new_entities)` |
| `heartbeat._build_memory_context` entity recall | `query_entities_ranked(entities, current_context=..., limit=5)` | `unified_recall(query=context, attention_tokens=entities, budget_kind="passive", max_total_chars=...)` |
| `mark_memories_presented` | 接 memory_ids set | 接 chunk_ids set（facade 返回的 chunk_id 即替代 memory_id 语义） |

## 5. 失败 / 降级契约

| 失败场景 | 行为 | 消费侧可见 |
|---|---|---|
| 单条 `_embed_texts` 内部 URLError | 不重试 → log.warning → 返回部分成功项 | facade 仍能拿到部分 vector 候选 |
| 整体 `_embed_texts` 返回 None（全失败/无 key） | facade 跳过 vector 路 | 仅 K + E 返回（非空时） |
| FTS5 路抛异常（触发器错乱） | facade 跳过 FTS 路 | 仅 V + E 返回 |
| 整体 5s 超时 | facade 返回已完成的部分 | 可能少于完整融合结果，但非空(除非 0 命中) |
| 三路全失败 + 无时序邻居 | 返回 `[]` | 注入点跳过此次联想，依赖常驻层 |
