"""统一切片 — 运行时注意力缓存 单测。

设计来源用户文档 §5.1:动态权重只活在当前对话/runtime,不写底层 chunks。
"""
from __future__ import annotations

import pytest

from domain.memory.memory.recall.unified import attention_cache


@pytest.fixture(autouse=True)
def _clear_cache():
    """每测前清 cache, 隔离。"""
    attention_cache.reset_for_test()
    yield
    attention_cache.reset_for_test()


def test_bump_increases_attention() -> None:
    v0 = attention_cache.get_activation(100)
    assert v0 == 0.0, "初始应为 0"

    v1 = attention_cache.bump_activation(100, delta=0.3)
    assert v1 == pytest.approx(0.3)
    v2 = attention_cache.bump_activation(100, delta=0.3)
    assert v2 == pytest.approx(0.6)


def test_bump_caps_at_1() -> None:
    for _ in range(10):
        v = attention_cache.bump_activation(200, delta=0.5)
    assert v == 1.0, "activation 上限是 1.0"


def test_decay_over_time() -> None:
    """bump 一次后, 时间过去 1 个半衰期(30 min) — 值应掉到约 1/2。"""
    base_time = 1000000.0
    attention_cache.bump_activation(300, delta=0.8, now=base_time)
    v_just_after = attention_cache.get_activation(300, now=base_time)
    assert v_just_after == pytest.approx(0.8, abs=0.01)

    # 过了 30 分钟(1 个半衰期) — 应约 0.4
    v_after_30min = attention_cache.get_activation(300, now=base_time + 1800)
    assert v_after_30min == pytest.approx(0.4, abs=0.05)


def test_decay_all_clears_old() -> None:
    """decay_all 应清掉陈旧的。"""
    base_time = 1000000.0
    # 一条旧的(bump 后过 10 个半衰期 ≈ 5 小时, 值应 ~0)
    attention_cache.bump_activation(400, delta=0.5, now=base_time - 36000)
    # 一条新的
    attention_cache.bump_activation(401, delta=0.5, now=base_time)

    removed = attention_cache.decay_all(now=base_time)
    assert removed >= 1, "至少旧的应该被清"
    # 新的仍在
    assert attention_cache.get_activation(401, now=base_time) > 0.4


def test_get_activations_batch() -> None:
    """批量读应在一次锁内完成。"""
    for cid in [1, 2, 3]:
        attention_cache.bump_activation(cid, delta=0.5)
    out = attention_cache.get_activations([1, 2, 3, 999])
    assert 1 in out and 2 in out and 3 in out
    assert 999 not in out  # 不存在的应该不返
    assert all(0.4 <= v <= 0.6 for v in out.values())


def test_seed_from_chunks_compat() -> None:
    """seed_from_chunks 接 iterable 兼容, 不存在 id 不影响。"""
    n = attention_cache.seed_from_chunks(
        [10, 20, -1], [0.3, 0.0, 0.5]  # -1 和 0 都跳过
    )
    assert n == 1, "只有 id=10 value=0.3 应 seed"
    assert attention_cache.get_activation(10) == pytest.approx(0.3, abs=0.01)


def test_snapshot_stats() -> None:
    attention_cache.bump_activation(100, delta=0.5)
    attention_cache.bump_activation(200, delta=0.3)
    s = attention_cache.snapshot_stats()
    assert s["entries"] == 2
    assert s["total_activation"] > 0.5
    assert s["top_id"] == 100  # 0.5 > 0.3
