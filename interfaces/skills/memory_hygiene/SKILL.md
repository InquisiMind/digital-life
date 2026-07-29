---
name: memory_hygiene
description: 记忆整理方法论(dream)。清理意识流 + 合并重复认知 + 标记过时 + 认知网络关联补全 + 结构化冲突解决 + predicate 回填。用 add_cognition/update_cognition/find_* 工具维护认知库。
version: 6.0.0
platforms: []
---

# 记忆整理 (Dream)

> 你的认知库跟你的意识流一样需要定期整理。
> 让知识「能用」而非「堆着」。

## 何时触发

night_dream routine (23:20) 自动触发。

## 整理流程 (6 步)

### 1. 清意识流
意识流里累积了一天/一周的运行残余(status 段、expired 事件描述)。
- 把有价值的判断用 `add_cognition` 沉淀到认知库
- 清理 CONSCIOUSNESS.md 里的 status 段(保留 [整理] audit trail + 最近经历)
- 别动 [整理] 标记的历史段(那是 audit trail)

### 2. 结构化冲突检测 (V5 V6 新增, 高优先级)
**精确 key 桶**: 先调 `find_conflicting_keys()` 看看有哪些 `cog_key`(subject:predicate) 凑成桶.

> 同 `cog_key` 有多条 cognition → 一定是其中之一:
> · 真冲突 (12% SL vs 8% SL) → 看时间最新/最权威的, 用 `update_cognition(old_id, action="supersede", new_body="新表述", new_payload={value: 正确值})` 覆盖
> · 不同 scope 的特例 (-8% 一般规律 vs -5% 风险事件后) → 都留, 在新认知 payload 写明确 scope
> · 同一规则的口头重复 → 用 `update_cognition(action="supersede")` 把它们合并成一条更精炼的

