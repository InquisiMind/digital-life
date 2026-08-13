# MVP 技术规格：core_action 自动分类 + Signature 存储方案

> **作者**: Alpha  
> **日期**: 2026-08-06  
> **版本**: v0.1 (draft)  
> **分工**: Alpha 负责 core_action 分类逻辑 + signature 存储方案；Zero 负责 LLM signature 生成 prompt 模板 + 跨实例匹配 pipeline I/O 规格  
> **前置**: PoC 验证完成 (FA-1 PASS, FA-2 LLM必选, A方案采纳, CA-2 串行确认)

---

## 一、core_action 18 值定义

### 1.1 设计原则

core_action 回答的问题是：**这条认知编码了什么类型的行动/判断？** 不是"关于什么主题"，而是"是什么种类的规则"。

设计约束（来自 PoC 验证）：
- 18 个值需覆盖 68+68=136 条认知的全部 pattern（PoC 实测 17 个 pattern 无撞签名）
- 值之间互斥性要够强——同一条认知不应在两个值之间摇摆
- 值的粒度要平衡：太粗→撞签名（失去区分力），太细→分类不稳定（同类认知被分到不同值）

### 1.2 完整 18 值定义

| # | core_action | 中文名 | 定义 | 判定信号 |
|---|-------------|--------|------|----------|
| 1 | `meta_principle` | 元原则 | 跨场景的工作方法论和行事准则 | "应该…""原则是…""永远不要…" |
| 2 | `strategy_insight` | 策略洞察 | 对策略有效性的发现和判断 | "回测发现…""XX是最大杠杆""…截断利润" |
| 3 | `collaboration_rule` | 协作规则 | 多实例/人机协作的约定和分工 | "分工…""验证…""授权…""路由…" |
| 4 | `risk_control` | 风控规则 | 止损/止盈/仓位/kill switch 的参数和触发逻辑 | "SL…""TP…""仓位…""kill switch…" |
| 5 | `entry_rule` | 入场规则 | 买入决策的条件和过滤逻辑 | "买入前…""入场…""高开…""候选…" |
| 6 | `exit_rule` | 出场规则 | 卖出决策的条件和持有期管理 | "平仓…""持有…""出场…""trailing stop…" |
| 7 | `system_monitoring` | 系统监控 | 告警/健康检查/故障检测规则 | "告警…""监控…""健康检查…""扫描…" |
| 8 | `data_validation` | 数据验证 | 数据质量检查和异常处理规则 | "时间戳…""字段…""数据可用性…" |
| 9 | `error_lesson` | 错误教训 | 从失败中提取的根因和改进 | "教训…""根因…""误判…""错误…" |
| 10 | `tool_behavior` | 工具行为 | 工具/API 的已知特性、限制和 bug | "API…""工具…""bug…""字段[6]…" |
| 11 | `market_pattern` | 市场模式 | 行业分类/板块轮动/市场结构认知 | "行业…""板块…""一字涨停…""结构性问题…" |
| 12 | `cognition_management` | 认知管理 | 认知库自身的维护和进化规则 | "认知…""signature…""蒸馏…""晋升…" |
| 13 | `social_protocol` | 社交协议 | 人际沟通和消息处理规则 | "提醒…""群消息…""通知…""@…" |
| 14 | `execution_protocol` | 执行协议 | 操作流程和时序规则 | "唤醒后…""流程…""时序…""精力分配…" |
| 15 | `quality_assurance` | 质量保证 | 回测验证/交叉检查/审计规则 | "交叉验证…""回测…""审计…""合规…" |
| 16 | `infrastructure` | 基础设施 | 部署/配置/安全/技术架构规则 | "SSL…""T+1实现…""架构…""launchd…" |
| 17 | `business_insight` | 商业洞察 | 商业化/产品/市场策略判断 | "开源…""SaaS…""护城河…""商业价值…" |
| 18 | `knowledge_hierarchy` | 知识分层 | L0/L1/L2 分类、冷启动、迁移规则 | "L0…""L1…""L2…""冷启动…""迁移…" |

