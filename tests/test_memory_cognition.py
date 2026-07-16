"""P4 认知演化生命周期 测试 — 聚焦三铁律防偏执 + supersede 链。

对应 spec SC-008 (想不强化)、SC-009 (链可溯源)、SC-010 (多级取代轨迹)。
"""

from __future__ import annotations

import pytest

from domain.memory.memory.recall.unified.slice import Slice
from domain.memory.memory.recall.unified.cognition import (
    CognitionState,
    on_access,
    on_reference,
    on_verified,
    on_falsified,
    promote,
    supersede,
    revise,
    cluster_born,
    visibility_decay,
    should_be_promoted_candidate,
)


def test_access_does_not_reinforce_authority_or_verification() -> None:
    """SC-008:被命中 100 次的切片 authority / verification 应不变(只动 activation)。
    这是防回声室的根本闸。
    """
    cognition = Slice(
        id=1, source="rules", phase="cognition",
        authority=0.5, verification=0.0, activation=0.0,
    )
    for _ in range(100):
        on_access(cognition)

    assert cognition.authority == 0.5, "access MUST NOT 改 authority"
    assert cognition.verification == 0.0, "access MUST NOT 改 verification"
    assert cognition.activation > 0.5, "access 应显著提升 activation"


def test_only_cognition_accepts_structural_reinforcement() -> None:
    """铁律二:verified/falsified 结构性强化仅作用于认知切片。
    经历类 verified 只走 reference 微涨路径,不该把 authority 拉到认知基线之上。
    """
    exp = Slice(id=10, source="digest_session", phase="experience",
                authority=0.5, verification=0.0, activation=0.0)
    on_verified(exp)
    # 经历类被 verified 应走 reference 路径: authority 微涨
    assert exp.authority < 0.7, "经历类不应被 verified 拉到认知基线"
    assert exp.verification > 0

    cog = Slice(id=11, source="rules", phase="cognition",
                authority=0.7, verification=0.0, cognition_state="active",
                activation=0.0)
    on_verified(cog)
    assert cog.authority > 0.7, "认知类 verified 应使 authority +Δ"
    assert cog.evidence_count == 1


def test_promote_then_on_verified_works_post_promotion() -> None:
    """铁律三:强化是晋升的结果,不是因。promote 后才接受 on_verified 强化。
    """
    exp = Slice(id=20, source="conversation", phase="experience",
                authority=0.5, verification=2.5, activation=0.0,
                entity_links=["A+策略"], attention_tokens=["A+"])
    # 判定 it's a promote 候选
    assert should_be_promoted_candidate(exp) is True, "verification 达阈即晋升候选"

    promoted = promote(exp, summary="关于 A+ 策略的总体判断")
    assert promoted.phase == "cognition"
    assert promoted.cognition_state == CognitionState.NASCENT.value
    assert promoted.derived_from == [20], "derived_from 必须指向原经历"
    assert promoted.verification == 0.0, "新认知 verification 从 0 起算(不继承)"

    # promote 之后,verified 才开始累积(铁律三)
    on_verified(promoted)
    assert promoted.verification == 1.0
    # evidence_count:promote 时设 1(原经历的支撑),verified 再 +1 = 2
    assert promoted.evidence_count == 2


def test_supersede_preserves_chain() -> None:
    """SC-009: supersede 必须保留双向链 + 永不硬删。
    """
    old = Slice(id=100, source="rules", phase="cognition",
                authority=0.4, cognition_state="active",
                freshness=0.8, entity_links=["X"])
    new = Slice(id=200, source="rules", phase="cognition",
                authority=0.8, cognition_state="active", entity_links=["X"])

    supersede(old, new)

    # 老 row 保留,标 replaced + freshness 0 + supersede_by 指向新
    assert old.cognition_state == CognitionState.REPLACED.value
    assert old.freshness == 0.0
    assert old.supersede_by == 200

    # 新 row 拿到 derived_from
    assert 100 in new.derived_from, "supersede 必须把老 id 写入新 derived_from"
    assert new.derive_kind == "supersede"
    # entity_links 续传(骨架不丢)
    assert "X" in new.entity_links