**叙述类桶 (V3 #4)**: 调 `find_narrative_conflict_buckets()` 找共享 entity_links ≥2 的认知组.
> 这些组没有精确 key 但 entity_links 重合:
> · 用 `recall_cognition_by_key` 或 `recall_memory` 拉回原文逐对判断
> · 用 LLM 自身读懂语义: subsume(归一)/supersede(覆盖)/split(分拆)/keep(保留)四选一
> · 一晚的 dream 这类桶通常 0-3 个, 适合集中处理

### 3. 合并重复认知 (cos 路径, 兜底)
认知库里可能有多条说同一件事或高度重叠的认知 (没有 cog_key, 只有 cos 类似的)。

检查方法: 调 `recall_memory` 搜几个最近频繁出现的实体名/概念，看返回的认知列表里有没有重复的。

合并方式:
- 内容基本一样 → 用 `update_cognition(old_id, action="supersede", new_body="合并后的统一表述")` 取代老认知
- 内容有关联但不完全一样 → 用 `add_cognition` 写一条更高层的元认知, entity_links 关联它们

### 4. 标记过时认知
- 项目结束 → 相关规则/教训过期 → `update_cognition(id, action="obsolete")` (软标 archived, 保留溯源) 或 `update_cognition(id, action="delete")` (硬删, 无引用时)
- 策略调整 → 旧止损线/规则被新规则替代 → `update_cognition(old_id, action="supersede", new_body="新值", new_payload={value: 正确值})`
- 数值更新 → 实体信息变了 → `update_cognition(old_id, action="supersede", new_body="新值", new_payload={value: ...})`
- 认知库是唯一真相源。RULES.md / LESSONS.md 不再参与召回索引, 只留作人类参考。
- 强化/质疑不需要手动调 — 每次 wake 结束 session digest 阶段自动执行。

### 5. 认知网络关联补全
- 检查是否有孤立认知(entity_links 和 derived_from 都空的)
- 如果有, 回顾它应该关联哪些实体/概念, 用 update_cognition(action="supersede") 更新(继承旧 entity_links + 补全)
- 或者直接 update_cognition(action="delete") if 确实没价值

### 5.5 predicate / polarity 回填 (V6 新增, 高优先级)
很多老 cognition 写入时 model 没填 cog_key + polarity, 留下纯 text. 这导致:
- "Alpha 喜欢 review" 和 "Alpha 不喜欢 review" 在 embedding 里 cos≈0.93 无法区分
- dream 无法 find_conflicting_keys 检测冲突 (因为没 key)

操作方式: 对**最近 3 天创建但没 cog_key** 的认知, 逐条读 text 自判:
- 有明确的 subject + 判断 → 调 `update_cognition(old_id, action="supersede", new_body="[同一text]", new_payload={"key": "subject:predicate", "value": ..., "polarity": "positive|negative|neutral"})` 回填
- 例: text="Alpha 偏好慢节奏 review" → new_payload={"key": "Alpha:preference_review_pace", "polarity": "positive"}
- 若 100% 抽象、无 subject 判断的认知, 跳过 (不强行造 key)

批量查找没 cog_key 的认知:
```
recall_memory("最近") → 看哪些返回的认知没 cog_key 字段
```

只回填近 3 天的就好, 不动更老的历史认知 (保持稳态).

### 6. nascent 冗余清理
- 用 `recall_memory` 搜近 3 天的 nascent 认知, evidence_count=0 + 创建 >3 天 → 疑似垃圾
- 用 `update_cognition(id, action="delete")` 删除 (系统自动强化在 session digest 跑, 不需要 dream 手动调)

### 7. 草稿/笔记收口
- SCRATCHPAD 清理超 24h 的碎片
- INSIGHTS 合并同主题

## 工具使用

### 写入新认知
```
add_cognition(
  text="一句话判断/事实/规则",
  entity_links=["相关实体名", "关键词"],
  source_category="lesson" 或 "rule" 或 "insight" 或 "fact"
  payload={"key": "主体:谓词", "value": ..., "ttl_h": 24?}  # 可选V2结构化
)
```
写入后:
- 系统自动检测相似(cos > 0.92), 高度重复的提示 wrap 用 update_cognition(action="supersede")
- 如有 payload 含 key 且同 key 已有认知:
  · 同 value → 自动跳过(纯重复)
  · 不同 value → 标记 conflict_with 列表, 让你判断

### 覆盖旧认知
```
update_cognition(old_id=XXX, action="supersede", new_body=
  old_chunk_id=要覆盖的认知id,
  new_body="更新后的认知内容"
)
```
旧认知被标记 replaced, 新认知继承 entity_links + derived_from。

### 删除无用认知
```
update_cognition(chunk_id=要删除的认知id)
```
如果被其它认知引用, 系统会拒绝并建议用 supersede。

### 精确查询认知
```
# 按 (主, 谓) 精确召回该 key 的所有版本
recall_cognition_by_key(subject="金开新能", predicate="stop_loss_line")
```
这是 1:1 主键查询, 比 sense_entity / recall_memory 都准。

### 回顾已有认知 (兜底)
```
recall_memory("关键词/实体名")
```
全局搜(认知 + 经历都搜), 看本周积累的重复项。

## 不要做的事
- 不要删有 derived_from 引用的认知(用 supersede 替代)
- 不要无脑全删低 score 的(可能是新写入还没来得及被验证)
- 不要在 dream 里写大量新认知(dream 是整理不是生产)
- 强化/质疑不需要手动调 — 每次 wake 结束 session digest 阶段自动执行
- 不要无视 `conflict_with` 警告 — 真冲突必须 dream 解决

## 完成 audit trail
整理完后追加 `[整理]` tag 到意识流做 audit:
```
## [整理] YYYY-MM-DD HH:MM
- V2 冲突解决: N 个 key 桶处理 (X 合并, Y 保留, Z 标 scope)
- 合并 N 条 cos 重复认知
- 标记 M 条过时认知
- 补全 K 条关联
- 删除 J 条无用认知
```
