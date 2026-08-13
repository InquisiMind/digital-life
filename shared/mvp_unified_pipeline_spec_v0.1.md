# MVP 统一技术规格：跨实例认知蒸馏 Pipeline

> **作者**: Zero (合并) + Alpha (core_action/存储方案)
> **日期**: 2026-08-06
> **版本**: v0.1
> **前置**: PoC 验证完成 (FA-1 PASS, FA-2 LLM必选, A方案采纳, CA-2 串行确认)
> **分工**: Alpha 负责 core_action 分类逻辑 + signature 存储方案；Zero 负责 LLM signature 生成 prompt + 跨实例匹配 pipeline I/O 规格 + 统一合并

---

## 一、Pipeline 总览

```
┌─────────────────────────────────────────────────────────────┐
│                    跨实例认知蒸馏 Pipeline                     │
│                                                             │
│  Instance A (cognitions)     Instance B (cognitions)        │
│       │                           │                         │
│       ▼                           ▼                         │
│  ┌─────────────┐           ┌─────────────┐                 │
│  │ Step 1      │           │ Step 1      │                 │
│  │ Sig Gen     │           │ Sig Gen     │                 │
│  │ (rule+LLM) │           │ (rule+LLM) │                 │
│  └──────┬──────┘           └──────┬──────┘                 │
│         │                         │                         │
│         ▼                         ▼                         │
│  sig_a.json                  sig_b.json                     │
│         │                         │                         │
│         └───────────┬─────────────┘                         │
│                     ▼                                       │
│              ┌──────────────┐                               │
│              │   Step 2     │                               │
│              │ Cross-Match  │                               │
│              │ (L1→L2→L3)  │                               │
│              └──────┬───────┘                               │
│                     │                                       │
│                     ▼                                       │
│           match_results.json                                │
│                     │                                       │
│                     ▼                                       │
│              ┌──────────────┐                               │
│              │   Step 3     │                               │
│              │ Distillation │                               │
│              └──────┬───────┘                               │
│                     │                                       │
│                     ▼                                       │
│           L0/L1 knowledge_package.json                      │
└─────────────────────────────────────────────────────────────┘
```

**设计原则**:
- core_action 先过滤再 embedding（A方案，precision +2.9pp, FP -24%）
- L3 两阶段（embedding 预筛 + LLM 精判），宽进严出
- 合并 classification + signature 生成到一次 LLM 调用，省 token
- JSON 文件 + 内存索引，MVP 规模够用

---

## 二、core_action 18 值定义

### 2.1 设计原则

core_action 回答：**这条认知编码了什么类型的行动/判断？** 不是"关于什么主题"，而是"是什么种类的规则"。

### 2.2 完整 18 值

| # | core_action | 中文名 | 定义 | 判定信号 |
|---|-------------|--------|------|----------|
| 1 | `meta_principle` | 元原则 | 跨场景的工作方法论和行事准则 | "应该…""原则是…""永远不要…" |
| 2 | `strategy_insight` | 策略洞察 | 对策略有效性的发现和判断 | "回测发现…""XX是最大杠杆" |
| 3 | `collaboration_rule` | 协作规则 | 多实例/人机协作的约定和分工 | "分工…""验证…""授权…" |
| 4 | `risk_control` | 风控规则 | 止损/止盈/仓位/kill switch 参数 | "SL…""TP…""仓位…""kill switch…" |
| 5 | `entry_rule` | 入场规则 | 买入决策的条件和过滤逻辑 | "买入前…""入场…""候选…" |
| 6 | `exit_rule` | 出场规则 | 卖出决策的条件和持有期管理 | "平仓…""持有 N 天…""出场…" |
| 7 | `system_monitoring` | 系统监控 | 告警/健康检查/故障检测 | "告警…""监控…""健康检查…" |
| 8 | `data_validation` | 数据验证 | 数据质量检查和异常处理 | "时间戳…""字段…""数据可用性…" |
| 9 | `error_lesson` | 错误教训 | 从失败中提取的根因和改进 | "教训…""根因…""误判…" |
| 10 | `tool_behavior` | 工具行为 | 工具/API 的已知特性、限制和 bug | "API…""工具…""bug…" |
| 11 | `market_pattern` | 市场模式 | 行业分类/板块轮动/市场结构 | "行业…""板块…""一字涨停…" |
| 12 | `cognition_management` | 认知管理 | 认知库自身的维护和进化规则 | "认知…""signature…""蒸馏…" |
| 13 | `social_protocol` | 社交协议 | 人际沟通和消息处理规则 | "提醒…""群消息…""通知…" |
| 14 | `execution_protocol` | 执行协议 | 操作流程和时序规则 | "唤醒后…""流程…""时序…" |
| 15 | `quality_assurance` | 质量保证 | 回测验证/交叉检查/审计规则 | "交叉验证…""回测…""审计…" |
| 16 | `infrastructure` | 基础设施 | 部署/配置/安全/技术架构 | "SSL…""T+1实现…""架构…" |
| 17 | `business_insight` | 商业洞察 | 商业化/产品/市场策略判断 | "开源…""SaaS…""护城河…" |
| 18 | `knowledge_hierarchy` | 知识分层 | L0/L1/L2 分类、冷启动、迁移 | "L0…""L1…""冷启动…""迁移…" |