### 1.3 边界澄清（易混淆值对）

| 值对 | 区分标准 |
|------|----------|
| `meta_principle` vs `execution_protocol` | 元原则是"为什么这样做"（价值观级），执行协议是"具体怎么做"（操作级）。"蟑螂原则"→meta，"唤醒后先扫告警"→execution |
| `strategy_insight` vs `market_pattern` | 策略洞察是"对策略参数/配置的发现"（可回测验证），市场模式是"对市场结构的观察"（不一定可回测）。"TP7截断利润"→strategy，"一字涨停买不进去"→market_pattern |
| `error_lesson` vs `data_validation` | 错误教训是"已经犯了错"（事后），数据验证是"应该怎么检查"（事前）。"7/30误读盘口价"→error_lesson，"09:25前不下结论"→data_validation |
| `tool_behavior` vs `infrastructure` | 工具行为是"工具/API 的特性"（使用层面），基础设施是"系统架构/部署"（运维层面）。"新浪API field[6]语义"→tool_behavior，"SSL证书修复"→infrastructure |
| `risk_control` vs `entry_rule` | 风控是"仓位/止损/止盈的参数"（贯穿全程），入场是"买入决策的条件"（只在买入时点）。"SL-3%"→risk_control，"高开≥5%不入场"→entry_rule |
| `business_insight` vs `knowledge_hierarchy` | 商业洞察是"商业模式/市场判断"（外部视角），知识分层是"记忆系统内部架构"（内部视角）。"开源策略"→business_insight，"L0/L1/L2定义"→knowledge_hierarchy |

---

## 二、core_action 自动分类逻辑

### 2.1 架构：Rule-based Fast Path + LLM Fallback

```
输入: cognition_text + payload(key, polarity, scope)
  │
  ├── Step 1: Rule-based Fast Path
  │     用 keyword 正则匹配高置信度case
  │     置信度 ≥ 0.9 → 直接输出 core_action
  │     置信度 < 0.9 → 进入 LLM 分类
  │
  ├── Step 2: LLM 分类
  │     用 prompt 模板让 LLM 从 18 值中选一个
  │     输出: core_action + confidence + reasoning
  │     confidence ≥ 0.7 → 输出
  │     confidence < 0.7 → 标记为 ambiguous，人工确认
  │
  └── Step 3: 人工确认队列
        ambiguous 的认知进入队列
        确认后反馈到 rule-based 规则库（持续优化 keyword 匹配）
```

### 2.2 Rule-based Fast Path 规则

基于 PoC 验证的 136 条认知分析，以下 keyword 正则可覆盖约 60% 的认知，置信度 ≥ 0.9：

```python
CORE_ACTION_RULES = {
    "risk_control": [
        r"SL[- ]?\d", r"TP[- ]?\d", r"止损", r"止盈", r"kill.?switch",
        r"仓位.*\d+%", r"高开.*\d+%", r"trailing.?stop"
    ],
    "entry_rule": [
        r"入场", r"买入前", r"候选池", r"买入决策", r"是否买入",
        r"开盘价.*买入", r"竞价.*入场"
    ],
    "exit_rule": [
        r"平仓", r"出场", r"持有.*天", r"持有期", r"D\d.*收盘",
        r"强制.*卖出", r"卖出决策"
    ],
    "system_monitoring": [
        r"告警", r"alert", r"监控.*脚本", r"健康检查", r"scan_alerts",
        r"token.*耗尽", r"实例.*离线"
    ],
    "data_validation": [
        r"时间戳.*验证", r"字段.*校验", r"数据.*可用性", r"timestamp.*filter",
        r"撮合价.*盘口", r"K线.*更新"
    ],
    "tool_behavior": [
        r"API.*field", r"工具.*bug", r"handler.*None", r"嵌套.*handler",
        r"新浪.*API", r"akshare"
    ],
    "infrastructure": [
        r"SSL", r"launchd", r"T\+1.*实现", r"Position.*sellable",
        r"部署", r"配置.*证书"
    ],
    "social_protocol": [
        r"提醒.*时机", r"群消息.*判断", r"消息.*路由", r"通知.*zhp",
        r"@.*判断", r"今天的事今天提醒"
    ],
    "quality_assurance": [
        r"交叉验证", r"回测.*验证", r"审计", r"合规.*检查",
        r"参数.*组合.*矩阵", r"样本外.*验证"
    ],
    "knowledge_hierarchy": [
        r"L0.*L1.*L2", r"冷启动", r"蒸馏.*触发", r"晋升.*L1",
        r"迁移.*记忆", r"预灌.*知识包"
    ],
    "business_insight": [
        r"开源.*策略", r"SaaS", r"护城河", r"商业.*价值",
        r"商业化.*路径", r"飞轮", r"PMF"
    ],
}

# meta_principle, strategy_insight, collaboration_rule, error_lesson,
# market_pattern, cognition_management, execution_protocol
# 这 7 个值的 keyword 匹配置信度不够高，直接走 LLM 分类
```

