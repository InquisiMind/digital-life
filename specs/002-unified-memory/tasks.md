---
description: "Dependency-ordered implementation tasks for unified memory (feature 002)"
---

# Tasks: 统一记忆体系（Unified Memory, Feature 002）

**Input**: `specs/002-unified-memory/spec.md` and `plan.md`
**Design**: `docs/design/unified-memory.md`

## Format

Use `- [ ] T001 [P?] [US?] Action with exact path and observable completion`.
Mark `[P]` only when tasks touch different files and have no dependency.

## Phase 1: Confirm Scope and Safety

- [x] T001 Re-read `specs/002-unified-memory/spec.md`, `plan.md`, `.specify/memory/constitution.md`, and `docs/design/unified-memory.md`; confirm P1 scope boundaries (对话三库合并 / markdown 入库 are explicitly excluded per §Assumptions)
- [x] T002 Confirm unrelated user changes (`README.md`, `docs/blog/digital-life-overview.md`, `articles/` per `git status`) remain untouched;record affected contracts in `specs/002-unified-memory/contracts/contracts.md` review (insertion-point table, sense-tool table)
- [x] T003 Confirm verification commands per `specs/002-unified-memory/quickstart.md` and acceptance evidence per `plan.md §Verification Plan`
- [x] T004 Load `docs/development/python-coding-standards.md` and `docs/development/python-testing-and-review.md`; declare `Spec Kit Mode: full` already in effect

## Phase 2: Tests and Contract Guards (P1-focused; revisit per phase)

- [x] T005 [P] Add test `tests/domain/memory/recall/vector/test_embed_texts.py::test_embed_texts_partial_success_keeps_non_null` mocking 3-text batch returning 2/3 embeddings; assert returned list has 3 items with one None preserved (current code returns None — driving bug)
- [x] T006 [P] Add test `tests/domain/memory/recall/vector/test_embed_texts.py::test_embed_texts_429_no_retry_logger_warning` mocking urlopen to raise HTTP 429; assert function returns partial/None without retry attempt (track call count=1) and a WARNING is logged (not DEBUG)
- [x] T007 [P] Add test `tests/domain/memory/summaries/test_segment_indexing.py::test_segment_narrative_is_indexed_after_consolidate` using in-memory SQLite + fake session; assert after `consolidate_after_session` a row with `source='digest_segment'` exists in `chunks` table (currently absent — bug)
- [x] T008 [P] Add test `tests/domain/memory/summaries/test_update_entity_from_narrative.py::test_extract_entities_then_sync_no_import_error` driving `update_entity_index_from_narrative("和 ZHP 聊了 A+ 策略")` against empty fixture index; assert no exception AND "ZHP" appears in `entity_index.json` (currently silent ImportError)
- [x] T009 [P] Add test `tests/domain/memory/recall/vector/test_chunks_phase_migration.py::test_alter_phase_idempotent` running `_get_db` twice on existing memory_vectors.db; assert second open succeeds without `duplicate column name` error and `phase` column exists

## Phase 3: Implementation by User Scenario (P1 = US1)

### User Story 1 - 缺口补丁：让现有记忆管线不再丢数据 (Priority: P1)

**Independent verification**: `bash specs/002-unified-memory/quickstart.md` P1 section (embed partial / segment indexed / entity synced / eval baseline not regressed)

