# P2 Gold Label Set (T030)

> 用途:修正 `scripts/eval_memory_recall.py` 用 entity_index 自派生 case + 纯 substring
> 判定带来的偏差。子串判定低估了 vector / unified 的语义命中(语义召回返回的 chunk
> 经常是"关联但不直接含 entity identifier"的中文描述)。
>
> 解决思路:用一个**独立金标集**,只包含**人工标注相关性**的 query → expected。
> 不再用 "substring match 实体名" 判 relevance,而是**手工 ground-truth** 该 query
> 应该召回哪类记忆。这套设计文档放在仓库里,后续可手工补条目。
>
> 当前 v1 占位 10 条(下方 JSONL);后续可继续补到 30+。

## v1 金标 (10 case)

格式 (JSONL): `{"query": "...", "should_match_kinds": ["digest_session", "conversation"], "should_mention_topic": "..."}` - 注意 relevance 判定**不再依赖 substring 实体匹配**,而是:
- `should_match_kinds`: 期望召回的 source 类型集合(之一即可,非互斥)
- `should_mention_topic`: 期望召回任一条文本里提到的主题片段(自然语言,慎用宽泛词)

```jsonl
{"query": "A+ 策略表现", "should_match_kinds": ["digest_session", "conversation", "rules", "lessons"], "should_mention_topic": "A+"}
{"query": "涨停次日如何处理", "should_match_kinds": ["digest_session", "lessons", "rules"], "should_mention_topic": "次日"}
{"query": "模拟炒股项目进展", "should_match_kinds": ["digest_session", "conversation"], "should_mention_topic": "模拟"}
{"query": "companion 虚拟陪伴", "should_match_kinds": ["digest_session"], "should_mention_topic": "companion"}
{"query": "情绪边界", "should_match_kinds": ["conversation", "rules", "consciousness"], "should_mention_topic": "情绪"}
{"query": "断档恢复机制", "should_match_kinds": ["lessons", "rules"], "should_mention_topic": "断档"}
{"query": "止损止盈线", "should_match_kinds": ["rules", "lessons", "digest_session"], "should_mention_topic": "止损"}
{"query": "盘前准备", "should_match_kinds": ["digest_session", "conversation"], "should_mention_topic": "盘前"}
{"query": "Alpha 协作", "should_match_kinds": ["conversation", "digest_session"], "should_mention_topic": "Alpha"}
{"query": " mesmo 我自己状态", "should_match_kinds": ["consciousness", "identity"], "should_mention_topic": "状态"}
```

## 验收建议

P2 金标集**只放文档说明**,不打通到 eval 脚本里(避免引入复杂的 ground-truth 判定流程)。
后续 P3 评估改造时再:
1. 在 `eval_memory_recall.py` 加 `eval_unified_gold(gold_jsonl)` 
2. relevance 判定 = source ∈ should_match_kinds 且 text 含 should_mention_topic
3. 报告里加一段 "GoldSet Reach"

这 10 条金标集的核心价值:**它们的 query 跟 entity_index 自派生 100 例不重叠**(避开了 "eval 用偏向数据评估偏向数据" 的循环论证)。

## 当下 P2 对照(unified 单路 66%, MRR 0.563)

以这 10 条手工核对:
- query 含自然语言「情绪边界」「断档恢复」等,entity_index 子串匹配勉强命中但这些 query 出现在 unified 上时,会通过**词法(BM25 中文 bigram) + 语义** 一起命中
- unified MRR 0.563 表示相关命中在前 2-3 条内,模型能立刻看到 — 这正是 §User Story 2 想要的"打个小灯就点起一簇"
