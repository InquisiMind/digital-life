# Implementation Plan: 统一记忆体系（Unified Memory）

**Branch**: `002-unified-memory` | **Date**: 2026-07-16 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `specs/002-unified-memory/spec.md`
**Design reference**: [`docs/design/unified-memory.md`](../../docs/design/unified-memory.md)

## Summary

把分散异构的记忆沉淀，统一物化成同一份切片（经历/认知两种相位），建成"整段语义 + 词法兜底 + 注意力提权"三路融合的检索面，并立起认知的状态机式演化生命周期。**分四阶段交付**：P1 缺口补丁、P2 统一检索面、P3 切片层重构 + 参数引擎、P4 认知演化。

技术取向（已对齐 clarify 三问）：
- 检索**不做重试**，429/超时直接透传降级到词法；整体检索**设硬时间上限（默认 5s）**。
- P2 阶段**不引入 tokenizer**，token 预算留给 P3 实测后定档。
- P1 修 segment 索引时**顺便预埋 P3 相位字段**（`phase=experience`），P1 消费侧不读、schema 已就位，避免 P3 二改表。

## Technical Context

**Language/Version**: Python 3.11（按 `docs/development/python-coding-standards.md`）
**Primary Dependencies**:
- 既有:`sqlite3` (内置)、`urllib.request` (嵌入调用)、`struct` (embedding 序列化)
- 既有:智谱 Embedding-3 (2048 维), 外部 API
- 无需新增第三方依赖（FTS5 由 `sqlite3` 内建提供）
**Storage**:
- `<data>/memories/memory_vectors.db`（核心：`chunks` + `associations` 表 → P3 加字段 + 新增 FTS5 虚拟表）
- `<data>/memories/memory_layers.db`(digest/segment 摘要来源)
- `<data>/memories/entity_index.json`（认知骨架，P3 起挂 entity_links）
- `<data>/state.db.messages`（对话原始数据，**不进切片**，仅 P3 迁移时读）
**Testing**:
- pytest + `domain/memory/` 现有测试基线
- `scripts/eval_memory_recall.py` 作为召回质量对照（P2 起补独立金标集）
- 嵌入 API 用 fake/mock，不依赖真实智谱（CI 友好）
**Affected Layers**:
- `domain/`(memory/recall、memory/summaries、memory/consciousness/entity_index) — 主战场
- `infrastructure/`(ai/agent.py 注入点、persistence 监控页接口)
- `application/`(console/monitor 6 处 chunks 查询)
- `interfaces/`(tools/sense_tools.py 三个工具入口切到统一检索)
- `docs/`(architecture/current-system、operations/memory-maintenance、设计文档)
**Constraints**:
- **检索永远非阻断点**(FR-001)：硬时间上限 5s、不重试、降级兜底
- **管线统一拍平**(FR-002)：新增记忆类型不写代码分叉
- **可溯源永不硬删**(FR-003 / FR-404)：取代/归档留链
- 不动三个对话库合并、不动 markdown 入库(明示排除)
- 不依赖外部 ANN（chunk 量级约 1500，全扫 python cosine 当前可接受；P3 评估升级）

## Constitution Check *(mandatory gate)*

