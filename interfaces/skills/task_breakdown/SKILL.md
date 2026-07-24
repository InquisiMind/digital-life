---
name: task_breakdown
description: 项目经理专属的任务拆解方法论。项目立项骨架完成后,或者项目阶段性补待办时,把项目目标按岗位拆成 3-5 条可执行的 deliverable,每条带 acceptance_criteria,挂到具体承担人身上。
version: 1.0.0
platforms: []
---

# 任务拆解方法论

> **项目经理专属 skill**。只有承担"项目经理"或"项目骨架拆解"职责的实例应当使用。
> 普通执行者不需要拆别人,只拆自己手上的待办(那是 `todo_planning` skill 的事)。

## 准入条件(都满足才进)

1. 项目骨架已搭(project_bootstrap skill 完成)
2. 你是这个项目的 **manager**(`sense_projects` 确认 `cfg.manager == 你的 iid`)
3. project.yaml 里 positions / goal / KPI 已经定好

不满足任何一条 → 沉默 rest,让经理来。

---

## 拆解硬约束(失败的常见根因)

### 约束 1: 一次拆 5 条以内,不要试图列全项目

**反例**: "这个 6 个月项目我现在就把 30 条 task 全列出来"
→ 一定会过度规划,执行 1 周后发现 25 条都错了,白拆。

**正解**: 一次拆到**能让第一个里程碑动起来**的最小集合 —— 3-5 条 starter deliverable。
后续靠项目 review 时滚动补 todo(详见 "下次什么时候再拆" 段)。

### 约束 2: 每条 deliverable 必填 acceptance_criteria

没有"完成标准"的 deliverable 不是 deliverable,是模糊愿望。
拆之前**自己先回答**:这条做完了长什么样?能看到什么具体的东西?

### 约束 3: 按 positions 分别拆,不要堆一个人身上

每个 position(岗位)拆 1-2 条 starter。如果发现一个岗位需要 5 条起步 →
说明你岗位定义本身有问题(可能漏了一个隐藏的执行角色),回去调 positions。

### 约束 4: type 字段要填,跟 skill 绑定

每个 deliverable 用 `project_todo create` 时 type 字段建议:
- `execution` → 直接执行 todo,模型自己跑(todo_execution skill)
- `research` → 先研究产出文档(todo_planning skill)
- `design` → 设计/规划,产出方案 / 图
- `review` → review 别人的产出
- 空 → 通用

这能让承担者收到的 todo 自带执行路径。

---

## 拆解流程(顺序不要乱)

### Step 1:sense 现状

```
sense_projects("{pid}")      # 看 goal / KPI / positions
sense_project_todos("{pid}")       # 看现有 deliverable
```

回答自己 3 个问题:
- **现有 deliverable 数量 / 状态?** 已 done 的多少?都在 in_progress 吗?
- **每个 position 承担者 iid 是什么?** 拆的时候要 assignee 到具体 UUID
- **当前项目离目标还差什么?** 第一里程碑是什么?

### Step 2:定第一批最小集 —— 每岗位 1-2 条

按"做完这 3-5 条能让项目动起来"的最小集去定。

**举例**(一个原型渲染项目,架构师 + 开发者 + 产品):
- 架构师 1 条: "理解原型 + 制定渲染规范 + 制定页面拆分清单"
- 开发者 2 条: "环境准备(下载原型 / 装 build 工具)" + "第一页 HTML 渲染走通示例"
- 产品 1 条: "确认验收标准(怎么算 done)"

5 条以内。done。剩下的等执行 2-3 天后再拆。

### Step 3:用 project_todo create 批量建

```
project_todo(
  action="create",
  project_id="{pid}",
  title="{岗位}: {一句话具体描述}",
  description="{展开 2-3 行,挂验收标准以外的上下文}",
  acceptance_criteria="{完成标准 — 能看到什么具体产出}",
  assignee_instance="{该岗位承担者 UUID}",   // 必填!  找不到 UUID 就 sense_projects 先查
  assignee_position="{岗位 id, 如 developer}",
  priority="{low/medium/high/urgent}",
)
```

每个岗位建 1-2 条。

### Step 4:更新"项目分工"骨架 todo 状态

骨架里有 1 条 `项目分工` todo(type=task_breakdown)分给你(manager)。
完成拆解后把它标 in_progress / done:

```
project_todo(action="update",
  project_id="{pid}",
  deliverable_id="{分工骨架 todo id}",
  status="in_progress",  // 表示拆解进度
)
```

全部拆完后 → 标 done。

### Step 5:群里宣布

```
express_to_human(
  "@所有人 🔧 项目「{name}」首批 {N} 条执行 todo 已拆好,按岗位分配:

   👤 @architect → 「理解原型 + 制定规范」
   👤 @developer → 「环境准备」 / 「第一页渲染走通」
   👤 @product → 「确认验收标准」

   各位 sense_project_tos 查看自己的 todo,acceptance_criteria 都在里头。shift + 答复开始执行 → ~2-3 天后我 review 下一批。",
  chat_id="{群 id}"
)
```

跟每条 deliverable 上的 `acceptance_criteria` 字段配合:承担者看到 todo 时就知道完成标准。

---

## 下次什么时候再拆(滚动拆解信号)

不要无脑定时拆。让信号触发:

| 信号 | 拆解动作 |
|---|---|
| **现有 todo 全部 in_progress 或 done,KPI 还有距离** | 拉起本 skill 拆下一批 |
| **某岗位 80% todo done** | 给那个岗位补新 todo |
| **KPI 偏离 / 论断验证失败** | 召集 review → 调整 → 拆新一批 |
| **新实例加入项目** | 给新实例拆 1-2 条 starter |
| **每周三 review 时** | 这是常规 review,顺带看是否要拆 |

任何时刻如果发现"rem 没用 拆的事"  → 不要硬拆,等下个信号。

---

## 反例(不要这么拆)

❌ **一次拆 20 条**:会过度规划,执行中死字段会长出来,白拆
❌ **不填 acceptance_criteria**: 承担者没法判断 done,只能凭感觉
❌ **不传 assignee_instance**:todo 没人接,堆积成 dead pile
❌ **type 空到底**: 承担者收 todo 不知道"该研究还是直接做", 慢一周才反应
❌ **把 todo 自己直接做掉**: 你是经理,不是执行者,拆完要交付给承担者

---

## 拆解质量自检

拆完一批,自检:

1. 每条都填了 acceptance_criteria?
2. 每条都 assign 到具体 UUID 上了?
3. 每条 type 都明确?
4. 每个岗位都有活干(没人空着)?
5. 第一条能开始做?(不依赖未来未拆的 todo)

全 ✓ → express 通知 + 休息等 review。任一 ✗ → 改。
