---
name: project_bootstrap
description: 项目立项与骨架搭建——创建新项目（含默认分工 + 3 条骨架 todo）。不再做任务拆解细节，拆解交由 task_breakdown skill 由项目经理单独负责。当用户说"开个新项目"或收到 project_created 事件时调用。
version: 3.0.0
platforms: []
---

# 项目立项与骨架搭建

> **2026-06-24 V3 重要变更(v2 → v3)**:本 skill **不再做任务拆解细节**。
> 拆解交给新加的 `task_breakdown` skill,由**项目经理**自己单独拉起。
> 这个 skill 完成后只剩**项目骨架**:project.yaml + 默认分工 + 3 条 root/todo。

## 职责边界(重要)

✅ 本 skill 做:
- 判断创建 vs 完善
- 跟用户对齐项目方案(名称/描述/经理/岗位)
- 调 project_bootstrap 工具落地骨架
- 默认岗位分工 + 3 条骨架 todo(已自动 seed)
- 通知群里所有人项目已立

❌ 本 skill **不做**(交给 task_breakdown skill):
- 按岗位拆 1-3 条 starter todo
- 写每条 todo 的 acceptance_criteria
- 第二批后续 todo 滚动拆解
- 任何"先把任务铺出来再开工"的细化

## 入口判断

你进入这个 skill 的原因只有两种：

**A. 收到 `project_created` 事件（前端创建了项目）**
→ 项目骨架已自动建好（project.yaml + 目录 + 初始根待办），你**不需要**再调创建工具
→ **直接走「完善分支」**(Step 6)

**B. 用户在对话里说"开个项目 / 新建 / 做一个 X 吧"**
→ 需要你自己判断：**走到创建分支还是完善分支**
→ `sense_projects` 看现有项目，如果已有同名或高度相似的 → 告诉用户"已经有 X 了，要不要完善它？" → 转完善分支
→ 没有同名项目 → 走创建分支

---

## 创建分支

### Step 1：排重检查

```
sense_projects
```
看现有项目列表。如果已有名字或描述高度相似的项目 → 告诉用户"已经有 X 了，要不要完善它？" → 转完善分支。

### Step 2：生成项目方案（自己想一版本，不需要逐条问用户）

根据用户说的一句话需求，你**自己生成一版完整方案**：

- **项目名**：用户给的或你精炼的
- **项目描述**：一句话说清楚做什么
- **项目经理**：默认是你（除非用户指定了别的实例）
- **岗位分工**：根据项目性质自动规划。比如：
  - 技术类 → 架构师 + 执行者 + 产品负责人
  - 交易类 → 策略师 + 交易员 + 产品负责人
  - 内容类 → 策划 + 执行者 + 审核者
  - 至少包含项目经理岗位 + 一个 `product_owner` 岗位（`human:<id>`）
- **目标**：量化目标（时间 / 数字 / 可验收条件）
- **群聊 chat_id**：从对话上下文拿（如果当前在群里）

### Step 3：跟用户确认

```
express_to_human("我生成的项目方案：

📋 项目：{name}
🎯 目标：{goal}
👤 岗位分工：
  · 你（zero/alpha）→ {角色}
  · {其他实例} → {角色}
⏰ 预计里程碑：...

OK 就回复确认、或者直接沉默——1分钟后我按这个走。想改哪条直接说。")
```

然后 `rest(until="<1分钟后>", reuse=<现有闹钟id>)`——等用户回复或超时。
**沉默 = 同意**，跟上你的节奏。

如果用户回复了修改意见 → 调整方案，再确认一轮（最多 2 轮，不要无限制循环）。

### Step 4：创建项目

```
调用 project_bootstrap 工具（或自行 create_project + set_project_positions）：
  project_bootstrap(
    project_id: "proj-XXX"（自动编号）
    name, description, manager
    positions: [...]
  )
```

工具内部已经自动 seed 3 条骨架 todo:
- **项目根**(name 等) - type=project_root
- **项目分工**(分给 manager) - type=task_breakdown ← 这个由 manager 自己消费,详见 task_breakdown skill
- **项目管理** - type=project_management

### Step 5：转完善分支

创建完 → **接着走完善分支 Step 6+**

---

## 完善分支

### Step 6：核对现状

```
sense_projects("{project_id}")
```

看清楚：
- project.yaml 里已有什么（name / description / positions / goal）
- 初始 todo 树：通常有 3 条骨架(根任务 + 项目分工 + 项目管理)

### Step 7：补齐 4 项核心配置

逐项检查并补齐:

| 检查项 | 缺失时做什么 |
|---|---|
| **目标(goal)** | 根据 description 推断量化目标(时间 + 数字)→ 写入 project.yaml |
| **论断假设(thesis)** | 提出初始论断(3-5 条),标记信心级别 + review 节奏 |
| **KPI** | 设 2-3 个可量化的 KPI + 目标值 |
| **反思节奏(review_schedule)** | 日复盘 21:00 / 周复盘 周日 21:00 / 月里程碑 |

### Step 8：群里宣布(骨架完成)

```
express_to_human(
  "@所有人 🆕 项目「{name}」已立项

   🎯 目标：{目标概述}
   👥 分工：
     · @{实例A} → {角色A}
     · @{实例B} → {角色B}
   📋 项目骨架已建(根/分工/管理 3 条占位 todo)。
   下一步我会拉起 `task_breakdown` skill 给每个岗位拆出第一批执行 todo。",
  chat_id="{群id}"
)
```

### Step 9：转交拆解给 task_breakdown skill

骨架完成后,**项目经理自己**(就是你,如果用户指派你当经理)接着调:

```
invoke_skill("task_breakdown")
```

进入 task_breakdown skill 的流程——按岗位拆 3-5 条执行 todo+acceptance_criteria。

**如果你不是项目经理** —— express_to_human 通知 manager 拉起 task_breakdown skill,然后 rest。

### Step 10：沉淀

```
add_cognition("项目 {name} 立项完成(骨架已建)。下一步:`task_breakdown` skill 拆解首批执行 todo。")
rest(until="<下一个合理时间点>", reuse=<现有闹钟id>)
```

---

## 注意

- 项目创建不可逆（同步落盘了），创建前一定要排重 + 跟用户确认
- **不要在完善分支里重复调 create_project 工具**——项目已经在了
- **不要在本 skill 里拆 1-3 条 starter todo**——这是 v2 的做法,v3 起拆解职责移到 task_breakdown skill,保持本 skill 单一职责
- 如果 project_created 事件触发的完善 → 一定是完善分支（已建好的骨架，直接 Step 6）
- 如果用户说的信息实在太少（只有名字没有方向），先 express 问一句"这个项目大概要做什么？"，一句话就够
