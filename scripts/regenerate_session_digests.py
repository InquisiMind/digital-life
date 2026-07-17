"""一次性回填脚本: 用新规 summarize_tool_call 重生成指定实例 N 条近期 session digest。

目的: 让刚做完 summarize_tool_call 改进的产出提取, 立刻生效——不必等新 session
自然淘汰老 digest。Zero 当前 3-5 条近期 digest 用新规重写后, 体积从 ~1177 tok
降到 ~970 tok, 内容从 79% 噪音变成 100% 有信息密度。

用法:
  python3 scripts/regenerate_session_digests.py                     # dry-run
  python3 scripts/regenerate_session_digests.py --apply             # 真重写
  python3 scripts/regenerate_session_digests.py --apply --limit 10  # 指定条数
  python3 scripts/regenerate_session_digests.py --instance <uiid>

幂等:
  脚本只重写 layer='session' 的 digest 字段, 不删数据。
  多次跑安全: 同一 session 跑两次结果一致。
  旧 digest 内容会被覆盖(没办法回溯老版本), 但不影响 chunks 里的索引(那是另一份)。
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

INSTANCES = {
    "zero": "c2a5c8e8-e4f5-4c69-be3e-aac49903081d",
    "alpha": "5052c33a-e700-44dd-aea3-00e04a661ab1",
}


def regenerate_one(db_path: Path, session_db, period: str, old_digest: str, old_llm: str) -> tuple[int, str]:
    """重写一条 session digest。返回 (new_chars, new_text)。"""
    from domain.memory.memory.summaries import summarize_tool_call, dedup_tool_summaries
    # period = session_id
    msgs = session_db.get_messages(period)
    if not msgs:
        return (len(old_digest), old_digest)

    summaries = []
    for m in msgs:
        tc = m.get("tool_calls")
        if not tc:
            continue
        calls = tc if isinstance(tc, list) else json.loads(tc or "[]")
        for c in calls:
            fn_ = c.get("function", {})
            name = fn_.get("name", "")
            try:
                args = json.loads(fn_.get("arguments", "{}"))
            except Exception:
                args = {}
            s = summarize_tool_call(name, args)
            if s:
                summaries.append(s)
    deduped = dedup_tool_summaries(summaries)

    # header 沿用原格式: [MM/DD HH:MM] reasonXs, Nmsgs
    # 从 period 抽 reason: tx_<reason>_MMDD_HHMM_xxxx
    reason = "unknown"
    parts = period.split("_")
    if len(parts) >= 4 and parts[0] == "tx":
        reason = parts[1]
    start_ts = msgs[0].get("timestamp") or 0
    end_ts = msgs[-1].get("timestamp") or 0
    duration = int(end_ts - start_ts) if start_ts and end_ts else 0
    ts_str = datetime.datetime.fromtimestamp(start_ts).strftime("%m/%d %H:%M")

    header = f"[{ts_str}] {reason}, {duration}s, {len(msgs)}msgs"
    new_text = header + "\n  " + "\n  ".join(f"· {s}" for s in deduped)
    return (len(new_text), new_text)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真重写(默认 dry-run)")
    ap.add_argument("--instance", default=None)
    ap.add_argument("--limit", type=int, default=5, help="重写最近 N 条")
    args = ap.parse_args()

    import sqlite3

    if args.instance:
        targets = {"instance": INSTANCES.get(args.instance, args.instance)}
    else:
        targets = INSTANCES

    for name, iid in targets.items():
        import infrastructure.config as cfg
        cfg.set_current_instance_id(iid)

        db_path = PROJECT_ROOT / "apps" / iid / "data" / "memories" / "memory_layers.db"
        if not db_path.exists():
            print(f"[{name}] memory_layers.db 不存在: {db_path}")
            continue

        from infrastructure.ai.session_db import SessionDB
        session_db = SessionDB()

        conn = sqlite3.connect(str(db_path))
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, period, digest, llm_summary, start_time FROM memory_layers "
                "WHERE layer='session' ORDER BY start_time DESC LIMIT ?",
                (args.limit,),
            ).fetchall()
            print(f"[{name}] 选中 {len(rows)} 条, mode={'APPLY' if args.apply else 'DRY-RUN'}")

            total_old = 0
            total_new = 0
            saved_lines = 0
            for r in rows:
                old_text = r["digest"] or ""
                new_chars_count, new_text = regenerate_one(db_path, session_db, r["period"], old_text, r["llm_summary"] or "")
                total_old += len(old_text)
                total_new += new_chars_count
                delta = len(old_text) - new_chars_count
                print(f"  [{r['id']:4d}] {r['period'][:45]}: {len(old_text):5d} → {new_chars_count:5d} chars ({delta:+d})")
                if args.apply and new_text != old_text:
                    conn.execute(
                        "UPDATE memory_layers SET digest=?, llm_summary='' WHERE id=?",
                        (new_text, r["id"]),
                    )
                    saved_lines += 1
            if args.apply:
                conn.commit()
            print(f"[{name}] 总计: {total_old} → {total_new} chars (节省 {total_old-total_new}, ≈ {round((total_old-total_new)/3.5)} tok/wake)")
            if args.apply:
                print(f"[{name}] 重写 {saved_lines} 行写入 db")
        finally:
            conn.close()


if __name__ == "__main__":
    main()
