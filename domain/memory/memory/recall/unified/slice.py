"""P3 — 统一切片原子 + 参数演化引擎 (自动驱动器半边)。

设计:
- docs/design/unified-memory.md §2 切片原子 / §6.4 双驱动
- specs/002-unified-memory/data-model.md

P3 在 P1 预埋的 phase 列基础上,把 chunks 表扩为承载完整切片属性,
并把"时衰/计数/归档"这些今天散落各处的逻辑收敛成单一函数族(§FR-304)。
新增记忆源只需提供一组基线值 + 一个归一器 — 检索/演化/投递零修改(§FR-303)。
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Literal

# ───────────────────── 数据类 ─────────────────────

Phase = Literal["experience", "cognition"]


@dataclass
class Slice:
    """统一切片原子。与 chunks 表行对应,字段名与列名 1:1 (除 entity_links/attention_tokens
    序列化为 JSON text)。data-model.md §切片原子 是设计参考,本类是它的 Python 表达。

    设计原则(spec §1 拍平):不按 source_kind 写代码分叉,差异通过字段值体现。
    """

    # 身份与内容(chunks 表既有列)
    id: int | None = None  # 数据库 PK;None = 未持久化
    body: str = ""          # chunks.text
    source: str = ""        # chunks.source
    chunk_hash: str = ""

    # P1/P3 扩展列
    phase: Phase = "experience"
    source_kind: str = ""

    # 时序元(三类连边的 ② 类)
    created_at: float = field(default_factory=time.time)
    session_id: str = ""
    segment_index: int | None = None

    # 诞生链(三类连边的 ③ 类;认知专属)
    derived_from: list[int] = field(default_factory=list)
    derive_kind: str = ""  # promote | cluster | supersede | manual

    # 参数(自动驱动器读写)
    authority: float = 0.5
    permanence: float = 0.3
    freshness: float = 1.0
    activation: float = 0.0
    verification: float = 0.0

    # 认知专属(P4 完整启用,P3 预埋)
    evidence_count: int = 0
    challenge_count: int = 0
    cognition_state: str | None = None
    supersede_by: int | None = None

    # 导航(三类连边的注意力提权)
    entity_links: list[str] = field(default_factory=list)
    attention_tokens: list[str] = field(default_factory=list)

    provenance: str = ""

    # ───── 序列化 ─────
    def to_row(self) -> dict[str, Any]:
        """转成适配 chunks 表列的 dict(JSON 字段已序列化)。
        SQLite 不原生存 list,所以 entity_links / attention_tokens /
        derived_from 用 JSON text。
        """
        return {
            "source": self.source,
            "chunk_hash": self.chunk_hash,
            "text": self.body,
            "file_mtime": self.created_at,
            "created_at": self.created_at,
            "phase": self.phase,
            "source_kind": self.source_kind,
            "session_id": self.session_id,
            "segment_index": self.segment_index,
            "derived_from": json.dumps(self.derived_from, ensure_ascii=False),
            "derive_kind": self.derive_kind,
            "authority": self.authority,
            "permanence": self.permanence,
            "freshness": self.freshness,
            "activation": self.activation,
            "verification": self.verification,
            "evidence_count": self.evidence_count,
            "challenge_count": self.challenge_count,
            "cognition_state": self.cognition_state,
            "supersede_by": self.supersede_by,
            "entity_links": json.dumps(self.entity_links, ensure_ascii=False),
            "attention_tokens": json.dumps(self.attention_tokens, ensure_ascii=False),
            "provenance": self.provenance,
        }

    @classmethod
    def from_row(cls, row: Any) -> "Slice":
        """从 chunks 表行(Sqlite Row/dict)构造 Slice。
        容忍字段缺失(老表迁移前的列)。JSON 字段安全解析。
        """
        def _g(name: str, default: Any = None) -> Any:
            if hasattr(row, "keys"):
                return row[name] if name in row.keys() else default
            return row.get(name, default) if isinstance(row, dict) else default

        def _jlist(name: str) -> list:
            raw = _g(name, "[]") or "[]"
            try:
                v = json.loads(raw) if isinstance(raw, str) else raw
                return v if isinstance(v, list) else []
            except Exception:
                return []

        return cls(
            id=_g("id"),
            body=_g("text", "") or "",
            source=_g("source", "") or "",
            chunk_hash=_g("chunk_hash", "") or "",
            phase=_g("phase", "experience") or "experience",
            source_kind=_g("source_kind", "") or "",
            created_at=float(_g("created_at", 0.0) or 0.0),
            session_id=_g("session_id", "") or "",
            segment_index=_g("segment_index", None),
            derived_from=_jlist("derived_from"),
            derive_kind=_g("derive_kind", "") or "",
            authority=float(_g("authority", 0.5) or 0.5),
            permanence=float(_g("permanence", 0.3) or 0.3),
            freshness=float(_g("freshness", 1.0) or 1.0),
            activation=float(_g("activation", 0.0) or 0.0),
            verification=float(_g("verification", 0.0) or 0.0),
            evidence_count=int(_g("evidence_count", 0) or 0),
            challenge_count=int(_g("challenge_count", 0) or 0),
            cognition_state=_g("cognition_state", None),
            supersede_by=_g("supersede_by", None),
            entity_links=_jlist("entity_links"),
            attention_tokens=_jlist("attention_tokens"),
            provenance=_g("provenance", "") or "",
        )


# ───────────────────── 基线映射(新增源只需登记) ─────────────────────

# data-model.md §相位映射 的初版默认值。跑起来再调。
_BASELINES: dict[str, dict[str, Any]] = {
    # 经历类
    "identity":          {"phase": "experience", "source_kind": "narrative",      "authority": 0.5, "permanence": 0.3},
    "journal":           {"phase": "experience", "source_kind": "narrative",      "authority": 0.5, "permanence": 0.3},
    "notes":             {"phase": "experience", "source_kind": "scratchpad",     "authority": 0.3, "permanence": 0.2},
    "conversation":      {"phase": "experience", "source_kind": "conversation",   "authority": 0.5, "permanence": 0.3},
    "digest_session":    {"phase": "experience", "source_kind": "digest",         "authority": 0.6, "permanence": 0.3},
    "digest_segment":    {"phase": "experience", "source_kind": "digest",         "authority": 0.6, "permanence": 0.3},
    "digest_day":        {"phase": "experience", "source_kind": "digest",         "authority": 0.7, "permanence": 0.5},
    "digest_week":       {"phase": "experience", "source_kind": "digest",         "authority": 0.8, "permanence": 0.6},
    # 经历零散
    "him":               {"phase": "experience", "source_kind": "user_memory",    "authority": 0.5, "permanence": 0.4},
    "goals":             {"phase": "experience", "source_kind": "goal",           "authority": 0.5, "permanence": 0.4},
    "plans":             {"phase": "experience", "source_kind": "plan",           "authority": 0.5, "permanence": 0.4},
    "work":              {"phase": "experience", "source_kind": "work",           "authority": 0.3, "permanence": 0.2},
    "context":           {"phase": "experience", "source_kind": "context",        "authority": 0.3, "permanence": 0.1},
    # 认知类
    "rules":             {"phase": "cognition",  "source_kind": "rule",           "authority": 1.0, "permanence": 0.95},
    "lessons":           {"phase": "cognition",  "source_kind": "lesson",         "authority": 0.8, "permanence": 0.85},
    "self_knowledge":    {"phase": "cognition",  "source_kind": "self",           "authority": 0.7, "permanence": 0.85},
    "knowledge":         {"phase": "cognition",  "source_kind": "profile",        "authority": 0.7, "permanence": 0.85},
}


def baselines_for_source(source: str) -> dict[str, Any]:
    """按 source 查基线值。未知 source 走最小默认(experience, authority 0.3)。
    新增源(spec FR-303 只需登记基线值)调用 register_normalizer_source() 注册。
    """
    return _BASELINES.get(source, {
        "phase": "experience", "source_kind": "misc",
        "authority": 0.3, "permanence": 0.2,
    })


def register_normalizer(source: str, baseline: dict[str, Any]) -> None:
    """新增记忆源时调用:登记基线值(phase/source_kind/authority/permanence)。
    这之后,任何写入这个 source 的 chunks 都会自动获得这组初值。
    """
    needed_keys = {"phase", "source_kind", "authority", "permanence"}
    if not needed_keys <= set(baseline.keys()):
        raise ValueError(f"baseline must include {needed_keys}; got {set(baseline.keys())}")
    _BASELINES[source] = baseline


# ───────────────────── 参数演化引擎(自动驱动器) ─────────────────────

# 常数(跑起来再调),按设计文档"参数先立机制"原则
_FRESHNESS_FLOOR = 0.05   # freshness 跌破此 × permanence 时触发归档
_ARCHIVE_FRESHNESS_RATIO = 0.1  # 归档阈值 = max(floor, ratio × permanence)
_ACTIVATION_DECAY_PER_HOUR = 0.5  # activation 每小时减半(短期热信号)
_PERHOUR_LAMBDA = 0.005  # freshness 衰减系数:permanence=0 时 720h(30d)衰减到 exp(-3.6)=0.027


def _decay_freshness(slice: Slice, *, delta_hours: float) -> None:
    """freshness 按 Δt × (1 - permanence) 衰减(指数, 不是线性)。
    permanence 高(认知≈0.95+)→ 衰减极慢;permanence 低(经历≈0.3)→ 快衰。

    公式: freshness *= exp(-delta_hours × (1 - permanence) × _PERHOUR_LAMBDA)
    设计文档 §6.4: 认知 "几乎不衰",permanence 0.95 时 30d 后衰减 < 5%,
    permanence 0.3 时 30d 衰减到接近 0(归档合理)。
    """
    if delta_hours <= 0:
        return
    rate = (1.0 - slice.permanence) * _PERHOUR_LAMBDA
    decay = math.exp(-rate * delta_hours)
    slice.freshness *= decay
    slice.freshness = max(slice.freshness, 0.0)


def _decay_activation(slice: Slice, *, delta_hours: float) -> None:
    """activation 每小时减半(短期热信号)。"""
    if delta_hours <= 0 or slice.activation <= 0:
        return
    halvings = delta_hours / 1.0  # 半衰期 1h
    slice.activation *= 0.5 ** halvings
    if slice.activation < 0.01:
        slice.activation = 0.0


def update_slice_dynamics(slice: Slice, *, now: float | None = None) -> dict[str, Any]:
    """自动驱动器入口 — 给单个 slice 应用所有时间驱动规则。
    P3 实现: Δt 衰减(freshness + activation)、归档阈值判定。
    P4 扩展: access/reference/verified/falsified 等信号(模型驱动半边)。

    返回 {archived: bool, changes: {...}} 供 caller 决定是否 commit。
    """
    if now is None:
        now = time.time()
    delta_s = max(0.0, now - slice.created_at)
    delta_hours = delta_s / 3600.0

    before = {
        "freshness": slice.freshness,
        "activation": slice.activation,
        "cognition_state": slice.cognition_state,
    }

    _decay_freshness(slice, delta_hours=delta_hours)
    _decay_activation(slice, delta_hours=delta_hours)

    changes: dict[str, Any] = {}
    if slice.freshness != before["freshness"]:
        changes["freshness"] = slice.freshness
    if slice.activation != before["activation"]:
        changes["activation"] = slice.activation

    # 归档阈值:认知 slice permanence≈1 → 几乎不可能归档(靠 challenge 失活,P4 处理)
    archive_floor = max(_FRESHNESS_FLOOR, _ARCHIVE_FRESHNESS_RATIO * slice.permanence)
    archived = False
    if (
        slice.cognition_state not in ("archived", "replaced")
        and slice.freshness < archive_floor
    ):
        slice.cognition_state = "archived"
        changes["cognition_state"] = "archived"
        archived = True

    return {"archived": archived, "changes": changes}


__all__ = [
    "Slice",
    "baselines_for_source",
    "register_normalizer",
    "update_slice_dynamics",
]
