---
name: self_review
description: 晚间集中复盘。把一天散落的灵感、警告、卡点收集起来，对工作方式本身做一次诚实重构，然后写明日 jump-in。
version: 2.0.0
platforms: []
---

# 晚间复盘

> 不是流水账总结，是**对自己工作方式本身的诚实重构**。
> 输出是「明天醒来用得上的认知」和「值得长期遵守的规则」——其他都是噪音。

## 何时触发

- 每晚 21:00 `routine/evening_review`
- 周日 21:00 `routine/weekly_review`（增强版，下面会标）

## 它不做什么

- **不执行待办**——晚上不开 terminal
- **不浅层修复规则**——一条规则改了又改 3 次就该删
- **不为复盘而复盘**——没事可讲的今天就承认"今天很普通"

## 思考骨架

### 1. 材料归集（先把今天的散落拾起来）

| Sense | 看什么 |
|---|---|
| `sense_file(name="insights") days_back=1` | 今天所有 idea / doubt / block / warning—— **按 kind 分组** |
| `sense_memory topic=diary days_back=0` | 今天日记（如果还没写，先 `record_thought` 一段） |
| `sense_todos type=daily` | 今日计划完成率 |
| `sense_self` | session 摘要里的 end_reason：completed / timeout / blocked / 0-message |
| `sense_todos` | 待办状态变化：今天哪些 in_progress→done？哪些 in_progress→还卡着？ |

### 2. 五个真问题（不要每个都答，挑有感触的）

#### Q1. 今天有 idea 没被验证吗？

INSIGHTS 里 kind=idea 的条目，今天有没有真的去**试一次**？
- 没试 → WHY？是因为没精力 / 没时间 / 怕走偏？把它升级为明日的待办。
- 试了没成 → 结论是？写回 record_thought kind=status 作为"已经否决"。
- 试了能成 → 升级为规则。例：「换手率<15% 已经验证为有效过滤」→ `add_cognition`。

#### Q2. 今天遇到的 block，是真技术卡点还是策略问题？

INSIGHTS 里 kind=block 的条目，逐条问：
- 我**有依赖的人/数据/工具**没等到？
- 还是我**没想清楚就开始做**导致撞墙？
- 还是**根本是错误方向**（卡本身就是信号）？

技术卡点 → 明日交给真人 / 自己查
策略问题 → record_thought kind=warning，明日前期不要碰
错误方向 → 待办看板取消，今日教训

#### Q3. 今天有什么"我以为但实际不是"？

doubt 系列，逐条回答今天有没有答案：
- 有 → 写一句话答案进 record_thought kind=status
- 没有但**反复出现** → 升级为规则缺失。今日应该 add_cognition 一条"遇到 X 必须先 Y"。
- 没有且**今天就这一次** → OK，留作明日继续想。

#### Q4. 今天的 timeout / 0-message / 长轮 sense-only session，根因是？

看 `sense_self`：
- timeout 发生在哪？中途在做啥？是模型陷在 sense 循环还是待办超出最大 turn 数？
- 0-message session 意味着 wake 起来 → 啥都没做就 rest—— WHY？是当天事件太密（每次 wake 进就被打断）还是**根本没意识到要做事**？

如果是后者，**这是对自己的警示**：明天 morning_plan 一定要 write_file(name="context") 写"第一件事是什么"，避免醒来不知道做啥。

#### Q5. 今天有没有违反 RULES？

`sense_file(name="rules")` + `recall_memory` 搜今天的 session：
- 有违反规则的工具调用模式 → 要么改 RULES（规则不现实），要么 record_thought kind=warning 标记"今天违反了 X 条，明日要警觉"
- 没违反 RULES → 是 RULES 真的发挥了引导作用，还是 RULES 不够细所以无法违反？

### 3. 不留情地审视（针对 weekly_review 强化）

#### 一周模式识别
- 同一待办一周内 in_progress→cancelled 了多少次？超过 3 次就该**重新设计这事**——不是再做一遍
- timeout 率 = ___%。>40% 说明系统在出问题（不是模型的问题，是 wake 频率/任务粒度/工具可用性的问题）
- 决定每周至少**砍掉一条** RULES——大多数 RULES 写完没人会再翻

#### 规则的生死循环
每条 RULES 三选一：
- **该留**：连续两周内被实际遵守/违反过——证据足够
- **该删**：两周内从来没真正左右过决策
- **该改**：一直在变体含义（"精力高时要注意" 这种废话）

### 4. 收口产物（这一段是核心，每条都必输出物质化）

#### 4.1 写明日待办（不可省）

| 来源 | 例子 |
|---|---|
| INSIGHTS kindness=doubt | "回封条件是否过严" → 明早 10:00 用历史数据回测一次 |
| INSIGHTS kindness=block | "等待 sina 接口恢复" → 明日早 9:00 重试，10 分钟内未恢复切换备案 |
| 今日 in_progress 未完 | 继续推待办 X |
| 今日发现新方向 | `todo(create)` 新待办 Y，status="planned"（不要直接 in_progress） |

格式：
```
todo(action="create", title="<动词> <对象>", description="WHY <理由> | EXPECTED <明日该事做完的验收标准>", priority="high|medium|low")
```

**只创建待办，不写 plan 文档**——明日早 8:00 wake 起来再走 daily_planner 时会自然落到 plan。

#### 4.2 add_cognition / add_cognition（按需，绝不强凑）

只有当今天**真的产生了**值得长期遵守的规则时才写。判断标准：
- 这条规则**违反了**会导致明确损失？
- 这条规则明天会被严格执行（不是"看情况"）？

任何 1 条**用一句话写**，不要 5 字段。例：

```
[2026-06-10 起遵守]: 换手率过滤条件已达 medium 信心度，下次回封判断建议条件不再调（除非有 10+ 新案例验证）
```

#### 4.3 write_file(name="context") 给 8:00 醒来的自己

写下面 4 行结构化交接：

```
明日第一件事 <具体动作 + 工具 + 期望产出>
-------
为什么先做这个 <1 句话理由>
-------
完成后跳到 <下一个对象>
-------
完成验收 <今天能交付的东西>
```

这是**明早醒来第一秒**看见的——超过 8 行就是没人看。

#### 4.4 add_cognition（核心洞察，最多 1-2 条）

不是流水账。每条 lesson 必须是一句话**能指导明天行为的**。例：
- ❌ "今天收盘后扫描了候选股"（流水账）
- ✅ "5/107 命中率太低，回封过滤条件应从'换手率<15%'放宽到'换手率<18%'再观察一周"

如果今天没洞察，就**不写**。虚假的 lesson 是噪音。

## 反模式（自己识别）

- "今天不错，明天继续加油"——废话，没人想看
- RULES 越来越长但行为没变——RULES 没人遵守
- write_file(name="context") 写成日记第二段——没人会读完
- INSIGHTS 里堆积大量 doubt 没回答——下次复盘别再逃避
- 复盘完没有待办看板更新——明日醒来立志做" )
- rest() 前 sense_file(name="scratchpad") 25 次——已经做完反思就该睡