- [x] T010 [P] [US1] Rewrite `_embed_texts` in `domain/memory/memory/recall/vector/__init__.py:110-141`: drop all-or-nothing `if all(...)` at L138, return list preserving successful embeddings and None for failures (keep `Optional[List[...]]` return type — when ALL failed and key present, may still return list-of-Nones; callers ALREADY guard `if emb is None: continue` per-recon); raise log level from `debug` to `warning` on exception; no retry
- [x] T010a [US1] Re-audit `_embed_single` (vector L144-146) after T010 so its `result[0] if result else None` correctly returns None when `_embed_texts(["x"])` returns `[None]` (truthy list); add explicit `return result[0] if result and result[0] is not None else None`
- [x] T011 [P] [US1] Reduce single HTTP timeout in `domain/memory/memory/recall/vector/__init__.py:132` from `timeout=30` to `timeout=8` (preserve budget for overall 5s recall ceiling to land in P2)
- [x] T012 [P] [US1] Add `phase` column migration in `domain/memory/memory/recall/vector/__init__.py` `_get_db()` after the `CREATE TABLE` executescript (try/except `sqlite3.OperationalError` "duplicate column name", mirror consolidation_runtime `_get_db` L158-166 idempotent-ALTER idiom); DEFAULT `''`; no data write yet
- [x] T013 [US1] Backfill `phase` lazily in `domain/memory/memory/recall/vector/__init__.py`: when indexing a chunk whose `phase=''`, derive from `source` per mapping in `data-model.md §相位映射` (e.g. `rules|lessons|self_knowledge|knowledge` → `cognition`, else `experience`); change the relevant chunks INSERTs to include `phase` column. NOTE: existing INSERTs (_index_source L288-292, index_conversations L1627-1631, backfill_conversations L1727-1731) DON'T include phase column today — they'll leave NULL; update them too so backfill actually fires
- [x] T014 [US1] Add `digest_segment` to `_DYNAMIC_SOURCES` in `domain/memory/memory/recall/vector/__init__.py:75-80` with `{weight: 2.0, threshold: 0.10, decay_hours: 168}` matching `digest_session`. **Recon confirmed**: without this entry, `_ALL_SOURCES.get("digest_segment")` returns None at L489-491 of `recall()` → segment chunks are SKIPPED at retrieve time (write is necessary but not sufficient)
- [x] T015 [US1] Call `_index_digest_to_vectors(narrative, "segment", period)` from `_generate_segment_narratives_worker` in `domain/memory/memory/summaries/consolidation_runtime.py:811-819` within the existing `if generated > 0` loop that already iterates segment rows; modify the SELECT to also fetch `period` column (`SELECT llm_summary, period FROM memory_layers WHERE layer='segment' AND period LIKE ?`) and call iteratively per row. Note `_index_digest_to_vectors` internally uses `source = f"digest_{layer}"` = `"digest_segment"` — pairs with T014
- [x] T016 [US1] Update `_index_digest_to_vectors` in `domain/memory/memory/summaries/consolidation_runtime.py:1196-1199` INSERT statement to also write `phase='experience'` to the new chunk row (only this INSERT; other index INSERTs leave phase NULL → filled lazily by T013). P1 pre-embeds P3 field per Clarifications Q1
- [x] T017 [US1] Rewrite `update_entity_index_from_narrative` in `domain/memory/memory/summaries/consolidation_runtime.py:707-726`: remove dead `add_entity` import; iterate `extract_entities_from_context(narrative)` (which returns `list[str]`) and call `sync_entity_from_source(name=entity, entity_type="concept", summary=narrative[:200], aliases=[])` for each; keep try/except narrow per-entity (one bad call should not break others)
- [x] T018 [US1] Audit `tests/` for other tests touching `consolidate_after_session` / `_index_digest_to_vectors` / `update_entity_index_from_narrative` and update expectations if behavior change breaks them
- [x] T019 [US1] Run `python3 scripts/eval_memory_recall.py --output /tmp/recall_p1.json`; compare to `apps/<id>/data/memories/recall_eval_report.json` baseline; record JSON in `specs/002-unified-memory/p1-eval-after.md` (do not regress)
- [x] T020 [US1] Run focused verification: `python3 -m pytest tests/domain/memory/ -v` then `python3 -m pytest tests/ -k "embed or segment or entity_from_narrative" -v`; resolve failures by fixing source, not weakening tests (constitution V)

### User Story 2 - 统一检索面：三路融合、单一 facade (Priority: P2)

**Independent verification**: `python3 -m pytest tests/ -k "unified_recall or fts or rrf or sense_tools_facade" -v` + manual `unified_recall('A+ 策略', attention_tokens=['A+'])` returns non-empty list

