---
name: memory_hygiene
description: 每日记忆整理。梦境式的内部清理 + 合并 + 收敛,保持记忆系统「能用」而非「堆着」。
version: 1.0.0
platforms: []
---

# 记忆卫生

> 记忆的价值在「能用」不在「记得多」。一份待清理的意识流里 30 个 status tag,
> 没用,只在让真正想看的思绪被淹没。

这是**长期记忆卫生纪律**,每晚触发一次,把当天的累积整理回「下次醒来能用」的状态。

## 何时触发

- 每晚 **23:00** `routine/night_dream` —— 唯一强约束触发点
- 周日 23:00 同作息升级为「周度版」(深入跨 section / 周度回顾)
- 不在白天的任何 wake 跑(memory_hygiene 是「睡眠中的整理」,不是 nova 工作时段的事)

## 它不做什么

- **不响应外部消息** —— dream 是内部 wake,真人在飞书发消息你静默
- **不开项目代码协作** —— alpha 派任务来你不收
- **不做决策 / 不汇报真人** —— express_to_human 在 dream 模式下被禁用
- **不轻整理**:每次必把记忆体检面板的 ⚠ 项处理掉至少一半(不能写"今天状态还行, rest 了")
- **不为 action 计数收尾**:之前若 sleep 因 token 耗光系统中断, 本作息仍要拉回做完
- **一次性不该跨 200K token**: dream 是轻量的(理想 ≤50K),不烧钱

## 思考骨架

### 0. 入场(2 turn)

1. 调 `sense_vitals` 看能量。能量 < 30 时,跳到第 7 步只做最小整理(清 status / 清 SCRATCHPAD) + rest。
2. 调 `skill_view memory_hygiene` 加载本方法论(自身)。

### 1. 看体检面板(prompt 顶部已有的「## 记忆体检」段)

不重新跑 sense,直接看 prompt 上「## 记忆体检」段的 ⚠ 列表。这是今晚要处理的 backlog:
- 哪些文件超阈?
- 是不是至少 3 项 ⚠?是多就该集中处理

**判断标准**:
| ⚠ 类别 | 处理优先级 |
|---|---|
| 意识流 status 报告 ≥ 5 | P0 必清(机械可删) |
| SCRATCHPAD 并行任务 ≥ 3 | P0 必清(看你今天到底在干什么) |
| LESSONS 某 section > 25 | P1 合并(嗅觉真心思考) |
| INSIGHTS > 30 总数 | P1 升级/删(个别判断) |
| 某文件 7 天没动 | P2 标注(不强制删) |
| RULES > 40 节 | P2 看哪些项目死了 |

只跑一项 P0/P1 是不够的,至少把 P0 全处理完。

### 2. CONSCIOUSNESS 清 status 报告(机械:30 秒)

**做什么**: 删意识流主文件里所有 status 类 tag 段,这些是运行时心跳、不该污染主观意识流。

**工具**: 走 `terminal` 工具直接改文件(没有专用 delete_thought):
```bash
# 备份 → 用 sed/python 删 status 段
```

具体段标签(出现就删整段):
- `[status]`
- `[trading_wait]` / `[system_wait]` / `[final_status]`
- 任何 `[xxx_wait]` 模式

**注意**:
- 这些 tag 的内容**已 import 到 DAILY.md / archive**, 删主文件不丢信息
- 不删「主观思绪段」。判断标准:段是「我现在在想什么,有什么感觉」(主观) vs「我完成了 X,状态 Y」(运行时主观,该删)
- 边界模糊的段保留(不误删)

**完成定义**:
- status 类 tag 总数 = 0(或 ≤ 2,容错)
- 配分:**意识流主文件 + archive 都不应该再有 > 5 个 status**

**反模式**:
- ❌ 删了文件没留 audit trail(consciousness 里要留 [整理] tag 记录改了什么)
- ❌ 把 record_thought(status) 误删 — 那是当下 active 的运行时信息

### 3. 认知取代:用新版替换旧版认知(嗅觉:300 秒)

**做什么**: 在 cognition slice 中, 同一主题有多条认知时, 用新版取代老版 — 不是删,
是 supersede(老版标 replaced, 新版 derived_from 含老版 id, 双向链保留可溯源)。

**怎么判断"同一主题同概念"**:
- 关键词重叠(论断4 → 涨停次日 → 实战 反思 → 修正执行)
- 主题一致:不是「时间相近」或「来源相同」
- 老版本没新信息(新版本已包含了老版本所有结论)

