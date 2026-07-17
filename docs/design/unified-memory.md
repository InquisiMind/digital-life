# 统一记忆体系设计

> 主设计文档 [第九章「记忆系统」](./digital-life-system-design.md#九记忆系统) 的扩展深化稿。
>
> 第九章确立了 **碎片 → 概念 → 召回** 的核心循环与晋升/淘汰。本文承接它，重点补三处：
>
> - **统一形状**：各模块按自己的写法沉淀（L0 单一真相不合并），最终物化成同一种切片。
> - **网状结构**：补第九章 9.7 缺环 —— 实体不再是扁平字典，而是三类连边的图。
> - **认知演化生命周期**：补第九章 9.4 只讲"单向晋升"的薄弱处 —— 认知形成之后如何持续变化。
>
> 状态：设计对齐阶段。具体实现（字段、阈值、分期）以源码为准，不在本文展开。
> 关联：[`memory-lifecycle-design.md`](../architecture/memory-lifecycle-design.md)（已 DEPRECATED，仅历史记录）。

---

## 0. 一句话

第九章回答"记忆怎么产生 / 消化 / 召回"。本文回答："当记忆被放进一张统一的、会自己生长的网时，**它该长成什么样、怎么持续变化**。" 记忆不是平铺的索引库，是一个**可成长的心智**。

---

## 1. 三条原则

1. **写时异构，读时同构**。各沉淀模块以自己方便的方式维护（markdown / JSON profile / 对话日志 / 待办结构…，各自单一真相），最终都物化成**同一种切片**。检索 / 演化只认切片。加一类记忆 = 写一个归一器，下游零修改。
2. **处理统一，拍平**。所有切片走**同一套**衰减 / 合并 / 覆盖 / 晋升管线；差异**只体现在切片携带的值**上（相位、权威、抗衰…），代码里不写 `if source_kind` 分叉。
3. **相位二元**。分层的本质不是主题，是**是否经过整理**：整理过的、主动沉淀的 = **认知**；未整理的 = **经历**。教训 / 规则 / 概念卡天然是认知；叙事 / 日记 / 洞察天然是经历。

> 相位即第九章的"概念 vs 碎片"，只是把轴从"记忆文件类型"抬升到一切切片的统一属性。

---

## 2. 统一切片（the atom）

无论来源，物化成同一种形状（字段是设计意图，非最终 schema）：

```
slice = {
  body,               # 整段内容（检索 / 展示）
  phase,              # experience | cognition
  source_kind,        # 经历类(叙事/日记/洞察) / 认知类(规则/教训/概念卡/自我认知)
  authority,          # 可信度基线: 规则≈1.0 > 教训≈0.8 > 概念卡≈0.7 > 叙事≈0.5
  permanence,         # 抗衰系数: 认知类高 / 经历类低
  entity_links[],     # 挂靠的认知节点(导航骨架)
  time_meta,          # created_at / session_id / segment_index
  derivation,         # derived_from[] —— 认知专属: 产权上游
  origin, provenance, # 可回溯
}
```

两条纠偏（相对第九章的隐含做法）：

- **概念 / 实体卡不用"名字"当关键词索引**。它是结构化认知，被**整段语义匹配**；名字只作注意力锚点（提权），不构成独立的关键词精确匹配路由。
- **原始对话不切片**（避免每句话一片的浪费）。会话被消化为 segment 叙事摘要时才切片；"刚说的话"靠上下文窗口覆盖，不靠检索。

---

## 3. 网状结构：三类连边

第九章 9.7 承认"实体是扁平字典，无层级关系"。本节把图立起来。

两种节点（经历切片 / 认知切片），连边有**三类生成法则**，每类对应一种回忆机制：

| 连边 | 生成依据 | 服务的回忆 |
|---|---|---|
| **时序连续性** | 同 session / 同日 / 同叙事相邻（`time_meta`） | "我后来怎么做 / 那之后发生啥" |
| **诞生链（产权）** | 认知的 `derived_from[]` | "这个结论我是怎么得出的" |
| **内容相似性** | 整段 embedding 近邻 | "类似的还有啥" |

> 共现联想（被一起召回 → 加弱权重）可作为第四类弱增强边叠加，但**图的骨架是上面三类确定性的边**，共现不打主力。

**核心洞察：认知既是节点，也是经历**——"形成认知"本身就是一次经历。所以认知复用经历的 `time_meta`、进时序链、可被未来回溯、可被聚类。这让网天然自洽，不为认知另造一套时间 / 产权机制。

---

## 4. 检索：三类连边融合

第九章 9.7 承认 wake 召回 / mid-session 联想 / `sense_entity` 三路径未抽象成单一 facade。本节统一它们。

原则：**匹配整段切片**（语义为主），不是匹配关键词。

```
query → 命中节点 → 沿三类连边各产候选 → 融合 → 一份去重排序
  ① 内容相似（整段语义 + 词法兜底）
  ② 时序邻居（time_meta）
  ③ 诞生链回溯（derivation）
  + attention / entity_links 命中 → 提权（非独立路由）
```

点火一个节点就沿三类边扩散：拉它的来历、它前后发生的事、跟它内容相似的——一簇浮现。`entity_index` 由此**退位为认知骨架 / 导航层**：查询期发现命中 → 把挂靠的切片拉进候选池并提权，不再作为独立产出结果表的召回路。这正是"实体 = 身份"语义的精确落实。三投递路径统一调这一份检索，只调度规则不同——9.7 缺环 #1 闭环。

> 多路并存的意义不在"凑更多结果"，在**某些路挂掉仍能召回**：语义主路径，词法在 API 不可用 / 检索专名时兜底，注意力提权重叠其上。一路失效 → 退路；全弱 → 工作 / 常驻层兜底。**检索永远不是关键路径阻断点。**

---

## 5. 投递三分

承接第九章，三种投递**共用同一个切片池和同一个检索 / 融合引擎**，只调度规则不同：

| 投递 | 触发 | 回答 | 预算 | 源池 |
|---|---|---|---|---|
| **常驻** | 系统每轮自动 | "我是谁、怎么做事" | 极小，精准优先 | 最核认知切片（规则 + 最近 N 条教训 + 人格） |
| **被动** | 上下文触发（认知节点出现 / 事件到达） | "刚提到的，我有相关记忆" | 中，相关优先 | 全切片池，融合 top |
| **按需** | 模型调工具 | "深挖某认知节点的全部细节" | 大，完整优先 | 以某节点为核心语义深挖 |

---

## 6. 认知演化生命周期（重心）

> 第九章 9.4 主要回答了"碎片怎么升成概念"——即**经历 → 认知**这条单向上升线。本文把它补完：**认知形成之后如何持续变化**。

### 6.1 为什么必须想清楚"认知的变化"

认知不是静态产物（晋升一次就结束）。它是一个**有状态、会老、会被推翻、会被更深理解替换、甚至会被重新发现**的活物。不演化的认知层会让生命体变成三种病态：

- **偏执狂** —— 被反复想起的认知越想越坚，回声室效应。
- **刻板者** —— 认知形成后不再变，与新经历脱节。
- **失忆者** —— 新认知直接覆盖旧的，丢失"我曾这么想"的成长轨迹。

所以把认知的状态机单独立成体系。

### 6.2 认知的状态机

```
   [nascent] ──晋升──► [active] ──累积验证──► [reinforced] ──聚类抽象──► [higher]
   (经历刚晋升)        (常态回收)              (强证据支撑)              (更高阶认知)
                          │
                    ┌─────┼──────────┐
                    ▼     ▼          ▼
              [revising] [challenged] [archived] ◄──长期无证据 + 低 authority──
              (模型重写)  (被反证降级)   (移出默认召回, 按需可见)
                    │         │
                    └► active(成功) / replaced(被新认知取代, 链换乘)
```

一个**不变性**和一条**必修链**：
- **永不硬删**。`archived / replaced / challenged` 都保留——降级不等于删除。认知必须可溯源——"我曾这么想过"本身是有价值的经历。
- **被取代必须带 `supersede_by` 指针**，新认知的 `derived_from += [旧]`。第九章 3.5 提到的"未来通过 `supersedes` 参数"的显式废止，本节是它的落地。

### 6.3 三条铁律（防偏执）

| 信号 | authority | verification | 说明 |
|---|---|---|---|
| 被召回（access，"想") | 不变 | 不变 | **想一次不强化任何东西**，只动短期 activation。这是堵回声室的根本闸 |
| 被采纳 / 复述（referenced） | 经历类 +X(微涨，作"应晋升"信号) · **认知类不变** | +0.5 | 被模型据以行动才算数 |
| 实践正反馈（verified） | 认知类 +Δ | +1、evidence_count +1 | 仅认知类接受结构性强化 |
| 实践反证（falsified） | 认知类 -Δ | -1、challenge_count +1 | 反证是认知退化的主因，时间不是 |

