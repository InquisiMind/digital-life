---
description: "Feature 002 completion record per spec-kit-policy.md"
---

# Feature 002 Delivery Completion Record

(per `spec-kit-policy.md §Completion Record`)

## Selected Mode and Feature Directory

- **Spec Kit Mode**: `full`
- **Feature directory**: `specs/002-unified-memory/`
- **Branch**: `002-unified-memory` (numbered feature branch, 由 `before_specify` git hook 创建)

## Implemented Scope

完整 4 阶段交付 (commits on `002-unified-memory`):
- `e87c9a6f` P1 缺口补丁: embedding partial-success + segment 索引 + add_entity 死代码根除 + phase 预埋
- `8c7f951a` P2 统一检索面: unified_recall facade (RRF + FTS5 + attention) + 5s 硬上限 + 三注入点切面
- `4a670472` P3 main body: Slice dataclass + 25 列 chunks schema + backfill + 参数引擎
- `3f070454` P3 收尾: project/todo normalizer + 时序元 + monitor 字段
- `a1ae4688` P4 认知演化: 状态机 + 三铁律 + supersede 链 + cluster_born + 健康遗忘
- `96524800` FINAL: spec Status → Implemented + FINAL-eval.md

55 task 全勾(P1-P4 implementation + Phase 4 documentation)。剩余非阻塞待办见下。

## Verification Evidence

- **新测试**: `tests/test_memory_embed_texts.py` (3) + `test_memory_segment_indexing.py` (2) +
  `test_memory_update_entity_from_narrative.py` (2) + `test_memory_chunks_phase_migration.py` (2) +
  `test_memory_cognition.py` (9) = **18 单测全过**
- **全仓 pytest**: `python3 -m pytest tests/` → **508 passed / 6 skipped / 2 pre-existing failure**
  (2 个 pre-existing 是 main 分支就有的 prompt 内容匹配脆性测试，与 feature 002 无关，多次验证一致)
- **持久化真实轨迹**:
  `promote_one(经历#3) → 认知#6928 → supersede_one → #6929`
  verify 双向链 + 5×access 不影响 authority (铁律一) — see `p1-eval-after.md` / `FINAL-eval.md`
- **召回评估** `scripts/eval_memory_recall.py c2a5c8e8-*` →
  Entity 57% / Vector 10% / **Unified 66%** / Combined **83%** / Unified MRR **0.563**

## Constitution / Risk Exceptions

无新例外。已存在的 R-1 (schema migration on mutable data) / R-2 (contract sync) /
R-3 (FTS5 platform) / R-4 (doc drift) / R-5 (eval gold pollution) / R-6 (5s timeout)
全部按 plan `Complexity and Risk Tracking` 表处理/mitigation 落实:
- R-1: 幂等 ALTER + 1461 行 backfill 实测无破坏。
- R-2: facade 切 3 个注入点(agent、heartbeat、sense_tools),全 0 回归。
- R-3: macOS 实测 FTS5 ENABLE_FTS5 ✓;detect 已经探测并 fail-safe 兜底。
- R-4: Unified memory 设计文档已回填实现期偏差(FTS5 分词、k=60 等)。
- R-5: eval 加独立金标集 v1 (10 条,`p2-gold-set.md`)。
- R-6: 整体 5s + 单次 8s 验证生效。

## Documentation Updates

- `docs/design/unified-memory.md` — 加 §附录"实现期落地决策"(A.1-A.6)
- `docs/architecture/current-system.md` — §domain/memory/ 加 unified 子模块描述
- `docs/operations/memory-maintenance.md` — 加 §"统一记忆层运维"(看/调/救/接 cognition API)
- `specs/002-unified-memory/` — 全套保留(spec / plan / tasks / research / data-model /
  contracts / quickstart / p1-eval-after / p2-eval-after / p2-gold-set / FINAL-eval / 本 record)

## Feature Artifact Retention Decision

**retention: 全保留**
- feature 002 题材是核心架构(不是单点需求),plan/data-model/contracts/research 有
  持久契约价值和长期参考价值
- checklists/requirements.md 完成后保留作 audit trail
- 不存在 stale / temporary / duplicated artifacts 需清理

## Remaining Risks

1. **push 待网络**:本地所有 commit 都已就位,github push 受 75s 网络超时阻塞
   — 网络恢复后执行 `git push github 002-unified-memory` 一次性推送 6 个 commit
2. **memory_hygiene SKILL.md 未接 cognition API**:模型驱动半边尚未在 skill prompt 里告诉模型
   "何时调 promote_one / supersede_one / cluster_born_persist",只能手工触发
   — 留给下一迭代(改 skill SKILL.md + 加 action_tool wrapper)
3. **金标集仅 10 条 v1**:eval 真实质量评估需扩到 30+ 跨语义/专名/时序三类
4. **token 预算未实测**:`resident=300 / passive=600 / on_demand=1500` 是初版保守值,
   等 mid-session 实跑后定档

以上均不阻塞 feature 合并/上线。