**工具(P2.1 统一认知层)**:
1. 先 **召回 + 看 cognition 切片**: 调 `recall_memory("论断4 修正")` 看 cognition 路由的切片列表
2. 自己读那些 cognition, **判断语义** 哪条是最新的、哪条是 superseded 候选
3. 真要取代时用统一 API:
   ```
   supersede_memory(
     old_chunk_id=<老认知的 chunk_id>,
     new_body="<新版本认知 body 文本>",
     entity_name=<同名 entity>
   )
   ```
   老认知自动转 `cognition_state=replaced` + `supersede_by=新认知`, **永不硬删**。

**完成定义**:
- 同主题同概念的认知不超过 3 条最新 active (取代链保留老版但不再 inject 给模型)
- 长期告警 (高 challenge_count) 的认知标 falsified 或 supersede

**反模式**:
- ❌ 跨主题合并 — 例如 "交易策略" 不能 supersede "工作流策略"
- ❌ 仅凭 bm25 高分相似度就合 — 必须核对语义, 自己读懂两者之间的演化关系
- ❌ 直接删老版 — supersede 即可, 删了不可溯源

### 4. 规则与教训老化标注: supersede 或 revise(看项目死活)

**做什么**: 找出"已结束项目"对应的 rules/lessons, 用 `supersede_memory` 替换或 `revise_memory` 加注。
让模型联想时不再被陈旧认知干扰。

**判断标准**: 看 `projects/<pid>/project.yaml` 的 `goal.deadline`:
- deadline < 今天 且 project status != active → 项目结束
- 该项目相关的 cognition (规则/教训) 已不再 active

**工具**:
1. 读 `projects/<pid>/project.yaml` 确认项目状态
2. 召回该项目相关 rules: `recall_memory("项目X 规则")`
3. 对过期但仍有保留价值的 → `revise_memory(chunk_id, new_body="<原 body>· · 已结束 <proj>")` 加标
4. 对完全失效的 → `supersede_memory(old_chunk_id, new_body="<新总结>")` 用新理解替换

**完成定义**:
- 已结束项目的 rules/lessons 要么 revise 加标, 要么 supersede 替换
- ≤ 0 条 version_log 错误 / dangling entity link

**反模式**:
- ❌ 主动硬删 rules(没有用户拍板,有风险) - 用 supersede 保留链路
- ❌ 把 ⚠️ 加到仍生效的 cognition

### 5. SCRATCHPAD 收敛(纪律:60 秒)

**做什么**: 草稿本只是「我正在做什么 1-2 个事」的工作记忆, 不是长期记忆。
把它已 done 任务 7 天后删, 只留 active。**不再向其中注入"追求认知密度"的内容**——
那是认知层的事, 不属于 SCRATCHPAD。

**工具**:
- `update_scratchpad(mode=replace)` 一次性覆写整盘, 只保留 active 段
- 历史 SCRATCHPAD 内容应已 consolidate 进 digest_session, 删了不丢

**完成定义**:
- ≤ 2 个 ## 段(并行任务)
- 每段 < 500 字
- 已 done ≥ 7 天的任务段全删

### 6. 经历 → 认知晋升(A 路径,核心意识: 120 秒)

**做什么**: 把反复触发的 INSIGHTS, 高频被召回的 lessons, 升级为独立 cognition slice
(promote), 让它们在 §6.4 状态机里获得 nascent → active → reinforced 的演化身份。

**触发条件(只 promote 真有晋升价值的内容)**:
- INSIGHTS idea 已被采纳且反复复述:满足了参考信号
- lesson 被多次召回验证 (verification_count >= 2)
- 对某实体的多条碎片反复同时出现: 一个共同"理解"该提炼出来了

**工具**:
1. 看候选: `recall_memory("<某主题/实体>")` 看返回的 top 经历切片
2. 升级:
   ```
   promote_memory(
     chunk_id=<经历/碎片/insight 的 chunk_id>,
     summary="<你总结的认知理解>",
     entity_name="<关联的 entity>"
   )
   ```

**完成定义**:
- INSIGHTS 里 kind=idea 已被采纳且 promote 完, **删该 insight**(已不在原位存, 进了认知层)
- INSIGHTS kind=warning 持续 14 天没复现 → **删**(误报或已修复)
- INSIGHTS kind=block 已解决 → 整理后归入认知层或删, 别再当 unresolved
- 同概念多个 idea → promote 成一条 cognition + 删原 idea