- [x] T021 [P] [US2] Create `domain/memory/recall/unified/__init__.py` with `unified_recall()` facade signature per `contracts/contracts.md §1`; placeholder returns empty list — full impl in T023
- [x] T022 [P] [US2] Create `domain/memory/recall/unified/fts.py`: SQLite FTS5 helpers — `ensure_fts5_schema(db)` (probe `PRAGMA compile_options LIKE '%ENABLE_FTS5%'`, fallback gracefully), `tokenize_for_fts(text)` bigram+Latin, `create_chunks_fts_triggers(db)`, `fts_search(query, limit) -> list[tuple[int, float]]` using `bm25(chunks_fts)`
- [x] T023 [US2] Implement `unified_recall()` body in `domain/memory/recall/unified/__init__.py`: three routes (vector via `domain.memory.recall.vector.recall` plumbing; FTS via `unified.fts`; attention via `extract_entities_from_context` + boost), RRF fusion `Σ 1/(60+rank)`, dedup by chunk_id, hard timeout via `concurrent.futures.ThreadPoolExecutor` + `wait(timeout=5.0)` returning partial
- [x] T024 [US2] Implement `_render_breadcrumbs(results, new_entities)` in `domain/memory/recall/unified/__init__.py` per `contracts/contracts.md §1`; preserve existing 🎯/🔍/📅/🔗 tag scheme
- [x] T025 [US2] Refactor `_inject_entity_recall` in `infrastructure/ai/agent.py:1755-1936`: replace Route A + Route B + 30-char dedup with single `unified_recall(combined, attention_tokens=new_entities, exclude_chunk_ids=..., budget_kind="passive")` + `_render_breadcrumbs`; keep `_injected_*` dedup sets and audit dual-write
- [x] T026 [US2] Refactor `_build_memory_context` in `domain/lifecycle/heartbeat.py:198-256`: replace direct `query_entities_ranked` call with `unified_recall(query=context, attention_tokens=entities, budget_kind="passive", max_total_chars=...)`; preserve `get_presented_memory_ids()` semantics
- [x] T027 [P] [US2] Update three sense tools in `interfaces/tools/sense_tools.py`: `recall_memory` L1023-1062, `recall_entity` L1373-1436, `sense_entity` L1067-1124 — replace internal `recall_memories`/`query_entities_ranked` calls with `unified_recall` facade; tool signatures unchanged
- [x] T028 [P] [US2] Update `application/console/monitor.py` 6 chunks-query sites (L1053, L1066, L1094, L1096, L1111, L1233) if any reference new fields; mostly text/source only → verify phase column addition doesn't break existing SELECT * (it shouldn't)
- [x] T029 [US2] Extend `scripts/eval_memory_recall.py`: add `eval_unified()` to run alongside entity/vector baselines; mark old baselines deprecated-comparison; do NOT delete old code paths yet (deprecation comment only)
- [x] T030 [P] [US2] Add 30 manual gold-label cases into `scripts/eval_gold_set.jsonl` covering: 10 cross-semantic (paraphrase), 10 proper-name (person/project), 10 temporal (same-session/adjacent); ensure they DON'T overlap with entity_index auto-derived cases
- [x] T031 [US2] Run focused verification: `python3 -m pytest tests/ -k "unified_recall or fts or sense_tools_facade or rrf or attention" -v`; assert SC-004 (unified recall ≥ max of single routes on gold set); run `python3 scripts/eval_memory_recall.py` and capture new baseline `recall_p2.json`

### User Story 3 - 切片层重构：写时异构、读时同构 (Priority: P3)

**Independent verification**: `python3 -m pytest tests/ -k "slice or schema_migration or slice_dynamics" -v` + monitor page shows phase/source_kind/authority fields

