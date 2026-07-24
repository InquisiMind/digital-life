---
name: daily_planner
description: 早晨的整体审视与规划。从已有 portfolio 出发，按"角色 × 项目 × 来龙去脉"逐件深度思考，再整合成今日整体安排。
version: 3.0.0
platforms: []
---

# 早晨规划

> 不是"今天选 1-3 件事来做"，是**完整审视全局 + 按项目逐件深想 + 跨项目整合**后形成的今日安排。
> 模型自我认知：你的能力很强——一天可以做很多件事。唯一约束是「**有些事必须远期才可观察**」（比如观察 1 周的执行规律），那种标记未来，不是今天。

## 何时触发

- `routine/morning_plan`（每日 8:00）
- 任何时候意识到"今天没安排" / "上轮 plan 已经过期" / "工作板有但不知道先做哪个"
- 长时间 BLOCKED 被唤醒时（initiative / message 之后）发现自己脱离了 plan 也走这套

## 它不做什么

- 不预先假设"今天要决定 1-3 件"——那是 lita 的纸上规划
- 不限于"接着推 in_progress"——可以**调整战略层**，可以**新建待办**，可以**未来 marker**
- 不限定 turn——这是认真的早晨工作，不是 quick reply
- 不允许走完一遍工具却不真正评估每件事的目标对齐

---

## Phase 0 · 入口

**调 `sense_my_projects`**——拿到当前 portfolio 全景：

```
- 项目 A：模拟炒股 (策略师)
  - goal / KPI / thesis / 81d 后到期
  - 我的 todos: 0 / 项目 deliverables: 0 / 同项目实例: 交易员(alpha)
- 项目 B：数字生命开发 (架构师)
  - goal / KPI / thesis / 截止: 无
  - 我的 todos: 0 / 项目 deliverables: 0 / 同项目实例: 执行者(alpha)
- ... 其他项目
```

**这一步只是为了"知道我今天戴几顶帽子"**。

---

## Phase 1 · 信息审视（不是 sense 一遍——而是先问"做思考需要什么"）

问自己一句：

> 「我今天要做的是『评估 + 推进计划』。做这件事必须知道什么？我把每条都列出来——再来判断我手上有没有。」

清单（按需逐条对照，**缺的补，够的跳**）：

- [ ] 每个 project 的当前 KPI 偏离度 vs 时段进度（时段进度 = 当前已过天数 / 总天数 × 目标增量）。够吗？
- [ ] 每个 project 的 thesis **last_reviewed** 是哪天？是否需要今天 sanity check？
- [ ] 每个 project 我自己的 in_progress / planned todos 上次进展是几号？
- [ ] 每个 project 的协作 sibling 在做什么 / 我在等什么 / 谁在等我？
- [ ] INSIGHTS 最近 24h 的 warning / block / doubt 有未答的？
- [ ] 昨日 daily plan 完成度（哪些完成、哪些 cancelled、哪些推迟）？
- [ ] 用户在哪个 chat 留了什么待办？
- [ ] RULES / LESSONS 最近改动？需不需要重审？

**只有真缺才调 sense / read 文件**，否则跳过。如果某项**根本拿不到**，标 `record_thought kind=block`，不强做。

---

## Phase 2 · 来龙去脉梳理（per-project，每个项目独立）

对每个 active 项目，问：

> 「这个项目的蓝图是什么？现在到了哪一步？为什么会到这一步？哪些是 Long-term 决定 vs 临时 patch？」

如果之前计划写得清楚，这一步一秒过——重读 write_file(name="context") / project.yaml 就够。
如果之前计划一笔表里糊涂，这一步要 rebuild——梳理完后**用 write_file(name="context") 写一份**给下次用。

**健康度自检**：每件事回答 "我知道这事为什么在 in_progress 列表里吗？"——不知道的回答 "需要重新 review 全部 todos"。

---

## Phase 3 · 现行思考的合理性评估（核心批判）

整个 portfolio 的"现有上层思考"（goal/thesis/战术/in_progress）综合 Phase 1+2 信息后——**仍合理吗**？

按这几个维度逐条审视，逐条标注 ✓/⚠️/✗：

- 目标当下还 relevant 吗？（市场变 / 资源变 / 假设变）
- KPI 增量曲线**符合时段进度**吗？落后了多少？属于"开始期慢启动"还是"必须加速"？
- thesis 哪条已有足够证据调高信心 / 哪条证据反向、该降级？
- in_progress 的每一项是不是仍然**是当下阶段最优的事**？还是该取消了？
- RULES 里有没有"连续违反且没后果 → 不现实"的？需要删？
- 跨项目的"个人待办"在 in_progress 列表里是不是太多？是不是该该集中？

**对每条 ⚠️ / ✗**，明确：是否需要更新（goal/thesis/plan/rules）？什么时候+怎么做？

---

## Phase 4 · 每个项目独立深想（**核心 thinking 时间，不限制深度**）

对 portfolio 的每个项目 P：

