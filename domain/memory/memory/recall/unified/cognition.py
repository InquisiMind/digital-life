"""P4 — 认知演化生命周期。

设计:
- docs/design/unified-memory.md §6.3 三铁律 / §6.4 跃迁 / §6.6 双驱动
- specs/002-unified-memory/data-model.md §认知状态机

实现六类跃迁 + 三条防偏执铁律:
  - promote        经历→认知晋升,phase 跃迁 + 重置参数
  - reinforce      active→reinforced(verification 累积)
  - challenge      active→challenged(反证累积)
  - supersede      老认知→replaced,新认知接管 entity_links(永不硬删,带 supersede_by 链)
  - revise         模型重写 body(精修)
  - cluster_born   多认知聚成更高阶新认知(B 路径)

调用方:
  - 自动驱动器(set_instance → access/reference/verified/falsified 累积)
  - 认知驱动器(memory_hygiene / dream 周期 model_promote/merge/supersede/cluster_born)

铁律(强制):
  一、access 只动 activation(短期),不动 authority / verification。
  二、structural reinforcement (verified/falsified) 仅对 phase=cognition 生效。
  三、promotion 是强化的因,不是果 — promoted 之后才接受 verified/falsified。
"""

from __future__ import annotations

import enum
import logging
import time
from typing import Any, Literal

from domain.memory.memory.recall.unified.slice import Slice

logger = logging.getLogger("domain.memory.recall.unified.cognition")


class CognitionState(str, enum.Enum):
    """认知切片的生命周期状态(§6.2 状态机)。
    经历切片 cognition_state = None(非认知)。
    """
    NASCENT = "nascent"          # 经历刚被提纯成认知(进入 active 前的过渡)
    ACTIVE = "active"            # 默认可召回态
    REINFORCED = "reinforced"    # 反复正反馈、evidence 高
    REVISING = "revising"        # 模型正在重写
    CHALLENGED = "challenged"    # 被反证降级
    ARCHIVED = "archived"        # 移出默认召回池,按需可见
    REPLACED = "replaced"        # 被新认知取代,链换乘
    HIGHER = "higher"            # 由聚类诞生的更高阶认知


# ───────────────────── 信号 / 三铁律 ─────────────────────

# 每条信号对每个参数的影响幅度
# 跑起来再调(spec §参数先立机制);目前是合理初始值
_DEFAULT_DELTAS = {
    "access_bump_activation":   0.3,    # 短期活跃度上涨
    "reference_bump_activation": 0.4,
    "reference_experience_authority": 0.02,  # 经历类"应晋升"信号微微
    "reference_verification_inc": 0.5,
    "verified_authority_inc":   0.05,   # 仅认知类
    "verified_verification_inc": 1.0,
    "falsified_authority_dec":  0.10,   # 仅认知类
    "falsified_verification_dec": 1.0,
}


def _bump_act(slice: Slice, key: str, *, now: float | None = None) -> None:
    """统一处理 activation 提升: 同时写 attention_cache(运行时,优先)和
    slice.activation(兼容字段, 单测友好)。都限幅 [0, 1.0]。
    """
    delta = _DEFAULT_DELTAS[key]
    slice.activation = min(1.0, slice.activation + delta)
    if slice.id is not None and slice.id >= 0:
        try:
            from domain.memory.memory.recall.unified.attention_cache import (
                bump_activation,
            )
            bump_activation(slice.id, delta=delta, now=now)
        except Exception:
            pass


def on_access(slice: Slice, *, now: float | None = None) -> None:
    """铁律一:被命中只动 activation,不动 authority / verification。

    P3+ 修正(用户文档 §5.1): activation 不再写 slice.activation (chunks 持久层),
    改写运行时 attention_cache。重启进程后 activation 自然清零,符合"动态权重
    只活在当前对话"原则。slice.activation 字段仍保留作 from_row 兼容读取,
    但运行时 cache 优先(见 facade._route_lexical)。
    """
    # 优先写运行时 cache(若 chunk_id 已知)
    _bump_act(slice, "access_bump_activation", now=now)
    # 不动 authority, 不动 verification — 堵回声室的根本闸


def on_reference(slice: Slice, *, now: float | None = None) -> None:
    """被模型复述/采纳。

    铁律二+三: verification 累积是普适的(模型复述 = 信号);
    authority 微涨仅经历类(作为"应晋升候选"信号);
    认知类 authority 不动(避免回声室)。
    """
    _bump_act(slice, "reference_bump_activation", now=now)
    slice.verification += _DEFAULT_DELTAS["reference_verification_inc"]
    if slice.phase == "experience":
        slice.authority = min(1.0, slice.authority + _DEFAULT_DELTAS["reference_experience_authority"])