- **Layer ownership and dependency direction**: PASS。全部改动落在 `domain/`(memory/recall 新模块、memory/summaries 完进消化内、entity_index 仅扩 sync)、`infrastructure/ai/agent.py`(消费侧切面)、`application/console`(监控同步)、`interfaces/tools`(工具入口)。`domain/` 不引 HTTP/CLI/UI；外部 API 调用继续在 `domain/memory/recall/vector/` 既有位置（vector recall 已是技术实现层例外，不在 domain 业务规则中新增此类依赖）。
- **Orchestration versus execution boundary**: N/A。本 feature 不动 `domain/orchestration/planning`（按仓库约束该处是产品代码非开发流程）。
- **Contract synchronization**: 需跟踪（风险点 R-2）。改 `chunks` schema、检索 facade 入口签名、注入点 → 必须同步：6 处 console 查询、3 个 sense 工具、agent `_inject_entity_recall`、heartbeat `_build_memory_context`、eval 脚本 imports。Phase 转 implement 前列出所有 producer/consumer。
- **Mutable runtime data safety**: 例外（已跟踪 R-1）。需对 `apps/<id>/data/memories/*.db` 现有数据写**幂等 ALTER + 迁移**：新字段加默认值、FTS5 表不存在时 `CREATE IF NOT EXISTS`、保留旧数据、不擦 chunks 行。`update_entity_index_from_narrative` 死函数段（实际是失效代码）整段重写。
- **Risk-based verification**: 见 §Verification Plan。按风险分层：P1 纯函数单测、P2 facade 契约测、P3 schema 迁移测、P4 状态机跃迁测。
- **Documentation synchronization**: 需跟踪（风险点 R-4）。`docs/architecture/current-system.md`、`docs/operations/memory-maintenance.md`、设计文档 `docs/design/unified-memory.md` 需随阶段同步。每个阶段都更新"当前能力"小节。
- **Compatibility and simplicity**: PASS。FTS5 走 SQLite 内建、不引第三方；不引入与现有三路径并存的兼容 shim（统一后即替换，不留双轨）。
- **Python quality standards**: PASS。所有改动按 `docs/development/python-coding-standards.md`：类型注解、SQL 参数化、嵌入失败不吞(改 warning)、异常分级。
- **Unrelated work preservation**: PASS。spec 显式排除对话库合并、markdown 入库、`apps/<id>/data` 数据本身修改（仅做 schema 幂等升级）。

## Impact Analysis *(mandatory)*

- **Architecture Boundary Check**:
  - 检索融合新模块位置:`domain/memory/recall/unified/`（与 `domain/memory/recall/vector/`、`domain/memory/recall/__init__.py` 同级）。
  - owner: `domain/memory/recall/`；消费侧 `infrastructure/ai/agent.py`、`domain/lifecycle/heartbeat.py`、`interfaces/tools/sense_tools.py`。
  - 边界测试: P2 起 facade 契约必须有 `test_unified_recall_contract` 验证三路径都过它。
- **Contract Impact**:
  - producer: 新增 `unified_recall(query, *, phase, attention_tokens, exclude_ids, budget_kind) -> list[Slice]` 单 facade。
  - consumer: `_inject_entity_recall`、`_build_memory_context`、`recall_memory` 工具、`recall_entity` 工具、`sense_entity` 工具、eval。
  - 同步项: 全部 consumer 改走 facade；旧召回/向量召回/实体召回函数可保留但标记 `# Deprecated: use unified_recall`，统一回归后清理。
- **Multi-instance Impact**: N/A（不变——memory 子系统继续 per-instance 隔离，第九章 9.7 跨实例共享已显式排除）。
- **Runtime Data Safety**:
  - `chunks` 加列：`ALTER TABLE chunks ADD COLUMN phase TEXT DEFAULT '';` 等，所有新列携带默认值。
  - FTS5 虚拟表：`CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts ...` + INSERT/UPDATE/DELETE 触发器。
  - 数据回填：旧 chunks 按其 `source` 字段映射默认 phase（`digest_session/conversation/identity/...` → `experience`；`rules/lessons/self_knowledge` → `cognition`）。
  - 回滚降级：迁移失败则表结构不变 + 老召回路径继续工作（向后再退一版即恢复）。
- **Migration and Compatibility**:
  - `update_entity_index_from_narrative` 死代码段重写，行为**严格更好**（从"静默抛 ImportError"变为"按 entity 名经 sync_entity_from_source 写入"），无旧 consumer 依赖它的工作正常行为。
  - `_embed_texts` all-or-nothing 改"留成功项"，行为严格更好。
  - 不修改对外 REST/事件契约。
- **Documentation Impact**:
  - `docs/architecture/current-system.md`: 加"统一检索 facade / FTS5 / 认知状态机"小节。
  - `docs/operations/memory-maintenance.md`: 新"调参、健康检查、降级诊断"小节。
  - `docs/design/unified-memory.md`: 与实现期发现的小偏差即时回填。
  - 三个文档目录的 README 若引用记忆主题需加文 load trigger。
