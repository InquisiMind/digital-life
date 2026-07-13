"""One-shot backfill: rewrite TEXT ISO time columns to UTC `+00:00`.

After ``domain/lifecycle/clock.now_iso()`` switched to UTC, legacy rows persisted
in Beijing ISO (`+08:00`) would mix with new UTC writes and break lexicographic
SQL filters. This script scans every instance ``state.db`` (plus shared DBs),
normalizes every known TEXT timestamp column to UTC, and rewrites embedded ISO
values inside JSON payload columns.

Format invariants post-migration:
  - All values match ``YYYY-MM-DDTHH:MM:SS+00:00``.
  - Naive values (no tz suffix) are assumed Beijing local then converted.
  - SQLite ``datetime('now')`` strings (`YYYY-MM-DD HH:MM:SS`) — also assumed
    Beijing local on the host.
  - REAL/INTEGER epoch columns are skipped (offset-agnostic).
  - `'Z'` accepted as alias for `+00:00`.

Usage:
  python3 scripts/migrate_timestamps_to_utc.py --dry-run
  python3 scripts/migrate_timestamps_to_utc.py --apply
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Iterable

# Importing project clock; rely on repo root being sys.path[0] when run as script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from domain.lifecycle.clock import BEIJING, UTC, parse_iso  # noqa: E402


ISO_RE = re.compile(
    r"\b(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)\b"
)


def normalize(value: str) -> Optional[str]:
    """Return UTC ISO normalized to seconds; None if value is empty / unparseable."""
    if not value or not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    # Accept SQLite `YYYY-MM-DD HH:MM:SS` (no tz, no T) by normalizing space→T.
    sqlitish = re.match(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})$", raw)
    if sqlitish:
        raw = f"{sqlitish.group(1)}T{sqlitish.group(2)}"
    try:
        dt = parse_iso(raw)
    except Exception:
        return None
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def scan_columns(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Return [(table_name, column_name)] for TEXT timestamp-like columns.

    Heuristic on column name + declared type.
    """
    candidates: list[tuple[str, str]] = []
    name_re = re.compile(
        r"^(created_at|updated_at|started_at|ended_at|completed_at|fired_at|"
        r"fire_at|consumed_at|triggered_at|injected_at|blocked_at|resume_when|"
        r"set_at|deadline|last_nurture|said_at|at|since|until|occurred_at|"
        r"run_at|stopped_at)$"
    )
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    for (table,) in rows:
        try:
            cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
        except sqlite3.Error:
            continue
        for _, col_name, col_type, *_ in cols:
            col_type_u = (col_type or "").upper()
            if col_type_u not in ("TEXT", ""):  # treat empty declared type as text-friendly
                continue
            if name_re.match(col_name):
                candidates.append((table, col_name))
    return candidates


def normalize_payload_json_columns(
    conn: sqlite3.Connection, table: str, id_cols: list[str]
) -> int:
    """Walk JSON payload columns (payload_json, meta_json) and rewrite any embedded
    ISO-looking string value to UTC ISO.

    Keeps numeric and non-ISO strings intact.
    """
    payload_cols = []
    try:
        cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return 0
    for _, col_name, *_ in cols:
        if col_name in ("payload_json", "meta_json", "metadata", "context_json"):
            payload_cols.append(col_name)
    if not payload_cols or not id_cols:
        return 0
    changes = 0
    for col in payload_cols:
        try:
            select_cols = ", ".join([*id_cols, col])
            rows = conn.execute(
                f"SELECT {select_cols} FROM {table} WHERE {col} IS NOT NULL AND {col} != ''"
            ).fetchall()
        except sqlite3.Error:
            continue
        for row in rows:
            pk_vals = list(row[:-1])
            old = row[-1]
            try:
                obj = json.loads(old)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            new_obj = _rewrite_nested(obj)
            if new_obj == obj:
                continue
            set_clause = f"{col} = ?"
            where_clause = " AND ".join([f"{c} = ?" for c in id_cols])
            try:
                conn.execute(
                    f"UPDATE {table} SET {set_clause} WHERE {where_clause}",
                    (json.dumps(new_obj, ensure_ascii=False), *pk_vals),
                )
                changes += 1
            except sqlite3.Error as exc:  # noqa: BLE001
                print(f"  ! {table}.{col} JSON rewrite failed: {exc}")
    return changes


