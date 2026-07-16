# Data Model: 统一记忆体系

## 实体概览

| 实体 | 性质 | 存储载体 | 何时创建/变化 |
|---|---|---|---|
| **Slice（切片）** | 检索/演化/投递原子 | `<data>/memories/memory_vectors.db.chunks`（升级后） | 任何记忆源被消化/写入时 |
| **Cognition Node（认知节点）** | 导航骨架 | `<data>/memories/entity_index.json` 中 entities | 源系统 contacts/projects/skills/sync 时 |
| **Edge（连边）** | 由切片字段派生 | chunks 字段 + `associations` 表 | 切片写入即隐式生成一二三类边；共现联想随召回累加 |
| **Embedding** | 查询/内容向量 | `chunks.embedding` BLOB(2048×8 bytes) | 切片写入时统一嵌入 |
| **FTS5 Index** | 词法倒排 | `chunks_fts` 虚拟表 + 触发器 | 切片写入时同步（与 chunks 表同步） |

## 切片原子（P3 完整版；P1 仅 phase；P2 不改字段）

```
Slice {
  # 身份与内容
  id:                 INTEGER PK   (chunks.id)
  body:               TEXT         (chunks.text)
  chunk_hash:         TEXT         (chunks.chunk_hash, source+text md5)
  source:             TEXT         (chunks.source)
  source_kind:        TEXT         # 新增: "narrative"|"digest"|"conversation"|"rule"|"lesson"|"profile"|...
  phase:              TEXT         # P1 新增: "experience"|"cognition"

  # 向量与词法
  embedding:          BLOB         (chunks.embedding, 2048维 packed doubles)
  # FTS5 索引 chunks_fts 同步 content='chunks'; 触发器维护

  # 时序与诞生链（三类连边的一二类）
  time_meta: {
    created_at:       REAL         (chunks.created_at)
    session_id:       TEXT         # 新增
    segment_index:    INTEGER      # 新增, nullable
  }
  derivation: {
    derived_from:     TEXT(JSON)   # 新增: ["chunk_id|memory_id", ...] 认知专属
    derive_kind:      TEXT         # 新增: "promote"|"cluster"|"supersede"|"manual" 认知专属
  }

  # 参数（演化引擎字段）
  authority:          REAL DEFAULT 0.5    # 新增: 0.0~1.0
  permanence:         REAL DEFAULT 0.3    # 新增: 0.0~1.0, 抗衰抗覆盖
  freshness:          REAL DEFAULT 1.0    # 新增: 当前的时效权重，随Δt×(1-permanence)衰减
  activation:         REAL DEFAULT 0.0    # 新增: 0~1, 最近被召回的短期活跃度(分钟级衰减)
  verification:       REAL DEFAULT 0.0    # 新增: 浮点验证计数

  # 认知专属（P4）
  evidence_count:     INTEGER DEFAULT 0
  challenge_count:    INTEGER DEFAULT 0
  cognition_state:    TEXT DEFAULT NULL   # nascent/active/reinforced/revising/challenged/archived/replaced/higher
  supersede_by:       INTEGER DEFAULT NULL (chunks.id 引用)

  # 导航（连边第三类 + 注意力提权）
  entity_links:       TEXT(JSON)   # 新增: ["entity_name", ...]
  attention_tokens:   TEXT(JSON)   # 新增: ["人名","项目名",...] 仅作提权
  origin, provenance: TEXT         # 来自哪个源、哪次消化
}
```

## 阶段性字段启用计划

| 字段 | P1 | P2 | P3 | P4 |
|---|---|---|---|---|
| phase | 写入 / 不读 | 写入 / 不读 | 读 + 写入所有归一器 | 用作强化边界 |
| source_kind | N/A | N/A | 写入 | 沿用 |
| authority/permanence/freshness/activation/verification | N/A | N/A | 写入 + 自动驱动器算 | 模型驱动器调 |
| session_id/segment_index/derived_from | N/A | 读(生成时序候选) | 写入 | 写入(supersede 链) |
| entity_links/attention_tokens | N/A | 读(提权候选) | 写入 | 沿用 |
| evidence_count/challenge_count/cognition_state/supersede_by | N/A | N/A | N/A | P4 全启用 |

## 相位映射（按 source 反推，schema 迁移时回填）

| `source` | `phase` | `source_kind` | authority 基线 | permanence 基线 |
|---|---|---|---|---|
| `identity`(CONSCIOUSNESS) | experience | narrative | 0.5 | 0.3 |
| `journal`(DIARY) | experience | narrative | 0.5 | 0.3 |
| `notes`(SCRATCHPAD) | experience | scratchpad | 0.3 | 0.2 |
| `conversation` | experience | conversation | 0.5 | 0.3 |
| `digest_session` | experience | digest | 0.6 | 0.4 |
| `digest_segment`(新) | experience | digest | 0.6 | 0.4 |
| `digest_day`/`digest_week` | experience | digest | 0.7/0.8 | 0.5/0.6 |
| `rules` | **cognition** | rule | 1.0 | 0.95 |
| `lessons` | **cognition** | lesson | 0.8 | 0.85 |
| `self_knowledge` | **cognition** | self | 0.7 | 0.85 |
| `knowledge`(profile) | **cognition** | profile | 0.7 | 0.85 |
| `goals`/`plans`/`work`/`him`/`context` | experience | misc | 0.3 | 0.2 |

（这些值是**初版默认**，设计文档明确"参数跑起来再调"，留可配置常量）

## 认知状态机（P4）

```
nascent   -(默认稳定态)->     active
active    -(verification↑)->  reinforced
reinforced-(聚类抽象)->        higher
active    -(模型修订)->        revising -(成功)-> active
                                   \--(差异大)-> supersede(链换乘)
active    -(challenge_count↑)->  challenged
challenged-(未及时修订)->        archived
任何态   -(freshness<floor*permanence)-> archived
新认知   -(出现替代)-[旧]->      replaced (supersede_by=新; 旧 freshness=0)
```

不变性：
1. **永不硬删**：archived/replaced/challenged 全部保留行。
2. **链必修**：supersede 新增 `derived_from += [旧]`，旧标 `supersede_by = 新`。
3. cognition_state 为 NULL 表示是经历切片（非认知）。

## 关系

- 一个 Cognition Node → 0..N 切片通过 `entity_links JSON LIKE '%"name"%'` 关联。
- 一个 cognition slice → 0..N origin slices 通过 `derived_from JSON LIKE '%"id"%'`。
- 一个 chunk → 0..M edge 通过 `associations(chunk_a, chunk_b, weight, last_activated)` 共现增强。