- [x] T032 [P] [US3] Create `domain/memory/recall/unified/slice.py` defining `Slice` dataclass per `data-model.md §切片原子`; include `to_row() / from_row()` for chunks table interop
- [x] T033 [US3] Implement full schema migration in `domain/memory/memory/recall/vector/__init__.py` DB init: add columns per `contracts/contracts.md §3 P3 schema` (authority/permanence/freshness/activation/verification/evidence_count/challenge_count/cognition_state/supersede_by/source_kind/session_id/segment_index/derived_from/derive_kind/entity_links/attention_tokens/provenance); all idempotent with DEFAULT values; add indexes `idx_chunks_session`, `idx_chunks_phase`
- [x] T034 [US3] Backfill new columns by `source` per `data-model.md §相位映射` (authority/permanence/source_kind/phase); UPDATE statements idempotent (`WHERE col IS NULL OR col = ''`)
- [x] T035 [US3] Implement FTS5 schema creation + triggers in `domain/memory/recall/unified/fts.py` `ensure_fts5_schema(db)` (deferred from T022); wrap in try/except for missing FTS5 compile option → log warning + set module flag to never use FTS (degrade to existing TF-cosine in `domain/memory/memory/recall/__init__.py`)
- [x] T036 [US3] Implement `update_slice_dynamics(slice, signals, now)` in `domain/memory/recall/unified/slice.py`: handle Δt freshness decay by permanence; access/refers to activation bumps; co-recalled to spread-activation weight; archive threshold check (`freshness < floor * permanence`). Per `plan.md` "自动驱动器"半边. Replace ad-hoc decay in vector_recall and elsewhere with calls to this single function (P3 partial; full replacement in P4 cleanup)
- [x] T037 [US3] Refactor `_index_digest_to_vectors` / `ensure_indexed` / `index_conversations` / `sync_entity_from_source` to all write full `Slice` rows (phase + authority + time_meta + entity_links pre-filled when available); accept new `Slice`-typed input where it simplifies the call site
- [x] T038 [US3] Implement `register_normalizer(name, fn, defaults)` registry in `domain/memory/recall/unified/slice.py` enabling new memory sources (project/todo/profile) to be added with `Slice` + baseline values only; per FR-303
- [x] T039 [P] [US3] Add project/todo normalizers (callable from scheduler consolidation): for each project.yaml emit a cognition slice (body = name+description+manager), for each todo emit experience slice (body = title+detail); call via new lightweight hook in `domain/lifecycle/scheduler.py` low-traffic checkpoint (mirror `consolidate_after_session` cadence)
- [x] T040 [US3] Add time-meta + edge materialization: when writing a Slice, populate `session_id`/`segment_index` from context; populate `derived_from` for cognition slices (bump on every promote/cluster in P4); content_similarity edges continue to use chunks.embedding (no change)
- [x] T041 [P] [US3] Update `application/console/monitor.py` to display new slice fields (phase/source_kind/authority/permanence/freshness/cognition_state) in chunks detail view; only add columns to SELECT, no business change
- [x] T042 [US3] Run focused verification: `python3 -m pytest tests/ -k "slice or schema_migration or slice_dynamics or backfill or register_normalizer" -v`; assert SC-007 (single function family for dynamics, static scan); then run full `tests/` for cross-layer safety (constitution V)

### User Story 4 - 认知演化生命周期：会成长、不偏执 (Priority: P4)

**Independent verification**: `python3 -m pytest tests/ -k "cognition_state_machine or supersede_chain or access_does_not_reinforce or cluster_born" -v`

- [x] T043 [P] [US4] Create `domain/memory/recall/unified/cognition.py` with `CognitionState` enum (nascent/active/reinforced/revising/challenged/archived/replaced/higher); define state transition map per `data-model.md §认知状态机`
- [x] T044 [US4] Implement `promote(experience_slice, entity, model_summary) -> cognition_slice`: phase跃迁 + reset authority/permanence to cognition baseline + `derived_from += [experience_slice.id]` + set `cognition_state='nascent'` then auto-advance to `active`; narrow try/except (one fail should not corrupt chain)
- [x] T045 [US4] Implement three 铁律 in `cognition.py`: `on_access(slice)` only bumps `activation`, never authority/verification (SC-008); `on_reference(slice)` for experience bumps authority +ε and verification +0.5, for cognition only bumps activation+verification+0.5 (no authority); `verified/falsified` apply only when `slice.phase == 'cognition'`
- [x] T046 [US4] Implement `supersede(old_slice, new_slice)`: old.supersede_by=new.id + old.freshness=0 + old.cognition_state='replaced'; new.derived_from append old.id; both rows preserved (永不硬删, FR-003); chain回看测试
- [x] T047 [US4] Implement `cluster_born(cognitions_cluster, model_summary) -> higher_slice`: derive timeless cognition from cluster; if model summary fails, write a placeholder slice with `cognition_state='revising'` and mark `challenge_count += 1` for later hygiene pass (FR-406)
- [x] T048 [US4] Implement health-遗忘 `visibility_decay(evidence_count, challenge_count, delta_t) -> multiplier` in `cognition.py`; applied in `unified_recall()` scoring as a multiplicative weight (not authority change — SC-005 distinction)
- [x] T049 [US4] Extend memory_hygiene skill (`shared/skills/memory_hygiene/SKILL.md` or equivalent) to invoke `promote()` on session-recurring entity + `cluster_born()` on detected clusters; on failure: log + keep originals + mark待复审 (FR-406)
- [x] T050 [US4] Run focused verification: `python3 -m pytest tests/ -k "cognition_state_machine or supersede_chain or access_does_not_reinforce or cluster_born or visibility_decay or memory_hygiene_promote" -v`; full `tests/` run after
- [x] T051 [US4] Reuse existing eval baseline + add P4-specific probes: in 100-round access stress test, assert cognition authority delta ≈ 0 (SC-008); assert `derived_from`/`supersede_by` integrity after multi-level supersede (SC-010)