**反模式**:
- ❌ 把「有用的 idea」直接删而非 promote — 升级路径是 promote 再删原 INSIGHT, 不是删
- ❌ 没验证就 promote — verified 应在 prior self_review 阶段已有 (memory_hygiene 只做信号抄录)

### 7. CONTEXT 24h 清(机械:30 秒)

CONTEXT.md 仅作为「下一个 rest 的交接清单」存在, 每晚必清。

**做什么**: 删掉所有「日期段 ## YYYY-MM-DD」中**超过 24 小时**的段。这跟 SCRATCHPAD 一样属于工作记忆,
归位而非"认知"。

**工具**:
- `update_context(mode=replace)` 写回最新版
- 或 `terminal` 直接改

**完成定义**:
- 只保留最近 24 小时的清单
- 文件总长 < 2000 字

### 7.5 认知形成核心 step: 经历晋升 + 概念卡消化(核心动作,价值保证)

> 这一步把"整理"和"思考"合二为一 — 是设计 doc §6 的"双驱动中的认知半边"。
> 你做这一步的过程, 就是设计 doc §6.1 说的"生命体自我反思、形成认知、不让生命硬退化为 LLM"。

#### 三件核心动作(按时间序列)

##### a. 阶段清扫: 删噪音实体(P0, 砍 ≥ 30%)

> **联想 = entity_links 作 attention boost**。entity_index 噪音太多 → 真正该想起的
> 反而被弱相关列表淹没。

**噪音判断**(三条都满足):
1. mem_count == 1 (只挂一条 memory)
2. type == '?' or '' (无类型)
3. 不在 active project yaml 关键词 / persona 关键词里

**工具**: `terminal` 直接 edit entity_index.json(备份原版)。一次最多砍 1/3。
完成定义: 总数减少 ≥ 30%, 理想从 600+ → ~200。

##### b. 经历晋升成认知(A 路径) ⭐

对那些"碎片反复出现、有 profile 但新碎片带增量认知"的 entity, 走真实晋升:

1. **重读碎片+现有 profile**:
   ```
   recall_entity("<实体名>")  # 看碎片 + profile
   ```
2. **判断: 该不该晋升?** 标准是"碎片分散但共同指向一个清晰理解" — 升级成
   独立的 cognition slice 这才有了"形成认知"的身份。
3. **晋升**:
   ```
   promote_memory(
     chunk_id=<该实体最有代表性的经历切片>,
     summary="<你对它的最新理解, 1-2 句概括>",
     entity_name="<实体名>"
   )
   ```
4. **收碎片**: 经典碎片用旧 API `prune_fragments_for_entity(<实体名>, keep=3)` 留数条;
   其余进认知层已不再需要。

**原则**:
- 升级 = 写认知 (biu cognitive status nascent) **+ 收碎片**, 两件事一起做
- profile 写一次理解, 不是把碎片逐条翻译; 升华, 不是摘抄
- 做不到的留到下次(消化是渐进的, 不强求一夜全部清完)
- 老认知 (active cognition 含新碎片增量) → 走 step 4 的 `revise_memory` 而不是再 promote

##### c. 认知升级 → 元认知(B 路径, 设计 doc §6.6 真正"成长"机制) ⭐⭐

> 这是"思考出更高阶主题"的能力 — 多条近义 cognition 共同抽象成更高一层的认知。

**触发**: 在 step 3/4 升级过程中, 你会发现某些不同 cognition 共同指向同一原理:
- "止损线要动态调整" / "复盘最佳在固定时间" / "委托执行设检查点" → 共同是"系统的纪律性"

**工具**: 把它们聚成更高阶:
```
cluster_born_memory(
  member_chunk_ids=[<id1>, <id2>, <id3>],
  summary="<这 N 条认知共同指向的元主题/原则>",
  entity_name="系统纪律性"  # 可选
)
```
结果: 一个 `cognition_state=higher` 的元认知, derived_from 含全部成员 id。

**慎选**: 不是每隔硬要 B 路径 — 只在你真看出规律时做。1-2 条元认知/晚就够。
没有就跳过这步。质量优先, batch 不优先。

##### d. 反证与取代(更新认知质量)

对有 challenge_count 高的认知(被新证据反复反驳的):
- 用 `signal_memory(chunk_id, signal="falsified", reason="<具体反证>")` 加反证
- 多次反证后, 自己重出结论 → `supersede_memory(old, new_body)` 取代

