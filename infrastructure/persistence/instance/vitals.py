"""Vitals DB — per-instance ``apps/<id>/data/vitals.db``.

Single-row state machine for the life-simulation engine: energy level
and the most recent nurture event. Pulled out of ``state.db`` so the
vitals module owns its persistence without sharing a lock with the
event scheduler or runtime audit.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from infrastructure.config import get_app_instance_id, get_instance_data_dir
from infrastructure.persistence.instance.base import InstanceDB


SCHEMA_VITALS = """
CREATE TABLE IF NOT EXISTS vitals (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    instance_id TEXT NOT NULL,
    energy REAL NOT NULL DEFAULT 70.0,
    updated_at REAL NOT NULL,
    last_nurture TEXT
);
"""

SCHEMA_VITALS_LOG = """
CREATE TABLE IF NOT EXISTS vitals_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id TEXT NOT NULL,
    energy_before REAL,
    energy_after REAL,
    delta REAL,
    reason TEXT,
    source_ref TEXT,
    occurred_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vitals_log_time ON vitals_log(instance_id, occurred_at DESC);
"""


class VitalsDB(InstanceDB):
    SCHEMA_SQL = (SCHEMA_VITALS, SCHEMA_VITALS_LOG)

    def __init__(self, db_path: Path | None = None, instance_id: str | None = None) -> None:
        if db_path is None:
            db_path = get_instance_data_dir(instance_id) / "vitals.db"
        super().__init__(db_path)
        self.instance_id = instance_id or get_app_instance_id() or ""
        self._ensure_singleton_row()

    def _ensure_singleton_row(self) -> None:
        existing = self.fetchone("SELECT id FROM vitals WHERE id = 1")
        if existing:
            return
        # capability 闸：服务型 vitals 关时不种子行（bundle eager 建库免不了，
        # 但"不生效而非没有"——零数据行；实例行为不变）
        try:
            from domain.service.capabilities import capability_enabled

            if not capability_enabled("vitals", self.instance_id):
                return
        except Exception:
            pass
        self.execute(
            "INSERT INTO vitals (id, instance_id, energy, updated_at) VALUES (1, ?, ?, ?)",
            (self.instance_id, 70.0, time.time()),
        )
        self.commit()

    def snapshot(self) -> dict[str, Any]:
        row = self.fetchone("SELECT * FROM vitals WHERE id = 1")
        return row or {}

    def adjust_energy(
        self,
        delta: float,
        *,
        reason: str | None = None,
        source_ref: str | None = None,
    ) -> float:
        cur_row = self.snapshot()
        before = float(cur_row.get("energy") or 70.0)
        after = max(0.0, before + delta)
        self.execute(
            "UPDATE vitals SET energy = ?, updated_at = ? WHERE id = 1",
            (after, time.time()),
        )
        self.execute(
            """INSERT INTO vitals_log
            (instance_id, energy_before, energy_after, delta, reason, source_ref, occurred_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (self.instance_id, before, after, delta, reason, source_ref, time.time()),
        )
        self.commit()
        return after

    def set_energy(self, value: float, *, reason: str | None = None) -> float:
        cur_row = self.snapshot()
        before = float(cur_row.get("energy") or 70.0)
        clamped = max(0.0, value)
        self.execute(
            "UPDATE vitals SET energy = ?, updated_at = ? WHERE id = 1",
            (clamped, time.time()),
        )
        self.execute(
            """INSERT INTO vitals_log
            (instance_id, energy_before, energy_after, delta, reason, occurred_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (self.instance_id, before, clamped, clamped - before, reason, time.time()),
        )
        self.commit()
        return clamped

    def set_last_nurture(self, note: str) -> None:
        self.execute(
            "UPDATE vitals SET last_nurture = ?, updated_at = ? WHERE id = 1",
            (note, time.time()),
        )
        self.commit()

    def list_log(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.fetchall(
            "SELECT * FROM vitals_log WHERE instance_id = ? ORDER BY occurred_at DESC, id DESC LIMIT ?",
            (self.instance_id, limit),
        )
