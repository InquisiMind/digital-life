#!/usr/bin/env python3
"""Feature 002 统一记忆体系 — 端到端单脚本验证。

7 大可观察行为一次跑完(用真实实例数据, 不是 mock):
  1. unified_recall 三路融合(向量 + 词法 + attention)
  2. 新记忆源(project normalizer)接入
  3. FTS5 词法在 API 故障时兜底
  4. 参数演化(铁律一: access 不强化)
  5. 认知晋升(promote + 召回)
  6. 取代链(supersede + 双向链完整)
  7. 健康遗忘(visibility_decay)

用法: python3 scripts/e2e_memory_verify.py [instance_id]
默认 instance_id = c2a5c8e8-e4f5-4c69-be3e-aac49903081d
"""
from __future__ import annotations

import os
import sys
import time
import json
from pathlib import Path

INSTANCE_ID = sys.argv[1] if len(sys.argv) > 1 else "c2a5c8e8-e4f5-4c69-be3e-aac49903081d"
os.environ["DIGITAL_LIFE_INSTANCE_ID"] = INSTANCE_ID

import logging
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")


def banner(title: str, ok: bool | None = None) -> bool:
    emoji = "✓" if ok else "✗" if ok is False else "□"
    print(f"\n{emoji}  {title}")
    return ok if ok is not None else True


def step(n: int, msg: str) -> None:
    print(f"   {n}. {msg}")


PASSED: list[str] = []
FAILED: list[str] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(name)
    flag = "✓ PASS" if ok else "✗ FAIL"
    suffix = f" — {detail}" if detail else ""
    print(f"      [{flag}] {name}{suffix}")


# ───────────── 1. unified_recall 三路融合 ─────────────

def test_unified_recall_three_routes() -> None:
    banner("1. unified_recall 三路融合(向量+词法+attention)")
    step(1, "调 unified_recall('A+ 策略')")
    from domain.memory.memory.recall.unified import unified_recall
    from domain.memory.memory.recall.vector import _get_db_path
    import sqlite3
    results = unified_recall("A+ 策略效果", attention_tokens=["A+", "策略"],
                             budget_kind="passive")
    record("unified_recall 返回非空结果", len(results) > 0,
           f"得到 {len(results)} 条切片")
    step(2, "看 chunk_id 是否在 chunks 表中真实存在")
    if results:
        # 起码有一条带 chunk_id 的(说明 RRF 路由到 vector 或 fts 真实 chunks)
        has_real_id = any(r.get("chunk_id", -1) >= 0 for r in results)
        record("至少 1 条切入到真实 chunks 表", has_real_id,
               f"真实 id 切片数: {sum(1 for r in results if r.get('chunk_id', -1) >= 0)}")


# ───────────── 2. 新记忆源 normalizer 已生效 ─────────────

def test_project_normalizer_indexed() -> None:
    banner("2. 新记忆源(project)接入 — FR-303 可扩展性")
    step(1, "强制 re-index project/todo")
    from domain.memory.memory.recall.unified.normalizers import index_projects_and_todos
    n = index_projects_and_todos()
    record("project/todo 索引至少 1 条", n >= 1, f"本次 re-index {n} 条")
    step(2, "chunks 表 source='project' 应有行")
    from domain.memory.memory.recall.vector import _get_db_path
    import sqlite3
    conn = sqlite3.connect(str(_get_db_path()))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT COUNT(*) as c FROM chunks WHERE source='project'").fetchone()
    conn.close()
    record("project slice 落库", rows["c"] >= 1, f"共 {rows['c']} 行")
    step(3, "unified_recall('数字生命 开发 项目') 能命中 project 来源")
    from domain.memory.memory.recall.unified import unified_recall
    r = unified_recall("数字生命开发 项目 我现在在做", budget_kind="passive")
    hit_project = any(x.get("source") == "project" for x in r)
    record("unified_recall 命中 project 来源", hit_project,
           f"top results source: {[x.get('source') for x in r[:5]]}")


# ───────────── 3. FTS5 兜底(模拟 API 故障) ─────────────

