# Memory Maintenance

本手册用于维护每个数字生命实例的记忆质量。架构机制见
[../architecture/memory-lifecycle-design.md](../architecture/memory-lifecycle-design.md)。

## 何时执行

- `weekly_review` 完成后进行完整维护。
- `initiative` 唤醒且 `check_memory_health` 报警时进行定向维护。
- `self_iteration` 聚焦记忆时，至少执行健康检查和结果记录。

## 标准流程

1. 运行 `check_memory_health`，确认 RULES、LESSONS、SCRATCHPAD 和
   CONSCIOUSNESS 的体积、重复和异常。
2. 运行 `dedup_lessons`，合并高相似经验；只有重复验证且违反会造成损失的经验才晋升为规则。
3. 用 `sense_rules` 检查重复、冲突和长期未触发规则。
4. 用 `sense_entity` 查看实体热力图，使用 `merge_entities` 合并重复实体。
5. 检查 `CONSCIOUSNESS.archive.md`、`DAILY.archive.md` 和 scratchpad 是否需要整理。
6. 记录发现、调整和剩余风险，不直接批量改写运行期记忆。

## 召回质量

检查 session 日志中的 `[实体触发记忆]` 和 `[快速联想]`：

- 有帮助的召回是否准确、及时。
- 噪声召回是否需要调整实体、别名或阈值。
- 预期出现但缺失的记忆是否尚未建立实体关联。

日志位于 `apps/{instance}/data/sessions/`。所有实例数据都是 mutable runtime
data；普通开发任务不得修改。

## 原则

- 证据先行，每次只处理少量明确问题。
- 不为追求整洁删除仍有维护价值的经验。
- 不把实例运行期数据提交到仓库。
- 架构或工具行为变化时，同步更新记忆生命周期设计文档。

## 统一记忆层（feature 002）运维

机制见 [`docs/design/unified-memory.md`](../design/unified-memory.md) 和
[`../architecture/current-system.md`](../architecture/current-system.md) §domain/memory。
本节只讲怎么"看、调、救"。

### 1. 看：chunks 表健康

```bash
# 进入实例目录
cd apps/<instance-id>/data/memories/

# 切片总数(应 ~1500 起步)
sqlite3 memory_vectors.db "SELECT COUNT(*) FROM chunks;"

# 按 phase 分布(应 experience ~10倍 cognition)
sqlite3 memory_vectors.db "SELECT phase, COUNT(*) FROM chunks GROUP BY phase;"

# 按 source 来源分布
sqlite3 memory_vectors.db "SELECT source, phase, COUNT(*) FROM chunks GROUP BY source ORDER BY 3 DESC;"

# 嵌入缺失率(应为 0 — embedding 列不该 NULL)
sqlite3 memory_vectors.db "SELECT COUNT(*) FROM chunks WHERE embedding IS NULL;"
```

### 2. 调：关键参数(view-only,代码里改)

文件 `domain/memory/memory/recall/unified/slice.py`:
- `_PERHOUR_LAMBDA = 0.005` — freshness 衰减系数。增加 → 经历更快被遗忘。
- `_FRESHNESS_FLOOR = 0.05` — 归档最小 freshness。
- `_ARCHIVE_FRESHNESS_RATIO = 0.1` — 归档阈值 = max(floor, ratio × permanence)。

文件 `domain/memory/memory/recall/unified/facade.py`:
- `_RRF_K = 60` — 倒数排名融合常数。
- `_BUDGET_MAX_CHARS` — 三种投递(resident/passive/on_demand)的字符预算。
- `_DEFAULT_TIMEOUT = 5.0` — 整体检索时间上限。

文件 `domain/memory/memory/recall/unified/cognition.py` 的 `_DEFAULT_DELTAS`:
- `access_bump_activation` / `verified_authority_inc` / `falsified_authority_dec` 等。

### 3. 救：降级诊断

**症状:召回看起来"什么都返回"但 model 仍说"想不起来"**
- 可能:embedding API 失效。
- 诊断:`grep "Embedding API failed" apps/<id>/logs/*` — 应有 warning 日志(§SC-003)。
- 自救:facade 会自动降级到 FTS5 词法路,不会完全空返(§SC-001)。

**症状:segment 摘要没出现在召回结果里**
- 诊断:`sqlite3 memory_vectors.db "SELECT COUNT(*) FROM chunks WHERE source='digest_segment';"` — 应该有行(每次 consolidate 后写入)。
- 自救:`python3 -c "from domain.memory.memory.summaries.consolidation_runtime import backfill_existing_sessions; backfill_existing_sessions(...)"` 回填。

**症状:FTS5 词法路不命中(专名/identifier 找不到)**
- 诊断:看 `tokenize_for_fts('emotion_interface')` 应输出 `'emotion OR interface'`。
- 自救:重建索引 — `python3 -c "from domain.memory.memory.recall.unified.fts import rebuild_fts_index; rebuild_fts_index()"`(幂等,可重复跑)。

**症状:认知被替换但 derived_from 链断了**
- 诊断:`SELECT derived_from, supersede_by FROM chunks WHERE id=<id>` — 双向应都有值。
- 自救:**不应该发生**(cognition_store 已用 UPDATE-first 保留 id);若确实断了,说明历史数据用 INSERT OR REPLACE 写过,需手工补 derived_from JSON。

### 4. 接 cognition API(模型驱动半边,待 hygiene skill 升级)

memory_hygiene skill 暂未直接调 cognition API;手工触发:

```python
from domain.memory.memory.recall.unified.cognition_store import (
    promote_one, supersede_one, revise_one, cluster_born_persist, apply_signal,
)
# 把经历切片 #123 提纯成认知
promote_one(123, summary="总结意见", entity_name="A+策略")
# 用新证据 #200 取代认知 #150
supersede_one(150, new_body="更准确的判断文本", new_authority=0.9, entity_name="...")
# 把多个近义认知聚成更高阶的元认知
cluster_born_persist([150, 199, 245], summary="综合后的元认知", entity_name="...")
```

`memory_hygiene` SKILL.md 后续会把这些写进 prompt,模型按需调用。