def on_verified(slice: Slice, *, now: float | None = None) -> None:
    """实践正反馈(任务成功 / 验证通过)。

    铁律二: 仅 phase=cognition 才接受结构性强化(authority +Δ)。
    经历类的"正反馈"作为 on_reference 一样的微涨(避免双重计分)。

    V3 (2026-07-23 #5): nascent → active 自动激活.
    nascent 是新写入认知的初始态(Alpha 反馈"何时晋升"主观判断痛点),
    通过 evidence_count ≥2 自动转入 active.
    """
    if slice.phase != "cognition":
        # 经历类按 reference 处理(走同一路径,简化)
        on_reference(slice, now=now)
        return
    _bump_act(slice, "reference_bump_activation", now=now)
    slice.authority = min(1.0, slice.authority + _DEFAULT_DELTAS["verified_authority_inc"])
    slice.verification += _DEFAULT_DELTAS["verified_verification_inc"]
    slice.evidence_count += 1
    # V3 #5: nascent 认知 evidence_count >=2 → 自动转 active (no mid-tier manual decision)
    # 这是 Alpha 反馈"碎片应该晋升而非主观判断"的结构化回答: 同源重新激活 = 升级证据
    if (
        slice.cognition_state == CognitionState.NASCENT.value
        and slice.evidence_count >= 2
        and slice.challenge_count == 0
    ):
        slice.cognition_state = CognitionState.ACTIVE.value
    # 自动跃迁: verification 充足 + 反证少 → REINFORCED
    if (
        slice.cognition_state == CognitionState.ACTIVE.value
        and slice.verification >= 2.0
        and slice.challenge_count == 0
    ):
        slice.cognition_state = CognitionState.REINFORCED.value


def on_falsified(slice: Slice, *, reason: str = "", now: float | None = None) -> None:
    """实践反证(任务失败 / 结果相左 / 模型反思)。

    仅认知类接受结构性反证(authority -Δ + challenge +1)。
    经历类的 falsified 退化成"该是被晋升的反向信号"——不动 authority,挑战计数仍可累积
    (P5 可能用作"晋升前已被反驳"的否决信号)。
    """
    slice.challenge_count += 1
    if slice.phase != "cognition":
        return
    slice.authority = max(0.0, slice.authority - _DEFAULT_DELTAS["falsified_authority_dec"])
    slice.verification = max(0.0, slice.verification - _DEFAULT_DELTAS["falsified_verification_dec"])
    # 自动跃迁: authority 跌 + challenge 计数高 → CHALLENGED
    if (
        slice.cognition_state in (CognitionState.ACTIVE.value, CognitionState.REINFORCED.value)
        and (slice.challenge_count >= 2 or slice.authority < 0.4)
    ):
        slice.cognition_state = CognitionState.CHALLENGED.value


# ───────────────────── 跃迁:结构性变化 ─────────────────────

# 认知基线:晋升后认知 slice 的默认起点
_COGNITION_BASELINE = {
    "authority": 0.7,
    "permanence": 0.85,
}


def promote(
    experience: Slice,
    *,
    summary: str = "",
    derived_from_ids: list[int] | None = None,
    entity_links: list[str] | None = None,
) -> Slice:
    """A 路径:经历反复触及同节点 → 模型提纯成认知。
    铁律三: 强化是晋升的结果,不是原因。
        - 经历切片的多次 referenced 是"应晋升"候选信号(触发此次调用);
        - 一旦 promoted,该认知才接受 verified/falsified 结构性强化。

    返回新生的认知 Slice(cognition_state=NASCENT → 调用方写库后转 active)。
    原经历保留(不删),其 derived_from 链反向指向认知体。
    """
    new_slice = Slice(
        source="knowledge",  # 概念卡
        chunk_hash="",        # 由调用方根据 body/content 计算
        body=summary or experience.body,
        phase="cognition",
        source_kind="profile",
        authority=_COGNITION_BASELINE["authority"],
        permanence=_COGNITION_BASELINE["permanence"],
        freshness=1.0,
        activation=0.0,
        verification=0.0,    # 从 0 起算,后靠 verified 累积(非继承)
        evidence_count=1,
        challenge_count=0,
        cognition_state=CognitionState.NASCENT.value,
        derived_from=derived_from_ids or ([experience.id] if experience.id is not None else []),
        derive_kind="promote",
        entity_links=entity_links or experience.entity_links,
        attention_tokens=list(experience.attention_tokens),
        provenance=f"promote from {experience.chunk_hash or experience.id}",
    )
    return new_slice


def supersede(
    old: Slice,
    new: Slice,
    *,
    now: float | None = None,
) -> None:
    """取代:老认知被新认知接替,链路换乘(§6.4 supersede)。

    - 老认知: cognition_state=REPLACED + supersede_by=新.id + freshness=0
    - 新认知: derived_from += [老.id],继承 entity_links(导航骨架持续)
    - 双方 row 都保留(永不硬删)
    """
    if old.id is None or new.id is None:
        logger.warning("supersede 要求双方已持久化(有 id);跳过链写入,只设字段")
    old.cognition_state = CognitionState.REPLACED.value
    old.freshness = 0.0
    old.invalid_at = now if now is not None else time.time()
    if new.id is not None:
        old.supersede_by = new.id
    if old.id is not None and old.id not in new.derived_from:
        new.derived_from = list(new.derived_from) + [old.id]
        new.derive_kind = "supersede"
    # P4: 新认知从 now 生效
    new.valid_at = now if now is not None else time.time()
    # 新认知继承老的 entity_links(导航骨架续传)
    merged_links = set(new.entity_links) | set(old.entity_links)
    new.entity_links = sorted(merged_links)