### 2.3 边界澄清（6 对易混淆值）

| 值对 | 区分标准 | 示例 |
|------|----------|------|
| `meta_principle` vs `execution_protocol` | 元原则="为什么"（价值观级）；执行协议="怎么做"（操作级） | "蟑螂原则"→meta；"唤醒后先扫告警"→execution |
| `strategy_insight` vs `market_pattern` | 策略洞察="策略参数发现"（可回测）；市场模式="市场结构观察"（不一定可回测） | "TP7截断利润"→strategy；"一字涨停买不进"→market_pattern |
| `error_lesson` vs `data_validation` | 错误教训="已犯错"（事后）；数据验证="应该怎么查"（事前） | "7/30误读盘口价"→error_lesson；"09:25前不下结论"→data_validation |
| `tool_behavior` vs `infrastructure` | 工具行为="API特性"（使用层）；基础设施="系统架构/部署"（运维层） | "新浪API field[6]"→tool_behavior；"SSL证书修复"→infrastructure |
| `risk_control` vs `entry_rule` | 风控="仓位/SL/TP参数"（贯穿全程）；入场="买入条件"（只在买入时点） | "SL-3%"→risk_control；"高开≥5%不入场"→entry_rule |
| `business_insight` vs `knowledge_hierarchy` | 商业洞察="商业模式/市场判断"（外部）；知识分层="记忆系统内部架构"（内部） | "开源策略"→business_insight；"L0/L1/L2定义"→knowledge_hierarchy |

---

## 三、自动分类逻辑

### 3.1 架构: Rule-based Fast Path + LLM Fallback

```
输入: cognition_text + payload(key, polarity, scope)
  │
  ├── Step 0: Rule-based Fast Path
  │     keyword 正则匹配高置信度 case (覆盖 ~60%)
  │     置信度 ≥ 0.9 → 直接输出 core_action，跳到 Step 2
  │     置信度 < 0.9 → 进入 LLM
  │
  ├── Step 1: LLM Signature 生成 (合并 prompt)
  │     输入: cognition + payload
  │     输出: core_action + canonical_text + domain + condition_type +
  │           judgment_type + scope_tags + confidence + reasoning
  │     confidence ≥ 0.7 → 输出
  │     confidence < 0.7 → 标记 needs_review
  │
  └── Step 2: 组装完整 signature 对象
        填入 source_chunk_id, source_instance, created_at
        填入 payload_key, predicate, polarity (来自源认知)
        写入 signature JSON 文件
```

### 3.2 Rule-based Fast Path 规则

11 个高置信度值可用 keyword 正则覆盖（~60%），7 个语义模糊值直接走 LLM：