三条铁律：

1. **"想"不强化任何东西** —— access 只动 activation，只有"被采纳 / 被复述"才计 verification。
2. **强化专属认知类** —— 经历类的复述只微微涨、作晋升候选信号；形成认知（晋升）之后才接受 verified / falsified。
3. **强化是晋升的结果，不是晋升的原因** —— 晋升因为"经历反复触及同一节点 + 累积 referenced"；晋升后强化才可能。避免"想得多 → 升得快"的循环。

一个补充的**健康遗忘**：长期没有新证据支持、也没被矛盾触及的高权威认知，**不降 authority，降它在排序里的可见性**——不做事实遗忘，做注意力的淡出："长期不被印证的真信念，安静地变成背景。"

### 6.4 跃迁（阈值触发，不写代码分叉）

全部由参数越过阈值触发，函数不按 `source_kind` 分叉——这就是"拍平"的力度：

- **晋升**：经历反复触及同节点 + 累积 referenced → 模型提纯成认知；phase 跃迁，参数重设到认知基线，`derived_from ← [参与经历]`。
- **强化 / 反证降级**：`verification` 累积或 `challenge_count` 上升 → `active ⇄ reinforced / challenged`。
- **修订**：模型重写 body → 成功（version+1，旧版留 provenance）或新旧差异大 → 走 supersede。
- **取代**：`model_supersede` → 旧 `supersede_by ← 新`、`derived_from += [旧]`，认知节点换乘。
- **聚类诞生**：对已有认知做语义聚类 → 发现认知簇 → 模型形成更高阶新认知（泛化 / 抽象），`derived_from ← [参与认知]`。这是它"真正变聪明"的机制——从大量具体教训里抽象出行为哲学。
- **归档**：`freshness < 地板 × permanence`。认知类 permanence≈1，数学上近免疫——衰竭靠 challenge，不靠时间。