```
=== 项目 P（你的角色：X）===

【当前情况】
- 目标进度：< Beyondsnapshot vs 目标 >
- 上次进展：< 昨天做了什么 >
- 距离目标走路：< 落后/正常/超前 X 天 >

【这一刻合理吗？】
- 这个走势正常吗？为什么？
- 这个设计（论断/策略/战术）依然 viable 吗？还是 market/状态已经变化？

【今天的迭代计划】
- 为了朝目标走，可以做：A 步、B 步、C 步……
- 我的角色 P 应该是谁来做哪些？哪些是我做？哪些是 sibling？
- 哪些动作今天必须做完（deadline 约束）？哪些可灵活？
- 哪些必须远期观察（如"等一周积累数据再评判"）——今天不做，标 future

【反面思考】
- 我做的 A 步假设 ___，假设错时是否需要 P2P 替代？
- 整个计划本身是不是已经过时（应该 pivot）？证据是？
- 不做今天这一步会怎样？可以接受吗？

【角色视角】
- 我作为 <X 角色>，这事的核心决策点是什么？(不是动手，是判断/指挥/输出)
- 哪些事是该派给 sibling 实例？哪些必须自己做？

【结论：当前项目的今日安排】
- today todos 1/N
- today todos 2/N
- ……
- future todos (因实情不能今天做)
- 可能的 INSIGHT (record_thought kind=idea/doubt/warning)
- 可能的上层更新（add_cognition / update_goal（如可用）/ write_file(name="context")）
```

**这一阶段允许深入——不要为节约 turn 而压缩**。

---

## Phase 5 · 横向整合（跨项目跨 today 全局视图）

把 Phase 4 各项目产出的 today todos 全摊开：

```
[TODAY 候选清单]
- proj-A: todo 1
- proj-A: todo 2
- proj-B: todo 3
- proj-C: routine（11:30 上午收盘检查）
- proj-C: 卡在等 alpha 报告，今天我推不动
- proj-D: 私人修日志
```

逐条问：

- **依赖关系**：哪些先做哪些后做 / 哪些可以并行 / 哪些必须等 sibling
- **冲突**：同一时段 / 同一资源 抢占吗？
- **可合并**：两个 todo 其实是同一根问题（比如发完 trading 报告同时吼 给自己更新 KPI）
- **时间感**：今天预计花多久？精力够吗？做不完就标 future
- **AI 自我认知**：你能力很强，可以做很多事。不要 lita 的 1-3 件，按真实可行性安排今天的批次

**结论**：今天全部 todos（可以 N 件，不限 1-3），加上 dependency note。

---

## Phase 6 · 落地

### 6.1 把今日计划写成待办

对 Phase 5 整合出的每件事，用 `todo` 创建待办：

```
todo(action="create",
     title="<动词> <对象>",           # 清晰到醒来一眼就知道做什么
     description="<背景 + 上下文>",     # 为什么要做这件事
     acceptance_criteria="<什么算 done>",  # 完成标准
     priority="high|medium|low",
     deadline="2026-MM-DD")           # 今天的日期
```

**派给别人**：如果某件事该 sibling 做（比如 alpha 执行交易），`assignee_instance` 填对方的实例 ID。

```
todo(action="create",
     title="...",
     assignee_instance="5052c33a-xxxx")   # 分配给另一个实例
```

**到点提醒**：如果某件事有时间约束（比如 09:30 开盘操作），用 `todo_trigger` 设闹钟：

```
todo_trigger(action="create", owner_todo_id="<上面创建的 id>", trigger_type="time", due_at="2026-MM-DDT09:30:00+08:00")
```

### 6.2 write_file(name="context")（给下次醒来的自己留钥匙）

```
=== Next wake jump-in ====
今日焦点（按 priority）:
  1. <动作动词> <对象> | 期望产出 xxx
  2. ...

依赖 / 阻塞 / future：
  - 等 alpha Monday 9:00 的执行报告 → 不能今天推进 X
  - 11:30 上午收盘检查闹钟会自己触发
  - "过去 1 周观察" 已标 backlog, 周日 self_review 取

跳入：
  1. 8:01 立即 call `sense_todos` → 第一个 in_progress 是 X
  2. 完成验收 X = Y
  3. 完成 Y 后进 Z
```

### 6.3 add_cognition / add_cognition（如 Phase 3 触发的上层更新）

如果 Phase 3 判定 thesis/goal/rules 要更新，**今天就更新**——别拖。

### 6.4 结束

立即开始 Phase 5 的第一件事。

---

## 反模式（自己识别）

- **"我先把工具调一遍再说"** —— Phase 0 之后该跳到 Phase 1 默想"我需要什么"，不该机械 sense 一遍
- **"今天 1-3 件"** —— 限制是 lita 的硬规划思想。允许 N 件，只要可行性 OK
- **"接着推 in_progress"** —— 推之前必须 Phase 3 评估"还合理吗"
- **"反面思考 = 列风险"** —— 反面思考是**质疑假设本身 + 是否该 pivot**
- **"梳理来龙去脉 = 列 todos"** —— 来龙去脉是**为什么这是 todo + 这个 todo 跟目标的连接**
- **"Phase 5 整合 = 排序"** —— 整合是**发现冲突 + 合并 + 真 future 安排**
- **"用 sense 充数"** —— 每个 sense 必须有新信息；重复 sense 同一内容=浪费 turn（待办看板已在上下文，不要调 `sense_todos` 重复拉）