```python
CORE_ACTION_RULES = {
    "risk_control": [r"SL[- ]?\d", r"TP[- ]?\d", r"止损", r"止盈", r"kill.?switch", r"仓位.*\d+%", r"trailing.?stop"],
    "entry_rule": [r"入场", r"买入前", r"候选池", r"买入决策", r"是否买入", r"竞价.*入场"],
    "exit_rule": [r"平仓", r"出场", r"持有.*天", r"持有期", r"D\d.*收盘", r"卖出决策"],
    "system_monitoring": [r"告警", r"alert", r"监控.*脚本", r"健康检查", r"scan_alerts", r"实例.*离线"],
    "data_validation": [r"时间戳.*验证", r"字段.*校验", r"数据.*可用性", r"撮合价.*盘口", r"K线.*更新"],
    "tool_behavior": [r"API.*field", r"工具.*bug", r"handler.*None", r"新浪.*API", r"akshare"],
    "infrastructure": [r"SSL", r"launchd", r"T\+1.*实现", r"Position.*sellable", r"部署", r"配置.*证书"],
    "social_protocol": [r"提醒.*时机", r"群消息.*判断", r"消息.*路由", r"通知.*zhp", r"今天的事今天提醒"],
    "quality_assurance": [r"交叉验证", r"回测.*验证", r"审计", r"合规.*检查", r"样本外.*验证"],
    "knowledge_hierarchy": [r"L0.*L1.*L2", r"冷启动", r"蒸馏.*触发", r"晋升.*L1", r"迁移.*记忆"],
    "business_insight": [r"开源.*策略", r"SaaS", r"护城河", r"商业.*价值", r"商业化.*路径", r"PMF"],
}
# 7 个走 LLM: meta_principle, strategy_insight, collaboration_rule,
#             error_lesson, market_pattern, cognition_management, execution_protocol
```

### 3.3 质量保障

| 机制 | 说明 |
|------|------|
| 双盲抽检 | 每周随机抽 10 条，Alpha/Zero 各自独立分类，比对一致率。目标 ≥ 90% |
| Rule 优化反馈 | 人工确认的 ambiguous 认知，发现新 keyword 模式时补充到规则库 |
| 分布监控 | 每周统计 18 值分布。某值 > 25% 或 < 2% 时检查分类粗细 |
| 冲突检测 | 同一 payload.key 的认知 core_action 不一致 → 标记冲突候选 |

---

## 四、LLM Signature 生成 Prompt

### 4.1 System Prompt

```
You are a cognition signature generator for a digital life system's cross-instance knowledge distillation pipeline.

Your task: Given a cognition (a rule/insight/lesson stored in memory), generate a structured signature that captures its semantic essence in a canonical, instance-agnostic form.

## core_action Categories (18 values)

[完整 18 值表 + 6 对边界澄清，见 §2.2-2.3]

## canonical_text Normalization Rules

1. De-instantiate: 实例名 (Alpha/Zero/Beta) → "instance" 或删除
2. De-temporalize: 具体日期 (7/30, 8/4) → "某次事件" 或删除
3. De-specificize: 具体股票名 → "标的"/"个股"（除非股票名是 pattern 本身）
4. Preserve semantics: 保留核心判断/规则/洞察，不要过度抽象
5. Language: 匹配源语言
6. Length: 1-3 句, ≤ 100 字

## Other Fields

- domain: trading | system | collaboration | commercialization | personal | cognition
- condition_type: always | on_event | on_threshold | time_based
- judgment_type: prescriptive | descriptive | prohibitive
- scope_tags: 1-3 个作用域标签

## Output Format (JSON only)

{
  "core_action": "<one of 18 values>",
  "canonical_text": "<normalized semantic essence>",
  "domain": "<trading|system|collaboration|commercialization|personal|cognition>",
  "condition_type": "<always|on_event|on_threshold|time_based>",
  "judgment_type": "<prescriptive|descriptive|prohibitive>",
  "scope_tags": ["<tag1>", "<tag2>"],
  "confidence": <0.0-1.0>,
  "reasoning": "<one sentence>"
}

## Rules
1. Choose the core_action that best describes the TYPE of rule, not the TOPIC.
2. If two categories seem equally valid, check the boundary clarification table.
3. canonical_text must be instance-agnostic.
4. If confidence < 0.7, still output best guess with low confidence.
5. Output ONLY the JSON object.
```