- **Python Quality Impact**:
  - 公共签名加 `-> list[Slice]`、`-> Optional[...]`。
  - SQL 全参数化（FTS5 同样）。
  - 嵌入失败从 `logger.debug(...)` 升为 `logger.warning(...)`；保留训练用 debug 的对应。
  - 不做全仓 reformat，仅本 feature 改动代码逐行满足新规。

## Project Structure

```text
domain/memory/recall/
├── __init__.py                      (旧 TF-cosine 兜底, 保留作 fallback)
├── vector/__init__.py               (改造: _embed_texts 留成功项 + 不重试 timeout)
└── unified/                         **新增** (P2)
    ├── __init__.py                  (unified_recall facade + RRF 融合)
    ├── fts.py                       **新增** (P2: FTS5 关键词路)
    └── slice.py                     **新增** (P3: 切片原子 + 参数引擎)

domain/memory/summaries/
└── consolidation_runtime.py         (改造: segment 索引、update_entity_index_from_narrative 重写)

domain/memory/consciousness/
└── entity_index.py                  (仅扩 sync_entity_from_source, 不改打分公式)

domain/lifecycle/
└── heartbeat.py                     (改造: _build_memory_context 走 facade)

infrastructure/ai/
└── agent.py                         (改造: _inject_entity_recall 走 facade + 检索时间上限)

application/console/
└── monitor.py                       (6 处 chunks 查询改字段名, 不动业务)

interfaces/tools/
└── sense_tools.py                   (recall_memory/recall_entity/sense_entity 走 facade)

docs/design/unified-memory.md        (与实现偏差同步)
docs/architecture/current-system.md  (新增"统一检索/认知状态机"小节)
docs/operations/memory-maintenance.md (新增调参/诊断小节)
```

**Structure decision**: 新增模块放 `domain/memory/recall/unified/`，与既有 recall 子层平级，符合"技术检索实现 = domain 的 memory 子域内、不跨染 domain 业务规则"。schema 改动落到 `domain/memory/recall/vector/` 既有 DB 路径上（沿用同一份 memory_vectors.db），不开新库以符合"统一载体 + 不冗余"原则。监控页适配留在 `application/console/` 因为它属于展示适配。

## Implementation Approach

按 spec 的 P1→P2→P3→P4 顺序，每阶段独立验收、可回滚：

### P1 · 缺口补丁（起点，独立可上线）

1. **修 `_embed_texts`（all-or-nothing → 留成功项）+ 不重试 + 失败升级 warning**
   - 文件：`domain/memory/recall/vector/__init__.py` L138-141
   - 改：去掉 `if all(e is not None ...)`；429/超时/网络 error 仅 log.warning 后 return 部分结果。
2. **降单次 HTTP timeout：30s → 与检索整体 5s 上限配套**
   - 同文件 L132：`timeout=30` → `timeout=8`（留余量给其他子路径完成）。新增 `unified_recall` 整体上限 5s 在 P2 实施，P1 先把单次 timeout 调下来避免一次调用吃掉全部预算。
3. **修 segment 索引蒸发**
   - 文件：`domain/memory/summaries/consolidation_runtime.py` `_generate_segment_narratives_worker`（L804+）
   - 改：成功写回 segment narrative 后补一行 `_index_digest_to_vectors(narrative, "segment", period)`。
   - P1 预埋相位：扩展 `_index_digest_to_vectors` 在写入时给 `chunks` 加 `phase` 字段（虽然 P1 不读）；schema 已迁移则填，未迁移则忽略（向前兼容）。
4. **修 `update_entity_index_from_narrative` 死函数（整段重写）**
   - 文件：同上 L707-726
   - 替换：删 import 错误的 `add_entity`；用 `extract_entities_from_context`（返回 str 列表）→ 每个实体调 `sync_entity_from_source(name=entity, entity_type=..., summary="", aliases=[])`。
   - 类型按 `narrative` 上下文启发猜测（默认 "concept"）。
5. **加 P1 chunks schema 迁移（幂等）**
   - 在 `_get_db()` 第一次调用时执行 `ALTER TABLE chunks ADD COLUMN phase TEXT DEFAULT ''`（用 try/except sqlite OperationalError 忽略"列已存在"）。
6. **`scripts/eval_memory_recall.py` 跑 P1 后基线**作为对比锚点（不改脚本，仅记录结果）。