def test_falsified_drives_cognition_to_challenged() -> None:
    """反证累积应触发 active→challenged 跃迁。"""
    cog = Slice(id=30, source="lessons", phase="cognition",
                authority=0.8, verification=3.0, challenge_count=0,
                cognition_state="active", activation=0.0)
    on_falsified(cog, reason="实盘失败")
    assert cog.challenge_count == 1
    # 单次反证不立刻 challenged(阈值 challenge>=2 或 authority<0.4)
    assert cog.cognition_state == "active"

    on_falsified(cog)
    assert cog.challenge_count == 2
    assert cog.cognition_state == CognitionState.CHALLENGED.value
    assert cog.authority < 0.8, "falsified 应扣 authority"


def test_cluster_born_constructs_higher_cognition() -> None:
    """B 路径: cluster_born 把多认知合成更高阶认知,继承 entity_links + derived_from。"""
    m1 = Slice(id=1, source="lessons", phase="cognition", authority=0.7,
               entity_links=["策略A"], evidence_count=2)
    m2 = Slice(id=2, source="lessons", phase="cognition", authority=0.8,
               entity_links=["策略B"], evidence_count=3)
    higher = cluster_born([m1, m2], summary="综合判断: 策略选择应基于市场流动性")

    assert higher is not None
    assert higher.cognition_state == CognitionState.HIGHER.value
    assert set(higher.derived_from) == {1, 2}
    assert set(higher.entity_links) == {"策略A", "策略B"}
    assert higher.permanence == 0.9, "元认知抗衰更强"
    assert 0.7 <= higher.authority <= 0.8


def test_cluster_born_null_safety() -> None:
    """空 members 或空 summary 应安全返回 None(§FR-406 退化策略基础)。"""
    assert cluster_born([], summary="x") is None
    assert cluster_born([Slice(id=1, phase="cognition")], summary="   ") is None


def test_visibility_decay_buries_stale_strong_cognition() -> None:
    """§6.7 健康遗忘:长期无新证据 + 无反证的高 authority 认知,可见性降到 0.5。
    但 authority 不变。
    """
    stale_strength = Slice(id=50, source="rules", phase="cognition",
                           authority=0.95, verification=5.0,
                           evidence_count=1, challenge_count=0, activation=0.0)
    factor = visibility_decay(stale_strength)
    assert factor < 0.6, "陈旧无证据认知可见性应 < 0.6"
    # authority 不变 — 我们只是不让它出现在排序里
    assert stale_strength.authority == 0.95

    # 充足证据的不埋
    backed = Slice(id=51, source="rules", phase="cognition",
                   authority=0.9, evidence_count=5, challenge_count=0)
    assert visibility_decay(backed) >= 0.9

    # 被质疑的不埋
    questioned = Slice(id=52, source="rules", phase="cognition",
                       authority=0.6, challenge_count=1, evidence_count=0,
                       activation=0.0)
    assert visibility_decay(questioned) == 1.0


def test_multilevel_supersede_chain_traceable() -> None:
    """SC-010: 多级取代后,链路仍可溯源到最初认知。
        v1 → v2 (supersede) → v3 (supersede)
        从 v3 的 derived_from 各级追回 v1。
    """
    v1 = Slice(id=1, source="rules", phase="cognition",
               authority=0.6, entity_links=["topic_Z"])
    v2 = Slice(id=2, source="rules", phase="cognition",
               authority=0.7, entity_links=["topic_Z"])
    v3 = Slice(id=3, source="rules", phase="cognition",
               authority=0.8, entity_links=["topic_Z"])

    supersede(v1, v2)  # v2.derived_from += [1]
    supersede(v2, v3)  # v3.derived_from += [2]

    # 从 v3 溯源到 v2(直接),从 v2 溯源到 v1
    assert 2 in v3.derived_from
    assert 1 in v2.derived_from
    # 链头: v1 / v2 都 replaced 状态;v3 active
    assert v1.cognition_state == CognitionState.REPLACED.value
    assert v2.cognition_state == CognitionState.REPLACED.value
    # 永不硬删: v3.derived_from 含 2, v2.derived_from 含 1 — 都可溯源
