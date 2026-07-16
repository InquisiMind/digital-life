# Plan Phase 0: Research — 统一记忆体系

> 把 plan Technical Context 里的"未知"和设计岔口做成研究决策记录。所有决策有调研依据，实现期不回头再争论。

## R-01 · segment 摘要是否真的从未被索引

**Decision**: 确认 segment narrative 不进 chunks 表，需补向量索引。

**Rationale**: `consolidation_runtime.py` 中：
- `_lazy_generate_segment_narrative` / `_generate_segment_narratives_worker` 把 segment narrative `INSERT INTO memory_layers WHERE layer='segment'`（L637/L671 那条线）。
- 同模块 `_index_digest_to_vectors(digest_text, layer, period)`（L1174）只在两条路径被调：session 摘要（L1256）、day 摘要（L1275）。**never called with layer='segment'**。
- 对照 `recall/vector/__init__.py` 的 `_DYNAMIC_SOURCES` 字典无 `digest_segment` 项。

**Alternatives considered**:
1. 仅给 memory_layers 加索引但不动 chunks → 破坏统一载体原则、查两库，reject。
2. 把 segment 当 session 一类 → 语义混淆（session 是整体摘要，segment 是段叙事），reject。
3. **加 `digest_segment` 到 `_DYNAMIC_SOURCES` 并在 worker 里调 `_index_digest_to_vectors(narrative, "segment", period)`** → 采用。

## R-02 · `update_entity_index_from_narrative` 死代码段

**Decision**: 整段重写，改用 `sync_entity_from_source`。

**Rationale**: L707-726 实际是失效代码：
- L710-713 `from ... import add_entity` → ImportError 被外层 L725 `except Exception: pass` 吞。
- 即使 import 成功，L714 `extract_entities_from_context(narrative)` 返回 `list[str]`，但 L717 `add_entity(name=entity.get("name"))` 当字典调用 → AttributeError。
- 双重死。

**Alternatives considered**:
1. 在 entity_index.py 里真补一个 `add_entity` → 引入新 API，与已有 `sync_entity_from_source` 重复（设计文档明确：sync 已是源系统自动调用的入口）；reject。
2. **改调 `sync_entity_from_source(name=entity, entity_type="concept", summary=narrative[:200], aliases=[])`** → 与设计文档 §1 的"4 源自动实体"体系一致；采用。

## R-03 · 嵌入 batch 失败策略

**Decision**: 留成功项；不重试；failed→warning。

**Rationale**（精确对齐 Clarifications Q3）:
- 现 `_embed_texts` L138 all-or-nothing，与 answers "批内保留成功" 相反。
- 429 重试会引起 RPS 翻倍触发新 429 风暴；answers 明确"不重试"。
- 整体检索时间上限 5s 是另一条铁律（answers）；单次 HTTP timeout 30s 已经超过上限，必须降。

**Alternatives considered**:
1. 指数退避 3 次 → 违反"召回非阻断点"（FR-001/FR-104），reject。
2. 直接透传失败、不重试、加 5s 上限 → 采用。

**具体 timeout**:
- `_embed_texts` 内 `urllib.request.urlopen(req, timeout=8)`（留 3s 给 FTS5 + RRF 融合）。
- 整体 `unified_recall()` 用 `signal.SIGALRM` 或 thread + `concurrent.futures.wait(timeout=5)` 上限。后者跨平台，采用线程池。

## R-04 · FTS5 中文分词选择

**Decision**: bigram + Latin 词，自实现 `tokenize_for_fts`。

**Rationale**:
- FTS5 默认 `unicode61` 对中文不分词（整段当一个 token）。
- jieba 引入第三方依赖（与"无第三方依赖"约束冲突）。
- bigram (CJK 字符对) + 词边界(ASCII `[A-Za-z0-9_]+`) 是 sqlite 全文检索中文的工业实践（Mediawiki、notion 近年做法），自实现 < 30 行。

**Alternatives considered**:
1. `fts5(contentless, unicode61 remove_diacritics 2 tokenchars '...')` → 仍按 ASCII 边界；reject。
2. jieba → 装新依赖；reject。
3. **手写 `tokenize_for_fts` 同时用于索引写入和查询** → 采用。

## R-05 · chunks schema 迁移时机与原子性

**Decision**: `ALTER TABLE` 幂等 + 老 chunks 走默认值 + 默认相位按 source 反推。

**Rationale**:
- SQLite `ALTER TABLE ADD COLUMN` 不带 IF NOT EXISTS；"列已存在" 抛 `sqlite3.OperationalError('duplicate column name')`。try/except 这一行用作幂等保护。
- 历史 chunks 默认 phase 按 source 映射：`rules|lessons|self_knowledge` → `cognition`；其它 → `experience`（按设计文档相位二元）。
- 改不动既有数据行内容，只加列+回填（PER POINT：回填用 `UPDATE chunks SET phase=? WHERE phase=''`，幂等）。

**Alternatives considered**:
1. 重建表（CREATE TABLE + INSERT SELECT + DROP + RENAME）→ 太重、停机风险；reject。
2. **ALTER + 默认值 + 懒回填** → 采用。

## R-06 · 检索硬上限跨平台实现

**Decision**: `concurrent.futures.ThreadPoolExecutor` + `wait(timeout=5)`。

**Rationale**:
- `signal.SIGALRM` 只 UNIX；macOS 行但 Windows 不可。
- 设计文档 §"检索永远非阻断点"要跨平台。
- 三路检索并发跑、`concurrent.futures.wait(timeout=5)`、超时返回已完成的部分（最大保真）。

## R-07 · entity_index 退位但不动打分公式

**Decision**: `query_entities_ranked` 4 因子公式不动；entity_index 退为"查询期提供 attention_tokens 列表"给 facade 提权。

**Rationale**: 设计文档明确"entity_index 退位为认知骨架"。打分公式（recency × 0.35 + ...）继续用于 entity 内部归并，但 facade 不再调它作为独立路；facade 让 entity hits 仅作为"chunk attention_tokens 命中 → 提权"。打分公式本身没坏、不动（兼容老 sense_entity 工具）。

## 总结

| Unknown | 决策 |
|---|---|
| segment 索引 | 加 `digest_segment` + 在 worker 调 `_index_digest_to_vectors` |
| add_entity 死代码 | 用 `sync_entity_from_source` 重写 |
| 嵌入失败 | 留成功项、不重试、timeout=8 |
| FTS5 分词 | bigram+Latin，自实现 |
| schema 迁移 | 幂等 ALTER + 按 source 反推 phase |
| 硬上限跨平台 | ThreadPoolExecutor + wait(timeout=5) |
| entity_index 退位 | 公式不动、attention_tokens 提权 |

所有 NEEDS CLARIFICATION 已解决，进入 Phase 1 设计。