def test_fts5_fallback_when_embedding_fails(monkeypatch: bool = True) -> None:
    banner("3. FTS5 词法在向量 API 故障时兜底 — FR-001 / SC-001")
    step(1, "暂时把 _embed_* 强制返回 None,模拟 429/无 key")
    from domain.memory.memory.recall.vector import _embed_single, _embed_texts
    orig_single = _embed_single
    orig_texts = _embed_texts

    import domain.memory.memory.recall.vector as vec_mod
    vec_mod._embed_single = lambda text: None
    vec_mod._embed_texts = lambda texts: None
    try:
        from domain.memory.memory.recall.unified import unified_recall
        r = unified_recall("A+ 策略 效果 验证", budget_kind="passive")
        # 应该仍能返回(FTS5 + attention 兜底), 但来源无 'vector' 字面
        sources = {x.get("source", "") for x in r}
        record("API 故障时 facade 仍非空", len(r) > 0,
               f"返回 {len(r)} 条,来源 {sources}")
        has_lexical = any(x.get("routes") and "lexical" in x.get("routes", []) for x in r)
        record("命中里有 lexical 路", has_lexical, "说明 FTS5 词法路兜底成功")
    finally:
        vec_mod._embed_single = orig_single
        vec_mod._embed_texts = orig_texts


# ───────────── 4. 参数演化: 铁律一 ─────────────

def test_iron_law_access_no_reinforce() -> None:
    banner("4. 三铁律(一) — on_access 不强化")
    step(1, "取一个认知切片, 100 次 access")
    from domain.memory.memory.recall.vector import _get_db
    db = _get_db()
    row = db.execute(
        "SELECT * FROM chunks WHERE phase='cognition' AND cognition_state IS NULL LIMIT 1"
    ).fetchone()
    db.close()
    if row is None:
        record("找到测试用认知切片", False, "无 cognition chunks 可用")
        return
    from domain.memory.memory.recall.unified.slice import Slice
    from domain.memory.memory.recall.unified.cognition import on_access
    s = Slice.from_row(row)
    auth_before = s.authority
    ver_before = s.verification
    for _ in range(100):
        on_access(s)
    record("100 次 access: authority 不变", s.authority == auth_before,
           f"before={auth_before:.3f} after={s.authority:.3f}")
    record("100 次 access: verification 不变", s.verification == ver_before,
           f"before={ver_before:.3f} after={s.verification:.3f}")
    record("activation 应上升", s.activation > 0.5,
           f"activation={s.activation:.3f}")


# ───────────── 5. 认知晋升(promote + 召回) ─────────────

def test_promote_and_recall() -> None:
    banner("5. 认知晋升(promote_one) — A 路径")
    step(1, "取一条经历切片, 提纯为认知")
    from domain.memory.memory.recall.vector import _get_db
    db = _get_db()
    row = db.execute(
        "SELECT * FROM chunks WHERE phase='experience' AND source='digest_session' LIMIT 1"
    ).fetchone()
    db.close()
    if row is None:
        record("找到测试用经历切片", False, "无 experience chunks")
        return
    from domain.memory.memory.recall.unified.slice import Slice
    exp = Slice.from_row(row)
    step(2, f"调 promote_one(#{exp.id}, summary='test_promote_e2e', entity_name=test_e2e)")
    from domain.memory.memory.recall.unified.cognition_store import promote_one, load_slice_by_id
    res = promote_one(exp.id,
                      summary="【E2E】测试晋升的认知:A+策略在震荡行情中表现受流动性约束",
                      entity_name="e2e_promote_test")
    if not res.get("ok"):
        record("promote_one 成功", False, str(res.get("error")))
        # 清理 fallback
        return
    new_id = res["new_cognition_chunk_id"]
    record("promote_one 产出新认知", new_id is not None, f"新认知 #{new_id}")
    new_cog = load_slice_by_id(new_id)
    record("新认知 phase=cognition", new_cog.phase == "cognition",
           f"phase={new_cog.phase}")
    record("新认知 derived_from 含原经历", exp.id in new_cog.derived_from,
           f"derived_from={new_cog.derived_from}")
    step(3, "用关键词召回, 看新认知是否被命中")
    from domain.memory.memory.recall.unified import unified_recall
    r = unified_recall("A+ 策略 震荡 流动性", budget_kind="passive")
    found = any(x.get("chunk_id") == new_id for x in r)
    record("新认知被 unified_recall 命中", found,
           f"topids: {[x.get('chunk_id') for x in r[:5]]}")

    # 记下 id 让 test 6 复用 / 7 清理
    test_promote_and_recall.new_cog_id = new_id  # type: ignore