### P2 · 统一检索面（facade + 三路融合 + 词法兜底）

1. **建 `domain/memory/recall/unified/__init__.py`：`unified_recall()` facade**
   - 内部三路：vector（`recall/vector/__init__.py:recall` 复用）、FTS5（新 `fts.py`）、entity attention（基于 `extract_entities_from_context`）。
   - RRF 融合：`score = Σ 1/(k+rank_i)`，候选去重按 chunk_id。
   - 整体检索硬上限 5s（P1 已调单次 timeout，整体上限在此实现）：每路在 thread/timeout 上限内执行，超时返回已累积的。
2. **建 `domain/memory/recall/unified/fts.py`**
   - `CREATE VIRTUAL TABLE chunks_fts USING fts5(text, source UNINDEXED, content='chunks', content_rowid='id')`。
   - 触发器：`chunks` INSERT/UPDATE/DELETE → 同步到 FTS。
   - 中文分词：bigram + Latin 词；`tokenize_for_fts(text)` 模块内函数，写入和查询共用。
   - 查：`SELECT id, bm25(chunks_fts) FROM chunks_fts WHERE chunks_fts MATCH ?` → 返回 `(chunk_id, -bm25_score)`。
3. **改注入点**
   - `infrastructure/ai/agent.py` `_inject_entity_recall`：原本 Route A + Route B 拼接 → 调 `unified_recall(combined, attention_tokens=new_entities)` 单返回，按面包屑格式（已存在的 🎯/🔍 标注重构）渲染。
   - `domain/lifecycle/heartbeat.py` `_build_memory_context`：同样切到 facade。
4. **改三 sense 工具**
   - `interfaces/tools/sense_tools.py` `recall_memory`/`recall_entity`/`sense_entity` 三个 handler 内部统一 import facade；signature 不变（向后兼容工具调用方）。
5. **监控页适配**
   - `application/console/monitor.py` 6 处 chunks 查询若引用新字段则同步（多数只读 id/source/text，P1 phase 字段不影响）。
6. **独立金标评估集**
   - `scripts/eval_memory_recall.py` 改：保留旧 entity/vector 基线 + 新增手动标注 30 条金标（含跨语义、专名、时序三类）。

### P3 · 切片层重构 + 参数引擎（自动驱动器半边）

1. **统一原子 `domain/memory/recall/unified/slice.py`**
   - `Slice` dataclass：body/source_kind/phase/authority/permanence/freshness/activation/verification/evidence_count/challenge_count/entity_links/attention_tokens/time_meta/derivation。
2. **chunks schema 完整升级（幂等 ALTER）**
   - 新增列：source_kind/authority/permanence/freshness/activation/verification/evidence_count/challenge_count/entity_links(JSON)/attention_tokens(JSON)/session_id/segment_index/derived_from(JSON)。
   - 历史回填：按 `source` 映射默认值（experience vs cognition）。
3. **归一器集合**
   - `_index_digest_to_vectors` 升级为正经 slice 归一器；`sync_entity_from_source` 改为不写 entity_index 而是产 slice；新加 project/todo/profile 归一器。
4. **参数引擎（自动驱动器）**
   - 时衰、参考计数、归档阈值统一函数：`update_slice_dynamics(slice, signals, now)`。所有写入点改调它。
5. **三类连边物化**
   - 时序连续:`session_id` / `segment_index` 字段直接生成边。
   - 诞生链:`derived_from[]` JSON 字段。
   - 内容相似:既有 embedding。
   - `associations` 表保留作共现增强边。
6. **监控页加 slice 属性展示**。

### P4 · 认知演化生命周期（模型驱动半边）

1. **认知状态机**
   - `domain/memory/recall/unified/cognition.py`:`CognitionState` 枚举 + `promote/revise/supersede/archive/cluster_born` 跃迁函数。
2. **三条铁律落码**
   - access 只动 activation（不动 authority/verification）。
   - 结构性强化（verified/falsified）只对 phase=cognition 生效。
   - 晋升（memory_hygiene/dream 周期）才产 model_promote。
3. **健康遗忘可见性衰减**
   - 排序权重因子加 `visibility_decay(evidence_count, challenge_count, δt)`。