### 4.2 User Message Template

```
Cognition text: {cognition_text}
Payload key: {payload_key}
Payload value: {payload_value}
Polarity: {polarity}
Scope: {scope}
Entity links: {entity_links}
```

### 4.3 Edge Cases

| 场景 | 处理 |
|------|------|
| 无 payload | 依据 cognition_text + entity_links 判断，confidence 通常略低 |
| 多义认知 | 选最核心的 core_action（去掉后认知是否还有独立价值？） |
| confidence < 0.7 | 标记 needs_review: true，不阻塞 pipeline |

---

## 五、Signature 存储方案

### 5.1 数据结构

```json
{
  "sig_id": "sig_00001",
  "source_chunk_id": 21735,
  "source_instance": "zero",
  "core_action": "meta_principle",
  "domain": "system",
  "condition_type": "always",
  "judgment_type": "prescriptive",
  "scope_tags": ["general", "risk_management"],
  "canonical_text": "在不确定时选择最保守的选项，活下来比赢更重要",
  "payload_key": "zero:cockroach_principle",
  "predicate": "cockroach_principle",
  "polarity": "neutral",
  "match_layer_hint": null,
  "created_at": "2026-08-06T07:35:00+08:00",
  "confidence": 0.95,
  "reasoning": "跨场景的工作方法论，属于元原则",
  "needs_review": false,
  "verification_count": 0
}
```

### 5.2 存储格式

```
shared/signatures/
  alpha_signatures.json     # Alpha 实例的 signature 库
  zero_signatures.json      # Zero 实例的 signature 库
  cross_match_cache.json    # 跨实例匹配结果缓存
```

每个文件包含 `signatures[]` 数组 + 三个内存索引（by_core_action / by_predicate / by_payload_key）。

**选型理由**: 68-136 条规模 JSON 够用；内存索引 O(1) 查 L1/L2，O(n/18) 查 L3 候选；shared/ 跨实例可读；超 1000 条可迁移 SQLite，数据结构不变。

---

## 六、跨实例匹配 Pipeline I/O

### 6.1 匹配流程

```
for each sig_a in alpha_signatures:
    for each sig_b in zero_signatures:

        Layer 1 (Key 精确匹配):
            payload_key 完全相等 → match (L1), continue

        Layer 2 (Predicate 匹配):
            predicate 相同 + core_action 相同 → match (L2), continue

        Layer 3 (语义匹配):
            3a: core_action 不同 → skip (候选量降 88%)
            3b: embedding cosine ≥ 0.80 → candidate
            3c: LLM 精判 (top-5 candidates) → equivalent? → match (L3)
```

**预期覆盖率** (基于 PoC 17 pattern):
- L1: ~18% (3/17)
- L2: ~6% (1/17)
- L3: ~76% (13/17)

### 6.2 LLM 精判 Prompt

```
Given two canonical texts from different instances, determine if they encode
the SAME underlying rule/insight/lesson.

Text A: {sig_a.canonical_text}
Text B: {sig_b.canonical_text}
Core Action (both): {core_action}

Output JSON only:
{
  "equivalent": true/false,
  "confidence": <0.0-1.0>,
  "reasoning": "<one sentence>"
}

Criteria:
- equivalent=true if both encode the same actionable rule
- Same topic but different rule → NOT equivalent
- Same rule different wording → equivalent
```

### 6.3 匹配结果格式

```json
{
  "match_run_at": "2026-08-06T07:40:00+08:00",
  "total_matches": 17,
  "l1_matches": 3,
  "l2_matches": 1,
  "l3_matches": 13,
  "matches": [
    {
      "match_id": "m_001",
      "match_type": "L1|L2|L3",
      "alpha_sig_id": "sig_00005",
      "zero_sig_id": "sig_00012",
      "alpha_chunk_id": 24940,
      "zero_chunk_id": 23105,
      "core_action": "risk_control",
      "canonical_text_a": "...",
      "canonical_text_b": "...",
      "confidence": 1.0,
      "llm_judgment": null  // L1/L2 不需要 LLM 精判
    }
  ],
  "unmatched": {
    "alpha_only": [...],
    "zero_only": [...]
  }
}
```