# ───────────── 6. 取代链(supersede) ─────────────

def test_supersede_chain() -> None:
    banner("6. 取代链(supersede) + 双向链完整 — SC-009 / SC-010")
    new_cog_id = getattr(test_promote_and_recall, "new_cog_id", None)
    if new_cog_id is None:
        record("前置有新认知", False, "test_promote_and_recall 无 new_cog_id")
        return
    step(1, f"用新 body 取代刚晋升的认知 #{new_cog_id}")
    from domain.memory.memory.recall.unified.cognition_store import (
        supersede_one, load_slice_by_id,
    )
    res = supersede_one(new_cog_id,
                        new_body="【E2E 修订】A+策略只在主力资金净流入>3亿时有效",
                        new_authority=0.9,
                        entity_name="e2e_promote_test")
    if not res.get("ok"):
        record("supersede_one 成功", False, str(res.get("error")))
        return
    old_id = res["old_id"]
    new_id = res["new_id"]
    step(2, f"验证双向链: 老#{old_id} vs 新#{new_id}")
    old = load_slice_by_id(old_id)
    new = load_slice_by_id(new_id)
    record("老认知状态 = replaced", old.cognition_state == "replaced",
           f"state={old.cognition_state}")
    record("老认知 supersede_by = 新 id", old.supersede_by == new_id,
           f"supersede_by={old.supersede_by}")
    record("新认知 derived_from 包含老 id", old_id in new.derived_from,
           f"derived_from={new.derived_from}")
    record("永不硬删: 老 id 仍存在", old is not None, f"老 row #{old.id}在")

    # 清理: 删除测试认知和它们的引用,防止污染真实数据
    from domain.memory.memory.recall.vector import _get_db
    db = _get_db()
    db.execute("DELETE FROM chunks WHERE id IN (?, ?, ?)",
               (old_id, new_id, test_promote_and_recall.new_cog_id))
    db.commit()
    db.close()
    print(f"   [cleanup] 删除 E2E 测试产生的 {old_id}, {new_id}, {new_cog_id} 切片")


# ───────────── 7. 健康遗忘(visibility_decay) ─────────────

def test_visibility_decay_stale_cognition() -> None:
    banner("7. 健康遗忘(visibility_decay) — §6.7")
    step(1, "构造陈旧强认知:evidence=1, challenge=0, activation=0")
    from domain.memory.memory.recall.unified.slice import Slice
    from domain.memory.memory.recall.unified.cognition import visibility_decay
    stale = Slice(id=99999, source="rules", phase="cognition",
                  authority=0.95, verification=5.0,
                  evidence_count=1, challenge_count=0, activation=0.0)
    factor = visibility_decay(stale)
    record("陈旧认知可见性 < 0.6", factor < 0.6, f"factor={factor:.2f}")
    record("陈旧认知 authority 不变(只是掩埋)", stale.authority == 0.95,
           f"authority 仍 {stale.authority}")

    step(2, "充足证据的不埋")
    backed = Slice(id=99998, source="rules", phase="cognition",
                   authority=0.9, verification=5.0,
                   evidence_count=5, challenge_count=0, activation=0.5)
    f2 = visibility_decay(backed)
    record("充足证据认知 factor >= 0.9", f2 >= 0.9, f"factor={f2:.2f}")


# ───────────── main ─────────────

def main() -> int:
    print(f"════════════════════════════════════════════════════════════════")
    print(f"   统一记忆体系 端到端验证 — 实例 {INSTANCE_ID[:8]}")
    print(f"════════════════════════════════════════════════════════════════")

    tests = [
        test_unified_recall_three_routes,
        test_project_normalizer_indexed,
        test_fts5_fallback_when_embedding_fails,
        test_iron_law_access_no_reinforce,
        test_promote_and_recall,
        test_supersede_chain,
        test_visibility_decay_stale_cognition,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            import traceback
            record(f"<{t.__name__} 未捕获异常>", False, f"{type(e).__name__}: {e}")
            traceback.print_exc()

    print(f"\n════════════════════════════════════════════════════════════════")
    print(f"   汇总: {len(PASSED)} passed, {len(FAILED)} failed")
    print(f"════════════════════════════════════════════════════════════════")
    for f in FAILED:
        print(f"  ✗ {f}")
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