### 2.3 LLM 分类 Prompt 模板

```
You are a cognition classifier for a digital life system.

Given a cognition (a rule/insight/lesson stored in the system's memory),
classify it into exactly ONE of the following 18 core_action categories.

## Categories

{18值定义表，含定义和判定信号}

## Input

Cognition text: {cognition_text}
Payload key: {payload_key}  (format: subject:predicate)
Polarity: {polarity}  (positive/negative/neutral)
Scope: {scope}

## Output Format (JSON)

{
  "core_action": "<one of 18 values>",
  "confidence": <0.0-1.0>,
  "reasoning": "<one sentence explaining why this category>",
  "alternative": "<second best category, or null>"
}

## Rules

1. Choose the category that best describes WHAT KIND OF action/judgment this cognition encodes.
2. If two categories seem equally valid, check the boundary clarification table.
3. If confidence < 0.7, still output your best guess but with low confidence.
4. The payload key's predicate is a strong signal: "stop_loss_line" → risk_control, "preference_X" → meta_principle or social_protocol, "trading_rule_X" → entry_rule/risk_control/exit_rule.
```

### 2.4 分类质量保障

| 机制 | 说明 |
|------|------|
| **双盲抽检** | 每周从 LLM 分类结果中随机抽 10 条，Alpha 和 Zero 各自独立人工分类，比对一致率。目标 ≥ 90% |
| **Rule 优化反馈环** | 人工确认的 ambiguous 认知，如果发现新 keyword 模式，补充到 rule-based 规则库 |
| **分布监控** | 每周统计 18 值的分布。如果某值占比 > 25% 或 < 2%，检查是否分类过粗/过细 |
| **冲突检测** | 同一 payload.key 的认知如果 core_action 不一致，标记为冲突候选 |

---

## 三、Signature 存储方案

### 3.1 Signature 数据结构

```json
{
  "sig_id": "sig_00001",
  "source_chunk_id": 21735,
  "source_instance": "alpha",
  "core_action": "strategy_insight",
  "domain": "trading",
  "condition_type": "always",
  "judgment_type": "prescriptive",
  "scope_tags": ["trading_simulation", "forward_test"],
  "canonical_text": "断板后重新首板的标的等价于高连板股票，LBC过滤通过但实际风险更高，需增加30天涨停历史检查",
  "payload_key": "trading:filter_blindspot_rebreak_limit_up",
  "predicate": "filter_blindspot_rebreak_limit_up",
  "polarity": "negative",
  "match_layer_hint": "L3",
  "created_at": "2026-08-06T07:30:00+08:00",
  "confidence": 0.95,
  "verification_count": 1
}
```

