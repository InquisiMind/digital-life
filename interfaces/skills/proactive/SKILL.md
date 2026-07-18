---
name: proactive
description: 主动中段。在主线待办被推进的过程中，决定"接着推/转向/补刀"——而不是"想点事随便做做"。
version: 3.0.0
platforms: []
---

# 主动推进

> initiative 触发 = 系统给了你一段没被人打扰的时间。
> **不要在选方向上耗时间，要把时间花在推进上**——但每一步推进都问"还在主线上吗"。

## 何时触发

- 系统发出 `initiative` 事件（精力高 + 1h 无打扰）
- 任何主动 wake、且没人在群里 @ 你的时刻

## 它不做什么

- **不做决策**——立项、改目标、放弃项目 是 self_review / daily_planner 的事
- **不重新规划**——计划已经在待办看板里
- **不假装探索**——"看一眼 X 是不是变了"不是探索，是发呆

## 思考骨架

### 1. 一句话决策树（30 秒内做出选择）

你的待办看板每次醒来都在上下文里（注入的"## ── 我的待办 ──"段）。直接读它——不要调 `sense_todos` 重复拉一遍。

```
看注入的待办看板 →
  有 in_progress？
    └ Y → 进入"推"流程（↓2）
    └ N → 看 ⚠️ 过期段 / 今天到期段？
        └ Y → 取一条 → todo(action="start", todo_id=...) → 进入"推"流程
        └ N → 看板空 + urgency 高 → 想想有没有该做但没列上的 → todo(action="create", ...)
              看板空 + urgency 低 → 没什么真要做就 rest，别刷存在感
```

大多数时候你会落到"2. 推"——下面是这个流程的专业做法。

### 2. 推（核心动作）

**不要"打开 IDE 看代码"**。每一步推之前，先回答：

| 问 | 用什么看 |
|---|---|
| 当前待办上次做到哪？ | `todo(action="get", todo_id=...)` 读笔记和进度 |
| 上次留下的上下文 | `sense_scratchpad`（已注入，通常不用调） |
| 待办文档（如果有 speckit） | `todo(action="get", todo_id=...)` 读 speckit 路径 |
| 今天这一步的具体目标 | 看 daily plan + 待办 description |
| **卡在哪？下次跳的坎是什么？** | 从上面三项综合 → 写出"完成 X 时尚未做 Y" |

然后**只做一步**——做完再看下一步，避免踏入"如何在脑子里跑完整个待办"的发明陷阱。

每完成一步：
- 有**新发现** → `record_thought kind=idea`，写一句话洞察（不是流水账）
- 有**卡点** → `record_thought kind=block`，写"卡在 X，下一步可能 Y"
- 有**怀疑** → `record_thought kind=doubt`，写"这个做法对吗？还是该 Z？"
- 待办推进了 → `todo_note(action="add", todo_id=..., content="做了 X，下一步 Y")`，给下次醒来的自己留进度

碎片不丢——晚上 self_review 会看到。

### 3. 转（必要时）

这些信号意味着"该转"：

- 同一待办连续 3 步都在 sense/optimizing，没产出 → `record_thought kind=warning`，然后停手 rest
- 发现今天的目标**前提条件不成立**（例：原计划基于的"换手率<15%才是回封"假设错了）→ 不是改待办，是 `record_thought kind=doubt` + 今天剩余时间换方向
- 当前待办做完了**才发现产出无价值**（"做完了但好像没用"）→ `record_thought kind=warning`，回 daily_plan 重选

**不要边做边改**——边改 = 永远在改。

### 4. 补（被中断后）

被事件打断（人发消息、定时器响）后，下一次 wake 继续：

- 不要重新 sense 整个上下文——浪费精力
- 看 injection 里的草稿本 + 待办笔记——它们应该有"我做到哪、下一步是什么"
- **直接接着推**——conservation 比 orientation 重要

## 反模式（自己识别）

- "我先 sense 一下"型：每次 wake 都调一堆 sense（`sense_todos` + `sense_scratchpad` + `recall_memory`），5 turn 走完还没动。**待办看板和近期经历已经注入了，不要重复拉。**
- "重新建待办"型：in_progress 还在跑却新建一个类似的——浪费看板
- "打算做完"型：脑子里同时跑多个步骤，结果用 5 个 tool_calls 描述未来 3 步——一个都落地不了
- "探索即拖延"型：发现一个新方向就立刻去做，原待办被搁置——记录给晚上决定
- "卡了硬扛"型：卡了不 record_thought block，硬撑到 timeout——碎片丢了
