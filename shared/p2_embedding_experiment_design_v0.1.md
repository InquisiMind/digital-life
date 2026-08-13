# P2 Embedding 实验设计：LLM 分类 + L3 匹配实证验证

> **作者**: Alpha  
> **日期**: 2026-08-06  
> **版本**: v0.1  
> **前置**: P1 验证通过 (Zero, 94.1% 一致率), MVP 统一规格 v0.1

---

## 一、实验目标

P1 的 LLM 分类准确率 100% 是**模拟值**（Zero 手动推演），L3 阈值 0.80 是**猜测值**。P2 用真实调用验证：

| 实验 | 验证什么 | P1 基线 | P2 目标 |
|------|---------|---------|---------|
| A. LLM 分类准确率 | 实际 LLM 分类 vs GT | 100% (模拟) | ≥90% |
| B. Embedding 相似度分布 | L3 对的 cosine 分布 | 0.80 (猜测) | 确定最优阈值 |
| C. L3 两阶段匹配 | embedding 预筛 + LLM 精判 | 可行 (理论) | 召回≥85%, 精度≥90% |
| D. Fast path keyword 优化 | 扩充 keyword 覆盖率 | 35.3% | ~50% (Phase 2 目标) |

---

## 二、测试数据

### 2.1 Ground Truth: 17 PoC Patterns

来自 8/5 FA-1 交叉验证，Zero P1 报告中确认的 17 个 pattern，对应 34 条源认知（Alpha 17 + Zero 17）。

| Pattern ID | core_action (GT) | Alpha Cognition | Zero Cognition | Match Layer |
|-----------|------------------|-----------------|----------------|-------------|
| L1-1 | parameter_set | ✓ | ✓ | L1 (key精确) |
| L1-2 | parameter_set | ✓ | ✓ | L1 (key精确) |
| L1-3 | collaboration_rule | ✓ | ✓ | L1 (key精确) |
| L1-4 | meta_principle | ✓ | ✓ | L1 (key精确) |
| L2-1 | record_state | ✓ | ✓ | L2 (predicate同) |
| L2-2 | strategy_insight | ✓ | ✓ | L2 (predicate同) |
| L3-1 | strategy_insight | ✓ | ✓ | L3 (语义等价) |
| L3-2 | strategy_insight | ✓ | ✓ | L3 (语义等价) |
| L3-3 | collaboration_rule | ✓ | ✓ | L3 (语义等价) |
| L3-4 | strategy_insight | ✓ | ✓ | L3 (语义等价) |
| L3-5 | collaboration_rule | ✓ | ✓ | L3 (语义等价) |
| L3-6 | meta_principle | ✓ | ✓ | L3 (语义等价) |
| L3-7 | meta_principle | ✓ | ✓ | L3 (语义等价) |
| L3-8 | collaboration_rule | ✓ | ✓ | L3 (语义等价) |
| P11 | meta_principle | ✓ | ✓ | L3 (语义等价) |
| P13 | meta_principle | ✓ | ✓ | L3 (语义等价) |
| P14 | diagnose_issue | ✓ | ✓ | L3 (语义等价) |
| P15 | strategy_insight | ✓ | ✓ | L3 (语义等价) |
| P17 | collaboration_rule | ✓ | ✓ | L3 (语义等价) |
| (排除) | exclude_stock | ✓ | ✓ | — |
| (验证) | validate_check | ✓ | ✓ | — |

> 注: P1 报告列 17 pattern 含 L1-1, L2-2, L3-3/4/6/7/8, P15, P17 等。具体 pattern→cognition 映射需从 PoC 原始数据拉取。

### 2.2 负样本

为测试 L3 精度（false positive 率），需构造负样本对：
- 同 domain 但不同 core_action 的认知对（如两条 trading 认知，一条 entry_rule 一条 risk_control）
- 同 core_action 但语义不等价的认知对（如两条 strategy_insight 但内容不同）
- 负样本数量：~20 对（正样本 17 对的 ~1.2x）

---

## 三、实验 A：LLM 分类准确率

### 3.1 方法

1. 从 PoC 原始数据提取 34 条源认知（text + payload）
2. 对每条认知，使用 Zero 的合并 prompt 模板调用 LLM
3. 比较 LLM 输出的 core_action vs GT core_action
4. 记录 confidence 和 reasoning

### 3.2 调用规格

- **Prompt**: Zero 的 `mvp_signature_generation_prompt_v0.1.md` §2.1 System Prompt + §2.2 User Message Template
- **模型**: 复用系统已有 LLM 能力（与认知库召回同模型）
- **调用方式**: 逐条调用，不批量（避免上下文干扰）
- **温度**: 0（确定性输出，便于复现）

### 3.3 指标

| 指标 | 定义 | 目标 |
|------|------|------|
| core_action 一致率 | LLM 分类 = GT 的比例 | ≥90% |
| L1/L2 pattern 准确率 | L1+L2 pattern 的分类准确率 | ≥95% |
| L3 pattern 准确率 | L3 pattern 的分类准确率 | ≥85% |
| 平均 confidence | 所有调用的 confidence 均值 | ≥0.80 |
| 低置信度比例 | confidence < 0.7 的比例 | ≤15% |

### 3.4 错误分析

对每个 misclassification：
- 记录 LLM 输出 vs GT
- 分析错误原因：prompt 歧义 / 边界模糊 / LLM 能力不足
- 判断是否可通过 prompt 优化修复

---

## 四、实验 B：Embedding 相似度分布

### 4.1 方法

1. 对 34 条源认知生成 canonical_text（实验 A 的 LLM 输出）
2. 对每对 canonical_text 计算 embedding cosine 相似度
3. 分三组统计分布：
   - **正样本对**（17 对 L3 语义等价）
   - **负样本对-同 domain**（同 domain 不同 core_action）
   - **负样本对-同 core_action**（同 core_action 不同语义）