def _rewrite_nested(obj):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, str) and ISO_RE.match(v):
                norm = normalize(v)
                out[k] = norm if norm else v
            elif isinstance(v, (dict, list)):
                out[k] = _rewrite_nested(v)
            else:
                out[k] = v
        return out
    if isinstance(obj, list):
        return [_rewrite_nested(x) for x in obj]
    return obj


def discover_databases(root: Path) -> list[Path]:
    """Return concrete DB paths that exist on disk for the known layout."""
    dbs: list[Path] = []
    apps_dir = root / "apps"
    if apps_dir.exists():
        for app_dir in apps_dir.iterdir():
            data = app_dir / "data"
            if not data.exists():
                continue
            for name in ("state.db", "runtime_log.db", "memory.db", "tasks.db",
                          "workflow.db", "sessions.db", "vitals.db", "hermes.db"):
                p = data / name
                if p.exists() and p.is_file():
                    dbs.append(p)
    return dbs


def primary_keys_for(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return ["rowid"]
    pk = [c[1] for c in cols if c[5] == 1]
    if pk:
        return pk
    # Composite or no PK → fall back to rowid
    return ["rowid"]


def migrate_db(db_path: Path, *, apply: bool) -> None:
    print(f"\n=== {db_path} ===")
    if apply:
        conn = sqlite3.connect(str(db_path))
    else:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        columns = scan_columns(conn)
        print(f"  TEXT time columns: {len(columns)}")
        for table, col in columns:
            try:
                pk_cols = primary_keys_for(conn, table)
                select = f"SELECT rowid AS __rowid, {col} FROM {table} WHERE {col} IS NOT NULL AND {col} != ''"
                rows = conn.execute(select).fetchall()
            except sqlite3.Error as exc:
                print(f"  ! cannot read {table}.{col}: {exc}")
                continue
            changes = 0
            samples: list[str] = []
            for r in rows:
                old = r[col]
                new = normalize(old)
                if not new or new == old:
                    continue
                changes += 1
                if len(samples) < 3:
                    samples.append(f"    {old!r} → {new!r}")
                if apply:
                    conn.execute(
                        f"UPDATE {table} SET {col} = ? WHERE rowid = ?",
                        (new, r["__rowid"]),
                    )
            tag = "wrote" if apply else "would update"
            print(f"  {table}.{col}: {tag} {changes} rows")
            for s in samples:
                print(s)
        # JSON payload normalization
        for table_name_result in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ):
            table = table_name_result["name"]
            n = normalize_payload_json_columns(
                conn, table, primary_keys_for(conn, table)
            )
            if n:
                tag = "wrote" if apply else "would update"
                print(f"  {table}.*_json: {tag} {n} JSON payloads")
        if apply:
            conn.commit()
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Commit changes; default is dry-run")
    parser.add_argument("--dry-run", action="store_true", help="Force read-only display")
    args = parser.parse_args()
    apply = args.apply and not args.dry_run
    root = Path(__file__).resolve().parent.parent
    dbs = discover_databases(root)
    if not dbs:
        print("No DBs discovered; aborting.")
        return
    print(f"Discovered {len(dbs)} DB file(s) under {root}:")
    for d in dbs:
        print(f"  - {d}")
    for d in dbs:
        migrate_db(d, apply=apply)
    print("\nDone." if apply else "\nDry-run complete; rerun with --apply to commit.")


if __name__ == "__main__":
    main()