## Phase 4: Documentation and Delivery

- [x] T052 [P] Update `docs/design/unified-memory.md` to reflect implementation-phase deviations (likely: specifics of FTS5 tokenizer, the 8s vs 5s timeout split, RRF k=60 choice rationale)
- [x] T053 Update `docs/architecture/current-system.md` §记忆 system section: add "统一检索 facade (`unified_recall`)" + "FTS5 词法兜底" + "切片原子 + 参数引擎" + "认知状态机" subsections per in-scope phase
- [x] T054 [P] Update `docs/operations/memory-maintenance.md`: new section "记忆参数调优与降级诊断" — how to view phase counts, how to detect embedding-429 fallback (warning log), how to rebuild FTS index, how to clear activation raw value
- [x] T055 For each phase delivered: run `python3 -m pytest tests/` full suite + any available `ruff` / `mypy` checks; record unavailable checks as "unverified" per constitution IX
- [x] T056 Run boundary and contract checks (especially after P2/P3 cross-layer changes); constitution compliance re-check before any phase merge
- [x] T057 Re-check acceptance evidence for each SC in spec.md — SC-001 (API-down non-empty), SC-002 (eval not regressed), SC-003 (warn log), SC-004 (unified ≥ max(routes)), SC-007 (single dynamics family), SC-008 (no self-reinforcement), SC-009 (supersede chain preserved), SC-010 (multi-level traceable)
- [x] T058 Decide spec/plan retention: this feature is high-doc-value (describes architecture not just a feature) → retain `plan.md`, `data-model.md`, `contracts/contracts.md`, `research.md` indefinitely; mark `checklists/requirements.md` as completed but keep as audit trail
- [x] T059 Mark `specs/002-unified-memory/spec.md` Status from `Draft` to `Implemented (per phase) <YYYY-MM-DD>` as each phase merges
- [x] T060 Produce delivery report per phase: scope, verification evidence, constitution exceptions, doc updates, artifact decision, remaining risk (per `spec-kit-policy.md §Completion Record`)

## Dependencies

- Section 1 (Safety) → ALL implementation.
- T005–T009 (Tests) may run **parallel** in P1; P1 implementation T010–T017 may interleave with tests but should pass them (TDD optional).
- P1 must complete + T019 (eval not regressed) before P2 starts (P2 needs corrected baselines).
- P2 tasks: T021–T022 parallel; T023 depends on T021+T022; T025/T026 parallel; T027/T028 parallel; T029 standalone.
- P3 tasks: T032/T035/T041 parallel; T033 before T034 (schema then backfill); T036 before T037.
- P4 tasks: most parallel except T044→T046 (promote precedes supersede semantics), T047 depends on T043.
- P2/P3/P4 each independently deliverable; P1 is the only true sequential prerequisite (corrects the baseline).

## Parallelizable groups (highlights)

- **P1 same-file independent fixes**: T010 (embed), T014 (`_DYNAMIC_SOURCES` dict), T017 (`update_entity_index_from_narrative` rewrite) — three different methods on two files; can land as separate commits in one phase.
- **P2 retrieval primitives**: T021 (facade skeleton), T022 (FTS primitives), T028 (monitor), T030 (gold set) all touch separate files — parallel.
- **P3 schema vs normalizer**: T032 (dataclass), T033 (schema), T038 (registry), T041 (monitor UI) parallel.

## MVP scope

**MVP = P1 only** (`User Scenario 1`). Delivers immediate recall quality recovery, no architectural risk, compiles + tests in isolation. Ship P1, run for 1–2 days on real instances to capture new baselines, THEN start P2.