> **规则 / 教训的"只标记不覆盖"特性，靠 permanence 值实现，不靠代码分叉**——它们 permanence≈1，在覆盖公式里近乎不可触发。这是拍平最有力的体现。

### 6.5 双驱动

承接第九章"工程做确定性，模型做判断"的分工：

- **自动驱动器**（规则、零成本、秒级）—— 时衰、计数、共现加边、conflict 初判、归档阈值。写入 / 召回时实时 + 每日批跑。
- **认知驱动器**（模型、周期）—— 晋升、合并、修订、取代、诞生新认知。memory_hygiene / dream 周期触发；只产 `model_*` 信号，具体参数仍由统一规则算。

一句话：**规则负责确定性的脏活（时衰 / 归档 / 计数 / 取代 / 聚类候选生成）；晋升与诞生新认知负责生命的成长，一律模型驱动。**

---

## 附：与第九章的对应

| 第九章 | 本文 |
|---|---|
| 9.1 记忆是什么（六类文件） | §1 相位二元 + §2 切片原子的 `source_kind` |
| 9.2 双轴标签 | §2 `source_kind` + `entity_links`，双轴收进原子 |
| 9.3 碎片 → 概念 → 召回核心循环 | §4 检索（统一召回 + 三类连边） |
| 9.4 晋升与淘汰 | §6 认知演化生命周期（**重点扩展**：状态机 + 跃迁 + 取代/修订/反证 + 双驱动） |
| 9.5 自我认知 | 认知切片源，复用同一演化引擎 |
| 9.7 缺环：统一召回接口 | §4 统一 facade |
| 9.7 缺环：实体关系图谱 | §3 三类连边法则 |

---

## 附录：实现期落地决策（feature 002 实施后回填）

设计本身体现"参数先立机制、阈值跑起来再调"，以下为实现期落地时的具体决策（均可在源码中找到，作为机制常数）：

### A.1 检索：三路并发 + 两层时间预算

- **整体 facade 上限 5s**（`unified_recall(timeout_seconds=_DEFAULT_TIMEOUT)`），用 `ThreadPoolExecutor + concurrent.futures.wait(timeout=5)` 实现，超时返回已完成的部分结果——是 SC-001「检索永远非阻断点」的精确落地。
- **单条嵌入 HTTP timeout 8s**（`_embed_texts` 内 `urlopen(..., timeout=8)`）——比原 30s 大幅缩短，为整体 5s 上限让出余地；嵌入失败**不重试**（避免 429 风暴）。

### A.2 RRF 融合常数

- **k = 60**（标准倒数排名融合常数）。
- 同 chunk_id 合并、不同源各自 rank；文本指纹（md5 前 200 字）兜底用于 vector 路没回 id 的候选去重——取代了原来"30 字符前缀去重"的脆弱做法。

### A.3 FTS5 中文分词策略

- 用 **bigram（CJK 双字）+ Latin word boundary**，自实现 `tokenize_for_fts(text)`，写入和查询共用同一函数——不引入 jieba 等第三方依赖。
- FTS5 编译时探测（`PRAGMA compile_options LIKE '%ENABLE_FTS5%'`）→ 不可用时 facade 静默降级，走纯 vector + attention 兜底。

### A.4 参数衰减公式