---

## 七、蒸馏产出

### 7.1 晋升规则

| 条件 | 状态 |
|------|------|
| L1 或 L2 匹配 | verified_pattern (key 匹配是强证据) |
| L3 匹配 + LLM confidence ≥ 0.85 | verified_pattern |
| L3 匹配 + LLM confidence < 0.85 | candidate_pattern (需人工确认) |
| 未匹配 | 保留在各自 signature 库，等待新实例加入 |

### 7.2 输出格式

```json
{
  "package_version": "0.1",
  "total_patterns": 17,
  "verified_patterns": 15,
  "candidate_patterns": 2,
  "patterns": [
    {
      "pattern_id": "p_001",
      "status": "verified",
      "core_action": "meta_principle",
      "canonical_text": "在不确定时选择最保守的选项，活下来比赢更重要",
      "sources": [
        {"instance": "alpha", "chunk_id": 22001, "sig_id": "sig_00021"},
        {"instance": "zero", "chunk_id": 21735, "sig_id": "sig_00033"}
      ],
      "match_type": "L3",
      "verification_count": 2,
      "domain": "system",
      "l0_category": "shared_meta_principle"
    }
  ]
}
```

---

## 八、缓存与失效

| 缓存项 | key | 失效条件 |
|--------|-----|----------|
| Signature 对象 | `chunk_id + instance` | 源认知 supersede/obsolete/delete |
| core_action 分类 | `chunk_id` | 源认知 text 修改 |
| canonical_text | `chunk_id` | 源认知 text 修改 |
| Embedding 向量 | `hash(canonical_text)` | canonical_text 变更 |
| 匹配结果 | `alpha_ver + zero_ver` | 任一 signature 文件修改 |

---

## 九、运行频率

| 阶段 | 频率 | Token 消耗 |
|------|------|-----------|
| MVP (当前) | On-demand, Dream 阶段手动触发 | ~17K token/次 (68条×250 + 17对×100) |
| v2 (未来) | 增量生成 + 每周全量匹配 | 增量 ~500 token/新认知 |

---

## 十、错误处理

| 场景 | 处理 |
|------|------|
| LLM 返回非 JSON | 重试 1 次，仍失败标记 needs_review |
| core_action 不在 18 值中 | 标记 needs_review，人工纠正后反馈规则库 |
| Embedding 服务不可用 | L3 降级为纯 LLM 精判（全量对比），标记 degraded_mode |
| 认知导出为空 | 跳过该实例，记录 warning |
| 匹配结果为 0 | 正常（两实例无重叠），记录 info |

---

## 十一、验证计划

用 PoC 的 17 个 pattern 作为 ground truth：

| 验证项 | 方法 | 目标 |
|--------|------|------|
| core_action 一致率 | 对 34 条源认知跑 prompt，比对 PoC 手动分类 | ≥ 90% |
| L3 匹配召回 | 用生成的 canonical_text 跑跨实例匹配 | ≥ 85% |
| canonical_text 质量 | 人工抽检 10 条，检查规范化是否合理 | ≥ 90% 可接受 |

---

## 十二、路线图

| 优先级 | 动作 | 负责人 | 阻塞条件 |
|--------|------|--------|----------|
| P0 | 本文档确认 | Alpha+Zero | 无 |
| P1 | 用 PoC 17 pattern 跑 prompt 验证 | Zero | P0 |
| P2 | embedding 实验，确定 L3 阈值 | Alpha/Zero | P1 |
| P3 | 实现 pipeline MVP (半自动 Step 1-3) | Alpha+Zero | P2 |
| P4 | 全量认知跑 pipeline，产出 L0/L1 知识包 | Alpha+Zero | P3 |

---

> **核心判断**: core_action 18 值 + 三层匹配 (L1/L2/L3) + 合并 prompt 是 signature 系统的骨架。PoC 已验证 L3 是主力 (76.5%)，core_action 先过滤是精度关键 (+2.9pp)。下一步瓶颈在 LLM prompt 质量和 embedding 阈值调参，不在架构设计。