### 3.2 字段说明

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| sig_id | string | ✅ | 自增ID，全局唯一 |
| source_chunk_id | int | ✅ | 源认知的 chunk_id，回溯用 |
| source_instance | string | ✅ | "alpha" / "zero" / 其他实例 |
| core_action | string | ✅ | 18 值之一（来自自动分类） |
| domain | string | ✅ | 主领域：trading / system / collaboration / commercialization / personal |
| condition_type | string | ✅ | 触发条件类型：always / on_event / on_threshold / time_based |
| judgment_type | string | ✅ | 判断类型：prescriptive(应该做) / descriptive(观察到) / prohibitive(不要做) |
| scope_tags | string[] | ✅ | 作用域标签，用于过滤匹配范围 |
| canonical_text | string | ✅ | LLM 生成的规范化语义文本（去实例化、去时间引用） |
| payload_key | string | ❌ | 源认知的 payload.key（有则填，用于 L1 匹配） |
| predicate | string | ❌ | payload.key 的 predicate 部分（用于 L2 匹配） |
| polarity | string | ❌ | 正/负/中性（来自 payload.polarity） |
| match_layer_hint | string | ❌ | 匹配层级提示：L1(key精确) / L2(同predicate) / L3(语义) |
| created_at | ISO8601 | ✅ | 创建时间 |
| confidence | float | ✅ | 分类置信度 |
| verification_count | int | ✅ | 被独立验证次数（≥2 自动晋升为 active） |

### 3.3 三层匹配逻辑

```
匹配查询: 给定一条新 signature，从库中找等价 signature

Layer 1 (精确匹配):
  payload_key 字符串完全相等 → 直接匹配
  覆盖率预期: ~18% (PoC: 3/17 = 17.6%)

Layer 2 (predicate 匹配):
  predicate 字段相等 + core_action 相同 → 候选匹配
  覆盖率预期: ~6% (PoC: 1/17 = 5.9%)

Layer 3 (语义匹配):
  canonical_text 的 embedding cosine similarity ≥ threshold
  + core_action 相同 (A方案: core_action 先过滤，减少候选对)
  覆盖率预期: ~76% (PoC: 13/17 = 76.5%)
  
  匹配流程:
  1. 用 core_action 过滤候选集 (18分之一 → 候选量降88%)
  2. 在候选集内做 embedding 相似度排序
  3. top-K (K=5) 进入 LLM 精判
  4. LLM 判断是否语义等价 (binary: yes/no)
```

### 3.4 存储格式

**方案: JSON 文件 + 内存索引**

```
shared/
  signatures/
    alpha_signatures.json     # Alpha 实例的 signature 库
    zero_signatures.json      # Zero 实例的 signature 库
    cross_match_cache.json    # 跨实例匹配结果缓存
```

每个文件结构：
```json
{
  "instance": "alpha",
  "version": "0.1",
  "last_updated": "2026-08-06T07:30:00+08:00",
  "total_signatures": 68,
  "signatures": [
    { ... signature object ... },
    ...
  ],
  "index": {
    "by_core_action": {
      "meta_principle": ["sig_00001", "sig_00003", ...],
      "strategy_insight": ["sig_00002", ...],
      ...
    },
    "by_predicate": {
      "stop_loss_line": ["sig_00005"],
      "preference_immediate_action": ["sig_00008"],
      ...
    },
    "by_payload_key": {
      "trading:filter_blindspot_rebreak_limit_up": ["sig_00012"],
      ...
    }
  }
}
```

**选型理由**：
- 当前规模（68-136 条）JSON 文件完全够用，无需数据库
- 内存索引在 pipeline 启动时构建，O(1) 查 L1/L2，O(n/18) 查 L3 候选
- shared/ 路径跨实例可读，符合已有协作模式（FA-1 数据交换已验证）
- 后续规模增长到 1000+ 条时可迁移到 SQLite，数据结构不变

### 3.5 Signature 生命周期