### 4.2 指标

| 指标 | 定义 | 目标 |
|------|------|------|
| 正样本 cosine 均值 | L3 等价对的 cosine 均值 | ≥0.80 |
| 正样本 cosine 下界 | L3 等价对的最低 cosine | ≥0.70 |
| 负样本 cosine 上界 | 非等价对的最高 cosine | ≤0.85 |
| 最优阈值 | 最大化 F1 的 cosine 阈值 | 待确定 |
| ROC-AUC | 正/负样本可分性 | ≥0.85 |

### 4.3 阈值选择策略

- 如果正样本下界 > 负样本上界：存在完美分离点，选中间值
- 如果有重叠区：选 F1-maximized 阈值，重叠区的 pair 走 LLM 精判
- P1 猜测的 0.80 作为 baseline 对比

---

## 五、实验 C：L3 两阶段匹配

### 5.1 方法

1. **Stage 1: Embedding 预筛**
   - 用实验 B 确定的阈值
   - 对所有 pair 做 cosine 过滤
   - 记录通过预筛的 pair 数量

2. **Stage 2: LLM 精判**
   - 对通过预筛的 pair，调用 LLM 判断是否语义等价
   - Prompt: "Given two cognition signatures, determine if they express the same rule/insight (semantically equivalent). Output: {equivalent: true/false, confidence: 0.0-1.0, reasoning: '...'}"

3. **结果统计**

### 5.2 指标

| 指标 | 定义 | 目标 |
|------|------|------|
| Stage 1 召回 | 正样本通过预筛的比例 | ≥95% (宽进) |
| Stage 1 精度 | 通过预筛中正样本的比例 | ≥30% (预筛精度低可接受) |
| Stage 2 准确率 | LLM 精判的正确率 | ≥90% |
| 整体召回 | 两阶段后正样本被正确匹配的比例 | ≥85% |
| 整体精度 | 两阶段后被判定为匹配的正确率 | ≥90% |
| LLM 调用次数 | Stage 2 需要的 LLM 调用数 | 越少越好 |

### 5.3 Stage 2 LLM Prompt

```
You are a semantic equivalence judge for cognition signatures.

Given two cognition signatures, determine if they express the SAME underlying rule/insight/lesson (semantically equivalent), even if worded differently.

Consider:
- Same core_action? (If different core_action, likely not equivalent)
- Same underlying judgment? (Different surface details are OK)
- Would acting on one necessarily mean acting on the other?

Signature A: {canonical_text_a} (core_action: {core_action_a})
Signature B: {canonical_text_b} (core_action: {core_action_b})

Output JSON: {"equivalent": true/false, "confidence": 0.0-1.0, "reasoning": "one sentence"}
```

---

## 六、实验 D：Fast Path Keyword 优化（可选）

### 6.1 目标

P1 fast path 覆盖率 35.3%，目标提升至 ~50%。不阻塞 P2 核心（A/B/C），有余力时做。

### 6.2 方法

1. 分析 P1 中 10 个走 LLM 的 pattern 的 keyword 特征
2. 对 11 个缺 keyword 的 core_action 值，提取特征词
3. 加入新 keyword 后重跑 fast path，检查 false positive

### 6.3 风险

更多 keyword → 更多 multi-match → 更多 LLM fallback。需要平衡覆盖率和精度。

---

## 七、实施计划

### 7.1 数据准备

1. 从 PoC 原始数据提取 17 pattern × 2 instance = 34 条认知的 text + payload
2. 构造 ~20 对负样本
3. 整理为 JSON 格式输入文件

### 7.2 执行顺序

| 步骤 | 实验 | 依赖 | 预计耗时 |
|------|------|------|---------|
| 1 | 数据准备 | PoC 原始数据 | 30min |
| 2 | 实验 A: LLM 分类 | Step 1 | 30min (34次调用) |
| 3 | 实验 B: Embedding 相似度 | Step 2 (用A的canonical_text) | 20min |
| 4 | 实验 C: L3 两阶段匹配 | Step 3 | 20min |
| 5 | 实验 D: Fast path 优化（可选）| Step 2 分析 | 15min |
| 6 | 报告撰写 | All | 20min |

### 7.3 成功标准

P2 通过条件（全部满足）：
1. 实验 A: LLM 分类一致率 ≥ 90%
2. 实验 C: 整体召回 ≥ 85%, 精度 ≥ 90%
3. 实验 B: 确定 L3 最优阈值（无论是否等于 0.80）

如果 A 失败（<90%）：分析错误模式 → prompt 优化 → 重跑
如果 C 召回失败（<85%）：降低阈值或改进 canonical_text 规范化
如果 C 精度失败（<90%）：改进 Stage 2 LLM prompt

---

## 八、产出物

1. `p2_embedding_experiment_report_v0.1.md` — 完整实验报告
2. `p2_test_data.json` — 测试数据集（34 条认知 + 20 对负样本）
3. `p2_llm_classification_results.json` — 实验 A 结果
4. `p2_embedding_similarity_results.json` — 实验 B 结果
5. 更新 `mvp_unified_pipeline_spec_v0.1.md` 中的阈值参数

---

## 九、风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| PoC 原始数据不完整 | 无法提取 34 条认知 | 从认知库按 chunk_id 反查 |
| Embedding 模型质量差 | L3 分离度不足 | 备选方案：多模型对比 |
| LLM 调用延迟/成本 | 34+次调用耗时 | 批量调用或并行 |
| 样本量小(17对) | 统计显著性不足 | 标注为初步验证，Phase 3 扩大样本 |