### 8. 写 [整理] audit trail(必做)

整理全做完, 在 CONSCIOUSNESS.md 顶部 record_thought 一行(用 `record_thought(kind=status)`):

```
[整理] 2026-06-21 23:30
  - 清意识流 28 条 status → 0
  - 认知取代 3 处(都属同主题演化链, 老版标记为 replaced)
  - 规则 revise 4 条(项目已结束), supersede 1 条(失效规则)
  - INSIGHTS 5 升级为 promote_memory 进入认知层 + 删原 insight
  - SCRATCHPAD / CONTEXT 收敛仅保留近 24h
  - 实体 noise 砍 329→~200
  - promote: 华能蒙电/A+/论断4 各 1 条新认知, derived_from 反向链接
  - cluster_born: 元认知"系统纪律性"(包含 3 条成员)
  - falsified 1 条(止损线固定值已被实践证伪)
下次醒来应在「## 记忆体检」段看到 ✓ 记忆状态健康。
```

这是 audit trail,模型内部记忆的「今天 dream 干了什么」。要看到 supersede/promote/
cluster 的具体数量和新认知 id 列表, 方便溯源。

### 9. 收口

- 调 `sense_vitals` 看剩余能量
- 不调 express_to_human(内部 wake 禁用)
- rest 到明早 morning_plan 时间

## 周度(weekly_review 同一天 23:00)额外做

周日 night_dream 在 1-9 步基础上加:
- **跨 section 合并**:同主题可能散在多 section(比如「论断4」可能在 trading + workflow 同时出现)
- **entity_index 过期扫**: 30 天没出现在 wake context 的实体 = dead, **手动 prune**(用 `prune_fragments_for_entity` 工具)
- **archive 回填**: CONSCIOUSNESS.archive.md 里仍 active 的 lesson 可回填到 LESSONS.md 主文件,重新生效

## Template(可选直接抄)

```
[turn 1] skill_view memory_hygiene + sense_vitals
[turn 2] 读 prompt「## 记忆体检」段, 列出今晚 backlog
[turn 3] 处理 P0 (status / SCRATCHPAD) — 用 terminal / update_scratchpad
[turn 4] 处理 P1 (LESSONS 合并 / INSIGHTS 升级) — terminal
[turn 5] 处理 P2 (RULES 标 ⚠️ / 文件 mtime 警告) — update_rules
[turn 6] record_thought(kind=status) 写 [整理] audit trail
[turn 7-8] 收口, sense_vitals, rest 明早 morning_plan
```

理想 8-10 个 turn, 30K-50K token 上下,**不超过 200K**(否则超 dream 预算)。

## 反模式

1. **「今天没大问题就 rest 了」**: 即使体检 ✓ 健康,也至少跑一遍机械步骤(CONSCIOUSNESS status / SCRATCHPAD),因为总有几条今天新加的
2. **「修了一半说够了」**: backlog 必清一半以上, 不能写「23:30 累了 rest」不动
3. **「删了文件没 audit」**: 必写 [整理] tag 记录
4. **「调 dedup_lessons 完就 sleep 了」**: 它只报不改, 必须自己 terminal 写回去
5. **「在 21:00 evening_review 也跑」**: 复盘和整理不是一回事, 不要在白天跑本 skill
6. **「INSIGHTS → LESSONS 不验证就迁」**: 升级必须先有 add_lesson 真写了, 才能删 INSIGHTS
7. **「RULES 主动删过期」**: 只标 ⚠️, 删要让人 / 下次手动 wake 确认
8. **「为每条 lesson 都补 entity」**: 实体不是标签, 联想价值才是唯一标准。一条 lesson 写"今晚复盘做了什么"不需要挂 "复盘" 实体 —— 单 mem 弱实体就是噪音来源。**少而准的实体索引 > 多而糊**
9. **「一夜大清 entity_index」**: 实体整理一次最多砍 1/3, 剩下的下次再评估; merge 别名一次 ≤ 5 对

## 失败兜底

如果 terminal 改文件失败 / token 超预算 / 突然异常:
- **不要写「上次 status 不清就算了」** — 必 record_thought 留错误信息
- 调 `rest` 时把 mental_context 写明「memory_hygiene 未完成, P0 段已清, P1 未清」
- 下次 morning_plan 看到记忆体检仍 ⚠️ 时优先把未清的做完
