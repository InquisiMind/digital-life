---
name: entity_curation
description: 实体记忆治理方法论。每周回顾时使用——对照碎片记忆提炼成"概念记忆"，让每个真有意义的实体都有结构化档案。
version: 1.0.0
platforms: []
---

# 实体记忆治理方法论

你承担每周回忆整理时使用这个方法论。**碎片记忆没有价值，概念记忆才是真知识**——一条事实档案远胜 100 条意识残留。

## 一、原则：碎片 → 概念

```
碎片 (fragments)              →    概念 (profile)
  · consciousness 残留                · 一段 summary
  · lesson 类字句                     · N 条 facts
  · 重复提及                          · 项目/股票/人专属元数据
                                      ↓
                                联想时直接读 profile，不读碎片
```

碎片让你**看不到森林**——把 100 条意识残留浓缩成 1 条档案，模型每次联想时直接读到的是这个实体的"思想钢印"，而不是流水账。

## 二、何时跑

**每周策略 review 末尾**（周日 21:00）跑这套。也可在 self_review 时主动跑一次。

## 三、流程：5 步治理

### 步骤 1：调 `index_health_check` 看现状

工具返回：
- `total_entities`：索引里实体总数
- `missing_profile_high_value`：**碎片 ≥5 但没 profile** 的实体——这是**高优先级**，碎片够多说明常被想起但没被压缩成概念
- `suggested_merges`：检测到同一段记忆被多个实体共用——很可能是别名，需 `merge_entities`
- `missing_type`：type=None 的实体（应该有 type 但没标）

### 步骤 2：批量合并别名

对每个 `suggested_merges`：
- 判断"primary"和"alias"哪个是常用名（比如"华能蒙电"是常用名，"600863" 是代号）
- 调 `merge_entities("华能蒙电", "600863")` 合并

### 步骤 3：拣选值得 profile 的高价值实体

哪些实体值得有 profile？

| 类别 | 例子 | 该建 profile |
|---|---|---|
| **人** | zhp / alpha / 苏迪 | ✅ 必建（角色、协作风格、最近互动）|
| **项目** | 模拟炒股 / 数字生命开发 | ✅ 必建（目标 / KPI / 论断状态 / 最新进度）|
| **持仓股** | 华能蒙电 / 红星发展 | ✅ 必建（买入价、止损线、操作历史、当前状态、可参考形态）|
| **方法论** | 龙头断板后回封、ETF 趋势 | ✅ 必建（适用条件、淘汰信号、回测数据）|
| **关键决策** | "5/27 卖华能蒙电" / "确认策略师/交易员分工" | ✅ 必建（决策依据 + 当时回看的角度）|
| **系统组件** | express_to_human、task_todo_due | ⚠️ 应该入 `add_cognition` 不入 entity_profile |
| **流程事件** | afternoon_checkin、routine | ❌ 不建档，模式化的事情 |

### 步骤 4：为每个高价值实体写 profile

调 `set_entity_profile(name, kind=..., aliases=[...], summary="...", facts=[...], extra={...})`。

**summary**：1-2 句"这个实体对**你**意味着什么"

```
summary: "2026 年 5-6 月做过的电力股龙头。回封策略 +19% 验证成功，已清仓。"
facts:
  - 买入 5/27 @6.28，1200 股
  - 止损线 -12% (5.53 元)，从未触发
  - 卖出 6/2 @7.45，盈利 +19.3%
  - 6/2 至今未再进场；若再次回封形态重现可考虑
extra:
  industry: 电力
  last_position_status: 已清仓
```

### 步骤 5：清理碎片（profile 写完后）

对**每个写了 profile 的实体**调 `(已废弃)(name, keep=3)` — profile 已经吸收概念，**保留最近 3-5 条**做案底足够，其他删掉。

## 四、写 profile 的标准

- **summary 必须对你有用**——一句话让你（下次联想到这个实体时）立刻知道"这是我做过的某事，关键结论是什么"
- **facts 必须是事实**——不带评论，只是发生的事 + 关键数字
- **extra 是结构化字段**——按 entity 类型灵活使用（person 的 role / stock 的 industry / project 的 progress_pct）

## 五、什么时候更新 profile

不止在周 review：

- **持仓股交易发生时** → 立刻 update profile 反映新状态
- **关键论断调整时** → 立刻 update
- **认识一个新人/新实体** → 第一次互动就建 profile

不要让 profile **陈旧地存在**。如果旧 profile 已过时（持仓已清、决策已推翻），编辑或重写，不要保留过期信息。

## 六、工具速查

| 工具 | 何时用 |
|---|---|
| `(已废弃)` | 周度自查，看 missing_profile / suggested_merges |
| `sense_entity` (entity) | 看某实体全部碎片（精炼前看素材）|
| `set_entity_profile` | 写/覆盖某实体的 profile |
| `merge_entities` | 合并别名 |
| `(已废弃)` | profile 写完后清理碎片 |
| `add_cognition` | 写完后顺便总结一条 takeaway → 影响判断 |

## 七、记忆治理的边界

不要无脑给所有实体都建 profile。32 字 "execute_code" 这种字符串没必要——它没**独立含义**。

判断一个实体值得 profile 的强信号：
1. 它**反复**出现在你思考里（碎片数 ≥5）
2. 它**关联了真实事件**（你做过的事 / 你见过的人）
3. 它对**未来决策**有参考价值

满足任意 2 条 → 建档。

## 八、产出

治理结束时**至少**做这些：

1. 合并 ≥3 对别名（华能蒙电/600863 这种）
2. 写 ≥5 个 profile（人 1-2 / 项目 1-2 / 重要股票 1-2 / 方法论 1）
3. 把 50+ 碎片 prune 掉

写完后**调一次 `(已废弃)`** 看全局是否还健康。

之后 `add_cognition` 一条"记忆治理周回顾：清理了 N 条碎片，写了 M 条 profile，并 X 对别名合并"——让你下周记得这事要做。