def revise(slice: Slice, *, new_body: str, version_log: str = "") -> None:
    """修订:模型重写 body。保留旧版做 provenance 记录,version++ 用 derive_kind 串起。

    (没有专门的 version int 列;用 provenance 累计标记,避免再扩 schema。)
    """
    old_preview = slice.body[:80].replace("\n", " ")
    slice.body = new_body
    extra = f" | revised ({version_log or 'no reason'})" if version_log else " | revised"
    slice.provenance = (slice.provenance or "") + extra
    if slice.cognition_state in (CognitionState.ACTIVE.value, CognitionState.REINFORCED.value):
        slice.cognition_state = CognitionState.REVISING.value
    logger.info("Cognition %s revised; was: %s…", slice.chunk_hash, old_preview)


def cluster_born(
    members: list[Slice],
    *,
    summary: str,
    entity_links: list[str] | None = None,
) -> Slice | None:
    """B 路径:对一组近义认知做语义聚类,产更高阶新认知。

    members 必须是 phase=cognition 的切片(谁有 id 更好,便于 derived_from 链)。
    返回新 HIGHER 认知 Slice;调用方负责持久化 + 写 entity_index(profile)。

    如 summary 为空或失败,调用方应 fallback:保留原样 + challenge_count +1(§FR-406)。
    本函数只构造 — 决策 vality 由 caller(memory_hygiene 模型决策)负责。
    """
    if not members or not summary.strip():
        return None
    # derived_from 收所有成员 id
    member_ids = [m.id for m in members if m.id is not None]
    member_links: set[str] = set()
    for m in members:
        member_links.update(m.entity_links)
    if entity_links:
        member_links.update(entity_links)
    new_cognition = Slice(
        source="knowledge",
        chunk_hash="",
        body=summary.strip(),
        phase="cognition",
        source_kind="higher_cognition",
        authority=sum(m.authority for m in members) / len(members),
        permanence=0.9,  # 元认知抗衰更强
        freshness=1.0,
        activation=0.0,
        verification=0.0,
        evidence_count=sum(m.evidence_count for m in members),
        challenge_count=0,
        cognition_state=CognitionState.HIGHER.value,
        derived_from=member_ids,
        derive_kind="cluster",
        entity_links=sorted(member_links),
        attention_tokens=list(member_links),  # 聚类后的 entity 名作 attention 锚
        provenance=f"cluster_born from {len(members)} cognitions",
    )
    return new_cognition


# ───────────────────── 健康遗忘:可见性衰减 ─────────────────────

def visibility_decay(slice: Slice, *, now: float | None = None) -> float:
    """§6.7 健康遗忘:长期无新证据、也无矛盾触及的高 authority 认知,
    降低其在排序中的可见性(不是降 authority,是降"被注意到的概率")。

    返回 [0,1] 衰减因子(乘到 rrf_score 上)。
    """
    if slice.phase != "cognition":
        return 1.0  # 经历类不参与 visible 性衰减(由 freshness 处理)

    # 用 evidence_count 与 challenge_count 衡量是否仍活跃;
    # 这里不从 chunks 里查 last_referenced_at(无该列),
    # 以 activation 作为"近期活跃"代理 — activation 低 + challenge=0 + evidence 小
    # 表示"长期无人碰也不被挑战",可见性应降。
    if slice.challenge_count > 0:
        return 1.0  # 仍在被质疑,不该埋
    if slice.evidence_count >= 3:
        return 1.0  # 证据充足,保留可见
    # activation 越低 → 越该埋
    # evidence < 3 + activation < 0.1 = "陈旧且无人提"
    # BUG FIX (Fix 2a): slice.activation reads from DB (always 0.0 after restart).
    # Use runtime attention_cache instead for live activation.
    from domain.memory.memory.recall.unified.attention_cache import get_activation
    runtime_act = get_activation(slice.id, now=now) if slice.id else 0.0
    factor = 0.5 + 0.5 * runtime_act
    return factor


# ───────────────────── 工具 ─────────────────────

def is_cognition(slice: Slice) -> bool:
    """判断是否是认知切片(经历返回 False)。"""
    return slice.phase == "cognition"


def should_be_promoted_candidate(
    experience: Slice,
    *,
    reference_threshold: float = 2.0,
    access_threshold: int = 5,
) -> bool:
    """判断某经历是否达到"应晋升"法语信号门槛。memory_hygiene 调。"""
    if experience.phase != "experience":
        return False
    if experience.verification < reference_threshold:
        return False
    return True  # verification 达阈即为候选;access 阈值可选


__all__ = [
    "CognitionState",
    "on_access",
    "on_reference",
    "on_verified",
    "on_falsified",
    "promote",
    "supersede",
    "revise",
    "cluster_born",
    "visibility_decay",
    "is_cognition",
    "should_be_promoted_candidate",
]