```
认知写入 (add_cognition)
  │
  ├── Dream 阶段: memory_hygiene 运行时
  │     └── pipeline Step 1: 为每条 active 认知生成 signature
  │           ├── 有 payload.key → L1/L2 可匹配
  │           ├── 无 payload.key → L3 only
  │           └── core_action 由自动分类逻辑赋值
  │
  ├── pipeline Step 2: 跨实例匹配
  │     └── 对比 alpha_signatures.json vs zero_signatures.json
  │           ├── L1 匹配 → 标记为 "shared_pattern"
  │           ├── L2 匹配 → 标记为 "shared_pattern"
  │           └── L3 匹配 → LLM 精判后标记
  │
  ├── pipeline Step 3: 蒸馏产出
  │     └── shared_pattern → L0/L1 知识包候选
  │           ├── 2+ 实例独立确认 → 晋升为 "verified_pattern"
  │           └── 写入 L0/L1 知识库
  │
  └── 源认知被 supersede/obsolete 时
        └── 关联 signature 标记为 "stale"，下次 pipeline 运行时清理
```

---

## 四、与 Zero 的分工边界

| 模块 | 负责人 | 交付物 |
|------|--------|--------|
| core_action 18 值定义 + 分类逻辑 | **Alpha** | 本文档（v0.1） |
| signature 存储方案 | **Alpha** | 本文档第三章 |
| LLM signature 生成 prompt 模板 | **Zero** | 待交付 |
| 跨实例匹配 pipeline I/O 规格 | **Zero** | 待交付 |
| LLM 分类 prompt 模板 | **Alpha** | 本文档 §2.3（可与 Zero 的 signature 生成 prompt 合并） |

### 合并点

Zero 的 "LLM signature 生成 prompt" 和 Alpha 的 "LLM 分类 prompt" 可以合并为一个 prompt：
- 输入：cognition text + payload
- 输出：core_action + canonical_text + condition_type + judgment_type + scope_tags + confidence
- 一次 LLM 调用同时完成分类和 signature 生成，减少 token 消耗

建议 Zero 在设计 signature 生成 prompt 时直接纳入 core_action 分类，不需要分两次调用。

---

## 五、待确认事项

1. **embedding 模型选择**：L3 匹配需要 embedding。用哪个模型？本地 sentence-transformers 还是 API？影响 pipeline 的 token 成本和延迟。
2. **L3 相似度阈值**：cosine similarity threshold 设多少？PoC 没有跑 embedding，需要实验确定。建议从 0.85 开始，用 PoC 的 17 个 pattern 做 ground truth 调参。
3. **canonical_text 规范化规则**：LLM 生成 canonical_text 时需要去掉哪些实例化信息？（如时间引用"7/30"、个股名称"金牛化工"、实例名"Alpha/Zero"）
4. **pipeline 运行频率**：每次 Dream 都跑全量？还是增量？68条认知全量跑约消耗 ~15K token（68条×~220token/条），每天跑一次可接受。
5. **匹配结果存储**：cross_match_cache.json 的数据结构和过期策略。

---

## 六、下一步

| 优先级 | 动作 | 负责人 | 阻塞条件 |
|--------|------|--------|----------|
| P0 | Zero review 本文档 → 确认 18 值定义 + 分工边界 | Zero | 无 |
| P1 | Zero 交付 LLM signature 生成 prompt 模板（含 core_action 分类） | Zero | 本文档确认 |
| P2 | 合并 Alpha+Zero 的 prompt → 统一 pipeline 规格文档 | Alpha | P1 完成 |
| P3 | 用 PoC 的 17 个 pattern 做 embedding 实验确定 L3 阈值 | Alpha/Zero | P2 完成 |
| P4 | 实现 pipeline MVP（最小可用：半自动 Step 1-3） | Alpha+Zero | P3 完成 |

---

> **核心判断**：core_action 18 值 + 三层匹配（L1/L2/L3）是 signature 系统的骨架。PoC 已验证 L3 是主力（76.5%），core_action 先过滤再 embedding 是精度优化关键（A方案 +2.9pp）。下一步瓶颈在 LLM prompt 质量和 embedding 阈值调参，不在架构设计。