- `freshness *= exp(-Δ_hours × (1 - permanence) × λ)`，**指数衰减**（不是线性——线性会让认知 30 天后 freshness→0、违反 §6.4「认知几乎不衰」的承诺）。
- 实测：permanence=0.95 时 30d 后 freshness=0.835；permanence=1.0 时 1 年后仍 0.11 不归档。
- 归档阈值：`max(0.05, 0.1 × permanence)`；认知类 permanence≈1，靠 challenge 不靠时间触发归档。

### A.5 supersede 持久化的 id 保留

- `cognition_store._persist_slice` 用 **UPDATE-first**（保留 chunk id 不变），不用 `INSERT OR REPLACE`（后者因 UNIQUE(source, chunk_hash) 会重新分配 id、断认知 derived_from/supersede_by 链）。
- 临床表现：`promote_one(经历#3)` 产出认知 #6928；`supersede_one(#6928)` 产出 #6929，#6929.derived_from 含 6928、#6928.supersede_by=6929 + state=replaced——双向链完整可溯源。

### A.6 模块地图

```
domain/memory/memory/recall/unified/
├── __init__.py          facade export (unified_recall, render_breadcrumbs)
├── facade.py            unified_recall 主体 — 三路 RRF + 预算 + 5s 硬上限
├── fts.py               FTS5 + 中文 bigram + BM25 + 触发器
├── slice.py             Slice dataclass + baseline 表 + 参数演化引擎
├── migration.py         历史回填 (backfill_slice_fields + backfill_entity_links)
├── normalizers.py       project / todo 归一器 + register_normalizer 接入
├── cognition.py         认知状态机 + 跃迁 + 三铁律(纯内存层)
├── cognition_store.py   hygiene-facing API + 持久层(promote_one / supersede_one / ...)
├── spread.py            联想扩散 — 时序邻居 + 诞生链 depth=2 BFS
├── scene_weights.py     场景意图过滤 — chat/deep_work/self_review/balanced
└── attention_cache.py   运行时短期注意力 cache (进程级, 不入 chunks 持久层)
```

---

## 附录 B：全体系审计发现与修复（2026-07-17）

> 本节记录 feature 002 完整落地后做的一次穿透审计的发现，以及对应修复。
> 设计原则与实现的偏差记录在此，方便未来读者理解"为什么 chunks 表的某些列填得不完整"。

### B.1 已修复（commit 链）

| # | 问题 | 修复 | commit |
|---|---|---|---|
| P0-1 | 4 处 conversation INSERT 只写 6 列 → session_id 100% 空 | 全部改 9 列 INSERT | 5629bbfc |
| P0-2 | backfill 只看 phase="" → rules authority 一直 0.5 | 改 WHERE 看 authority 偏差 >0.15 也重刷 + scheduler tick 启动时跑一次 | 5629bbfc |
| P1-3 | entity_links 填充率 1% → 导航骨架形同虚设 | conversation INSERT 写 entity_links + backfill_entity_links(import 实体名) | 00459cb1 |
| scene | precision 0.503 仍低 | scene_weights 4 场景按 query 切换 weight profile → 0.662 | 9b8f36dd |
| activation | chunks 表混了运行时/持久 | 搬 attention_cache 进程级 cache | 03dcf4b0 |
| audit | 大量垃圾(模板/过期)占 62% | memory_audit.py + apply archived 988 条 | 70ea0306 |
| B 治源 | 新对话继续产 wake_template | _is_noise_content 入口过滤 | 17cc7d33 |

### B.2 已知限制（不在本轮修的）

| # | 问题 | 影响 | 下一步 |
|---|---|---|---|
| P1-4 | digest_session cognition_state 混用 NULL/archived | 统计需防御 | 直接 SQL 改 archived→NULL |
| P2-5 | 历史 1576 行 session_id 未回填 | 旧数据无时序连边 | 回填脚本 from chunk_hash |
| C 监控 | heartbeat 没有 noise_n | 看不到当前垃圾率 | _unified_layer_stats 加 |
| eval 重设 | eval 集从 entity_index 自派生 → 清理后 answer 失效 → precision 假低 | 30 case 独立金标集 |
| 独立 golden eval | 缺素质 | 不依赖 entity_index | 30 条人工标定 |

### B.3 数据完整性校准值（实测 commit 00459cb1 后）

- `entity_links` 填充率 25.5% (真实命中 entity 名, 未命中的是 text 里不含已知实体)
- `authority` rules=1.0 / lessons=0.8 / knowledge=0.7 / digest_session=0.6 / conversation=0.5
  跟 data-model.md §相位映射完全对齐
- `session_id` 对 active 596 切片里 NEW conversation 已写(增量); 历史旧数据 P2-5
  做批量回填后恢复


