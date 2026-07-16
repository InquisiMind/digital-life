"""Feature 002 — sandbox 实例注入脚本。

把一批"已知答案"的 Slice 注入到专用 sandbox 实例,
然后 golden eval 用这些精确的 chunk_id 做 ground truth 评估。

注入后会自动生成 ground_truth.json,作为 tests/eval_memory_golden.py 的参照。

设计:
- sandbox 实例 id: eval-sandbox-001(在 apps/eval-sandbox-001/ 下)
  注入前清空 chunks 表,从纯净状态开始
- 8 个主题,每个主题包含:
  - 一组相关 Slice(按主题真实分布造经历 / 认知 / 时序连续 / 诞生链)
  - 一组 query + 期望 chunk_id 应/不应 in 召回的 set
- 所有字段都按生产真实数据形态填: phase/source_kind/session_id/derived_from/entity_links
- chunk_hash 用稳定 hash(主题+序号),保证 ground truth id 稳定
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SANDBOX_INSTANCE = "eval-sandbox-001"


def reset_sandbox() -> None:
    """清空 sandbox 实例的 chunks 表(保留 schema)。"""
    os.environ["DIGITAL_LIFE_INSTANCE_ID"] = SANDBOX_INSTANCE
    from domain.memory.memory.recall.vector import _get_db, _get_db_path, _get_mem_dir
    # 确保 schema 就位
    mem_dir = _get_mem_dir()
    mem_dir.mkdir(parents=True, exist_ok=True)
    _get_db().close()
    db = _get_db()
    db.execute("DELETE FROM chunks")
    db.execute("DELETE FROM chunks_fts")
    db.commit()
    # 注意:chunks_fts 是 content=chunks,触发器会自动删,但全 REPLACE 之前可能
    # 留 stale row。rebuild 一次。
    try:
        db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
    except Exception:
        pass
    db.commit()
    db.close()
    print(f"  sandbox reset: {_get_db_path()}")


def _drop_pending(slice_dict: dict, overrides: dict) -> dict:
    """组装一条 Slice dict(field 名按 to_row,缺省走合理值)。"""
    base = {
        "source": "digest_session",   # 默认经历摘要
        "chunk_hash": "",
        "text": "",
        "file_mtime": time.time(),
        "created_at": time.time(),
        "phase": "experience",
        "source_kind": "digest",
        "session_id": "",
        "segment_index": None,
        "derived_from": "[]",
        "derive_kind": "",
        "authority": 0.6,
        "permanence": 0.4,
        "freshness": 1.0,
        "activation": 0.0,
        "verification": 0.0,
        "evidence_count": 0,
        "challenge_count": 0,
        "cognition_state": None,
        "supersede_by": None,
        "entity_links": "[]",
        "attention_tokens": "[]",
        "provenance": "eval-sandbox-001",
    }
    base.update(slice_dict)
    base.update(overrides)
    return base


def insert_slice(db, slc: dict) -> int:
    """插入 chunks,返回生成的 id。
    如有 API key,也填 embedding(让语义路能工作)。失败时静默跳过 — 词法/attention 仍可测。
    """
    # 试算 embedding
    try:
        from domain.memory.memory.recall.vector import _embed_texts, _embedding_to_blob
        embs = _embed_texts([slc.get("text", "")])
        if embs and embs[0] is not None:
            slc["embedding"] = _embedding_to_blob(embs[0])
    except Exception as e:
        print(f"  [warn] embed skipped for {slc['chunk_hash']}: {e}")

    cols = list(slc.keys())
    placeholders = ",".join("?" * len(cols))
    col_list = ",".join(cols)
    cur = db.execute(
        f"INSERT INTO chunks ({col_list}) VALUES ({placeholders})",
        [slc[c] for c in cols],
    )
    return cur.lastrowid


def build_dataset() -> tuple[list[dict], list[dict]]:
    """构造 8 主题数据集。
    返回 (slices_to_insert, golden_queries)。
    golden_queries: {theme, query, must_hit(list[chunk_hash]), may_not_hit, note}
    """
    slices: list[dict] = []
    queries: list[dict] = []
    now = time.time()

    # ─────────────── Theme A — 单条直接语义召回 ───────────────
    slices.append(_drop_pending({
        "source": "digest_session",
        "chunk_hash": "A_worklog",
        "text": "今天完成了产品 A 当天的上线发布。生产环境数据同步测试通过,"
                "用户反馈首版响应略慢,P95 latency 850ms,后续可优化。",
        "session_id": "session_a_publish",
        "authority": 0.7, "permanence": 0.5,
        "entity_links": json.dumps(["产品A", "上线发布", "P95延迟"]),
    }, {}))
    slices.append(_drop_pending({
        "source": "digest_session",
        "chunk_hash": "A_standup",
        "text": "今日 standup 通用同步:团队全员健康,OKR 进度无变化,会议流程无问题。",
        "session_id": "session_a_standup",
        "authority": 0.4, "permanence": 0.2,
        "entity_links": json.dumps(["团队", "OKR"]),
    }, {}))
    queries.append({
        "theme": "A", "query": "上次产品 A 上线效果怎么样",
        "must_hit": ["A_worklog"],
        "may_not_hit": ["A_standup"],
        "note": "精确语义命中经历, 干扰项同为 'A' 开头但内容是 standup",
    })

    # ─────────────── Theme B — 同义词 query 召回 ───────────────
    slices.append(_drop_pending({
        "source": "digest_session",
        "chunk_hash": "B_api_invoke",
        "text": "客户调用 invoke api 时反馈偶发 500,排查根因为网关连接池配置上限过低,"
                "已调高至 1000、复测稳定。",
        "entity_links": json.dumps(["API", "invoke", "网关", "连接池"]),
    }, {}))
    queries.append({
        "theme": "B", "query": "最近有没有调用详情",
        "must_hit": ["B_api_invoke"],
        "may_not_hit": [],
        "note": "query 用 '调用详情'(不出现 invoke 字面) → 验证语义同义词召回",
    })

    # ─────────────── Theme C — 跨主题干扰 / 精度反检验 ───────────────
    slices.append(_drop_pending({
        "source": "digest_session",
        "chunk_hash": "C_real_plan",
        "text": "本季度规划: 1) 统一记忆接口上线,2) 数字孪生 POC,3) 完成 feature 002 验证。",
        "entity_links": json.dumps(["规划", "记忆", "孪生"]),
    }, {}))
    slices.append(_drop_pending({
        "source": "digest_session",
        "chunk_hash": "C_distract_plan",
        "text": "对话中用户提到 '晚餐计划', 计划晚上做番茄炒蛋。",
        "entity_links": json.dumps(["对话", "晚餐"]),
    }, {}))
    queries.append({
        "theme": "C", "query": "我说过的规划是什么",
        "must_hit": ["C_real_plan"],
        "may_not_hit": ["C_distract_plan"],
        "note": "跨主题干扰测试: 'plan' 字面同但语义不同(规划 vs 餐食)",
    })

    # ─────────────── Theme D — 时序链召回 (session 邻居) ───────────────
    sid_D = "session_d_continuous"
    slices.append(_drop_pending({
        "source": "digest_session",
        "chunk_hash": "D_step1",
        "text": "project X 阶段一: 收集了用户访谈 12 份, 整理共性需求 → 待原型设计。",
        "session_id": sid_D, "segment_index": 0,
        "created_at": now - 7200,
    }, {}))
    slices.append(_drop_pending({
        "source": "digest_session",
        "chunk_hash": "D_step2",
        "text": "project X 阶段二: 基于用户访谈输出了 3 个原型, 内部评审通过 → 准备用户测试。",
        "session_id": sid_D, "segment_index": 1,
        "created_at": now - 3600,
    }, {}))
    slices.append(_drop_pending({
        "source": "digest_session",
        "chunk_hash": "D_step3",
        "text": "project X 阶段三: 5 位用户原型测试完成, 3/5 选择 A 方案 → 推进 A 方案。",
        "session_id": sid_D, "segment_index": 2,
        "created_at": now,
    }, {}))
    queries.append({
        "theme": "D", "query": "project X 阶段二 进展",
        "must_hit": ["D_step2"],
        "may_hit_neighbor": ["D_step1", "D_step3"],  # 时序链邻居
        "note": "时序连边测试: 命中第 2 步,应同时召回第 1 / 第 3 步(session 邻居)",
    })

    # ─────────────── Theme E — 诞生链召回 (derived_from) ───────────────
    slices.append(_drop_pending({
        "source": "digest_session",
        "chunk_hash": "E_exp1",
        "text": "经验观察: 周三晚 9 点固定复盘的产出比 ad-hoc 复盘密度高 2 倍。",
        "phase": "experience", "authority": 0.5,
    }, {}))
    slices.append(_drop_pending({
        "source": "digest_session",
        "chunk_hash": "E_exp2",
        "text": "另一观察: 周三 9:00 复盘比周一 9:00 复盘注意力更集中、产出更长。",
        "phase": "experience", "authority": 0.5,
    }, {}))
    # 注意: 同 chunk_hash=E_cognition 的 derived_from 应包含 E_exp1 / E_exp2 的 id
    # id 我们这里 hash 稳定但 raw id 还要等 insert 拿 → 先建 derived_from placeholder
    slices.append(_drop_pending({
        "source": "knowledge",  # cognition
        "chunk_hash": "E_cognition",
        "text": "认知: 周三固定时间(21:00)是复盘最佳的稳定性时刻,应固化为 routine 而非可选。",
        "phase": "cognition", "source_kind": "profile",
        "authority": 0.85, "permanence": 0.9, "cognition_state": "active",
        "derive_kind": "promote",
        # derived_from 稍后由 post-process 填 (需知道 E_exp1 / E_exp2 的 id)
    }, {}))
    queries.append({
        "theme": "E", "query": "周三 9 点复盘",
        "must_hit": ["E_cognition"],
        "may_hit_derived": ["E_exp1", "E_exp2"],
        "note": "诞生链测试: 命中认知 → 应同时召回它的 derived_from 源经历(认知是怎么来的)",
    })

    # ─────────────── Theme F — attention_tokens boost ───────────────
    slices.append(_drop_pending({
        "source": "digest_session",
        "chunk_hash": "F_mira_meeting1",
        "text": "Mira 在产品 sync 上提出: 模块化拆分能让新人更快上手,降低 ramp-up 时间。",
        "entity_links": json.dumps(["Mira", "产品sync"]),
        "attention_tokens": json.dumps(["Mira"]),
    }, {}))
    slices.append(_drop_pending({
        "source": "digest_session",
        "chunk_hash": "F_general_meeting",
        "text": "团队例会讨论下季度 OKR, 各 owner 同步自己进度, 无异常。",
        "entity_links": json.dumps(["OKR"]),
    }, {}))
    queries.append({
        "theme": "F", "query": "Mira 的提案 是什么",
        "must_hit": ["F_mira_meeting1"],
        "may_not_hit": ["F_general_meeting"],
        "note": "attention_tokens=Mira 让含它的 slice 排到 top; 通用的 OKR 例会不应挤进来",
    })

    # ─────────────── Theme G — phase 排序: 认知 > 经历 ───────────────
    slices.append(_drop_pending({
        "source": "digest_session",
        "chunk_hash": "G_exp_commit",
        "text": "经验记录: 在第 14 周 4 次提交代码出现构建失败, 全部因 types 版本不同步。",
        "phase": "experience", "entity_links": json.dumps(["构建失败", "types"]),
    }, {}))
    slices.append(_drop_pending({
        "source": "rules",  # cognition 强基线
        "chunk_hash": "G_rule_versioning",
        "text": "规则: 提交前必须先 sync types 的最新版本,避免构建期类型不匹配失败。",
        "phase": "cognition", "source_kind": "rule",
        "authority": 1.0, "permanence": 0.95, "cognition_state": "active",
        "entity_links": json.dumps(["构建失败", "types"]),
    }, {}))
    queries.append({
        "theme": "G", "query": "构建失败 怎么办的",
        "must_hit": ["G_rule_versioning"],  # 认知应该排第一
        "may_hit_derived": ["G_exp_commit"],
        "note": "认知规则(权威 1.0)应排在经历记录之上, 因为它是结论性建议",
    })

    # ─────────────── Theme H — 精确文本(token)召回 (无 embedding) ───────────────
    slices.append(_drop_pending({
        "source": "digest_session",
        "chunk_hash": "H_specific",
        "text": "客户工单 #PROJ-4532 已 resolve. 根原因: 内部缓存 key 拼写错误。修复 commit: f3a8b9c。",
        "entity_links": json.dumps(["工单", "PROJ-4532"]),
    }, {}))
    queries.append({
        "theme": "H", "query": "PROJ-4532 工单",
        "must_hit": ["H_specific"],
        "note": "精确专有名词 + 数字, 词法路应高度命中(token 精确匹配)",
    })

    return slices, queries


def post_process_derived_from(db: sqlite3_like=None) -> None:
    """E_cognition 的 derived_from 应含 E_exp1 / E_exp2 的真实 id。
    在所有 slice insert 后跑一次, 通过 chunk_hash 反查 id。
    """
    if db is None:
        from domain.memory.memory.recall.vector import _get_db
        db = _get_db()
    try:
        # 拿三条的 id
        ids = {
            "E_exp1": db.execute(
                "SELECT id FROM chunks WHERE chunk_hash='E_exp1'"
            ).fetchone(),
            "E_exp2": db.execute(
                "SELECT id FROM chunks WHERE chunk_hash='E_exp2'"
            ).fetchone(),
            "E_cognition": db.execute(
                "SELECT id FROM chunks WHERE chunk_hash='E_cognition'"
            ).fetchone(),
        }
        if any(v is None for v in ids.values()):
            print("  [warn] E 主题 3 条 slice 缺一, 跳过 derived_from 填充")
            return
        derived_list = [ids["E_exp1"][0], ids["E_exp2"][0]]
        db.execute(
            "UPDATE chunks SET derived_from=? WHERE chunk_hash=?",
            (json.dumps(derived_list), "E_cognition"),
        )
        db.commit()
        print(f"  E_cognition derived_from 填充: {derived_list}")
    finally:
        pass  # caller 负责 close


def inject_and_save_truth() -> dict:
    """主入口: reset + insert + post-process + 把 hash→id 映射存盘作 ground truth。"""
    reset_sandbox()
    from domain.memory.memory.recall.vector import _get_db
    slices, queries = build_dataset()

    db = _get_db()
    hash_to_id: dict[str, int] = {}
    try:
        for slc in slices:
            new_id = insert_slice(db, slc)
            hash_to_id[slc["chunk_hash"]] = new_id
        db.commit()
        post_process_derived_from(db)

        # 触发 backfill(保证 phase / authority 等基线值填齐)
        from domain.memory.memory.recall.unified.migration import (
            backfill_slice_fields_if_needed,
        )
        # 重置内部 cache 强制跑
        import domain.memory.memory.recall.unified.migration as mig_mod
        mig_mod._backfill_done = False
        backfill_slice_fields_if_needed(force=True)
    finally:
        db.close()

    # 不出意外: 我们手设了 phase / authority, backfill 应当不覆盖非空字段。
    # 检查 sanity
    db = _get_db()
    rule_row = db.execute(
        "SELECT phase, authority FROM chunks WHERE chunk_hash='G_rule_versioning'"
    ).fetchone()
    print(f"  sanity G_rule_versioning: phase={rule_row['phase']} authority={rule_row['authority']}")
    db.close()

    # 导出 ground truth: 每个 query 把 hash 翻译成 id
    golden = []
    for q in queries:
        g = {
            "theme": q["theme"],
            "query": q["query"],
            "note": q.get("note", ""),
            "must_hit_id": [hash_to_id[h] for h in q.get("must_hit", []) if h in hash_to_id],
            "may_not_hit_id": [hash_to_id[h] for h in q.get("may_not_hit", []) if h in hash_to_id],
            "may_hit_neighbor_id": [hash_to_id[h] for h in q.get("may_hit_neighbor", []) if h in hash_to_id],
            "may_hit_derived_id": [hash_to_id[h] for h in q.get("may_hit_derived", []) if h in hash_to_id],
        }
        golden.append(g)

    truth_path = Path("scripts") / "eval_golden_truth.json"
    truth_path.write_text(
        json.dumps(golden, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n  ground truth written: {truth_path} ({len(golden)} queries)")
    print(f"  injected {len(slices)} slice rows, hash→id map:")
    for h, i in sorted(hash_to_id.items()):
        print(f"    {h:25s} → #{i}")
    return {"hash_to_id": hash_to_id, "golden": golden}


if __name__ == "__main__":
    inject_and_save_truth()