4. **聚类诞生新认知**
   - `cluster_cognitions()` 取近义认知簇 → 喂模型 → 写新 cognition slice + derived_from 链。可关闭开关 + 失败降级标记待复审。
5. **memory_hygiene skill 扩展**
   - 调用 `promote/cluster_born` 接口；产出失败 → 保留原样 + 标记待复审（FR-406）。

## Verification Plan *(mandatory)*

- **Focused checks**:
  - P1: `test_embed_texts_partial_success`、`test_embed_texts_timeout_no_retry`、`test_segment_narrative_indexed`、`test_update_entity_index_from_narrative_live`（用 fixture 空索引，断言实体被写入）。
  - P2: `test_unified_recall_rrf_fusion`、`test_unified_recall_vector_failure_degrades_to_fts`、`test_unified_recall_hard_timeout`、`test_three_sense_tools_use_facade`（静态扫描 import）。
  - P3: `test_chunks_schema_migration_idempotent`、`test_slice_defaults_by_source`、`test_history_backfill`、`test_update_slice_dynamics`。
  - P4: `test_access_does_not_reinforce`（铁律一）、`test_only_cognition_reinforced`、`test_supersede_chain_preserved`、`test_cluster_born_normal_vs_failure`。
- **Python checks**: 每阶段改动文件运行 `python -m pytest tests/<corresponding>`。可用的: `mypy`/`ruff`（如有，未达不阻塞但记 unverified）。
- **Boundary/contract checks**: P2/P3 跨层 → 跑 `tests/` 下涉及 memory/recall 的边界/契约测试；新增 facade 契约测。
- **Broader checks**: P3 schema 迁移完成后跑全 `tests/`；P4 上线前跑全套。
- **Acceptance evidence**:
  - SC-001/P1: 嵌入 0 token 残留 → 召回仍非空（手动 trigger `recall_memory` 工具看返回）。
  - SC-002: eval_recall 输出新基线 JSON 对比修复前。
  - SC-003: 触发 429 模拟 → grep 控制台日志见 warning。
  - SC-004: 在新增金标 30 例上 facade 召回率 ≥ 单路并集。
  - SC-007: 静态扫描"衰减"相关函数集中在 `slice.py` 一处。
  - SC-008/P4: 单测 `test_access_does_not_reinforce` 验证。

## Complexity and Risk Tracking

| Exception or Risk | Why Needed | Mitigation and Verification |
| --- | --- | --- |
| R-1 三阶段对 `apps/<id>/data` 做 schema 幂等升级（IV 例外） | 否则 P3 无法落地统一切片字段 | 所有 ALTER 用默认值；老路径继续可用；迁移失败→表不变+测试 `test_migration_idempotent`；不撤任何数据 |
| R-2 跨 6 文件 producer/consumer 同步（III） | facade 入口签名变更波及广泛 | plan 列全 consumer，tasks 阶段逐一勾对；契约测断言三 sense 工具与两注入点都过 facade |
| R-3 FTS5 触发器在 macOS Python sqlite3 兼容性 | FTS5 是指定平台默认编译支持的扩展，但需验证本仓环境 | 启动时 `CREATE VIRTUAL TABLE IF NOT EXISTS` + 探测 `PRAGMA compile_options LIKE '%ENABLE_FTS5%'`；未启用则降级纯 TF-cosine（既有 `recall/__init__.py`），不阻塞 |
| R-4 设计文档与实现偏差漂移（VI） | 长周期改造，文档易滞后 | 每阶段 merge 前同步 `docs/design/unified-memory.md`、`docs/architecture/current-system.md`、操作手册；偏差纳入 Clarifications |
| R-5 segment 索引+预埋 phase 后，旧 eval golden 数据混入新老 chunks | 评估对照基线可能被污染 | 评估脚本加时间戳分段，分别跑"改前/改后"两批 chunks；保留报告 JSON 对比 |
| R-6 检索硬上限 5s 对极慢 agent round 的影响 | 若用户在 mid-session 联想触发，超时是否会让对话中断 | 上限作用于 facade 函数返回，不升级到 runtime 异常；返回部分结果即正常 inject；测 `test_unified_recall_hard_timeout` 验证 |
