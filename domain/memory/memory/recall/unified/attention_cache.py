"""P3+ — 运行时注意力缓存(process-level, 非持久)。

设计来源(用户文档 §5.1):
  "对话/思考上下文(工作台): 存储所有动态参数 — 思考轮次、实时注意力权重、
   衰减因子、激活记录、连锁关联分数。"

现状偏差: chunks 表的 activation 列混了"事实"(永久)与"动态"(运行时) — 重启
runtime 后 activation 还在, 违反"重启就该忘掉临时状态"。

修复架构:
  - chunks 表保留 activation 列(向后兼容、可查历史值)
  - 真正生效的 activation 由 process-level cache 持有:
    * 启动时从 chunks 表 seed 一遍(向后兼容历史)
    * 写 on_access 只动 cache, 不动 chunks 表
    * 重启进程后 cache 自然清零(回到持久层的"上次写入值")
  - facade 读 activation 优先读 cache(覆盖持久层)

API:
  bump_activation(chunk_id, delta)     模型每次想到某切片时调
  get_activation(chunk_ids)            facade 在 lexical 路 SELECT 时调
  decay_all(ration=0.5, max_age_s=...) 周期性衰减压低
  reset_for_test()                     单测隔离

本模块在场景过滤器(scene_weights.py)之外增添一层"最近想到过的"上下文增益,
不进 chunks 事实层(避免污染)。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Dict, Iterable

logger = logging.getLogger("domain.memory.recall.unified.attention_cache")

# 单进程 cache(整个 agent runtime 共用一份)
# {chunk_id: {"value": float, "last_bump": float, "bump_count": int}}
_cache: Dict[int, dict] = {}
_lock = threading.Lock()

# 上限 — 单实例一辈子最多看 50000 切片够用
_MAX_ENTRIES = 50000

# 默认衰减: 每 30 分钟把所有 value 减半(用户文档 §5.2 "几小时衰减")
_DECAY_HALFLIFE_SECONDS = 1800  # 30 min
_DECAY_MIN = 0.01  # 低于此值就清掉


def bump_activation(
    chunk_id: int, *, delta: float = 0.3, now: float | None = None
) -> float:
    """某切片被模型想到一次 → 给短期注意力 +delta。
    返回新的 value(限幅 [0, 1.0])。
    """
    if chunk_id < 0:
        return 0.0
    now = now if now is not None else time.time()
    with _lock:
        entry = _cache.get(chunk_id)
        if entry is None:
            entry = {"value": 0.0, "last_bump": now, "bump_count": 0}
            _cache[chunk_id] = entry
        entry["value"] = min(1.0, entry["value"] + delta)
        entry["last_bump"] = now
        entry["bump_count"] += 1
        # 上限保护
        if len(_cache) > _MAX_ENTRIES:
            # 简单 FIFO 删一次 — 真实场景不会撞到
            _evict_oldest_locked()
        return entry["value"]


def get_activation(chunk_id: int, *, now: float | None = None) -> float:
    """读单条。不存在返 0.0。
    不触发衰减(读不应该写), 但应用当前衰减估算:
    effective = value * 0.5 ^ ((now - last_bump) / halflife)
    """
    now = now if now is not None else time.time()
    with _lock:
        entry = _cache.get(chunk_id)
        if entry is None:
            return 0.0
        return _decay_view(entry, now)


def get_activations(chunk_ids: Iterable[int], *, now: float | None = None) -> dict[int, float]:
    """批量读。返回 {chunk_id: effective_activation}。
    适合 facade 一次拿一组 chunk_id 的场景。
    """
    now = now if now is not None else time.time()
    out: dict[int, float] = {}
    cids = list(chunk_ids)
    with _lock:
        for cid in cids:
            entry = _cache.get(cid)
            if entry is None:
                continue
            v = _decay_view(entry, now)
            if v > 0:
                out[cid] = v
    return out


def decay_all(*, now: float | None = None) -> int:
    """定期清一次:把所有 value 按 last_bump 算的衰减齐一次,
    低于 _DECAY_MIN 的删掉。返回清掉的条数。
    """
    now = now if now is not None else time.time()
    removed = 0
    with _lock:
        items = list(_cache.items())
        for cid, entry in items:
            v = _decay_view(entry, now)
            if v < _DECAY_MIN:
                del _cache[cid]
                removed += 1
            else:
                # 同步 entry
                entry["value"] = v
                # last_bump 不动 — 表示"这是已衰减的值"
        if removed:
            logger.debug("attention_cache.decay_all removed %d entries (now %d)",
                         removed, int(now))
    return removed


def snapshot_stats() -> dict:
    """监控 / 调试用:当前 cache 有几条、合计激活度、最高激活 id。"""
    with _lock:
        items = list(_cache.items())
        if not items:
            return {"entries": 0, "total_activation": 0.0, "top_id": None}
        now = time.time()
        total = sum(_decay_view(e, now) for _, e in items)
        top_id, _ = max(items, key=lambda kv: _decay_view(kv[1], now))
        return {
            "entries": len(items),
            "total_activation": round(total, 2),
            "top_id": top_id,
        }


def reset_for_test() -> None:
    """单测隔离 — 每个测前清干净。"""
    with _lock:
        _cache.clear()


def seed_from_chunks(chunk_ids: Iterable[int], values: Iterable[float]) -> int:
    """启动时从 chunks.activation 列 seed 进 cache(向后兼容旧写入)。
    正常情况下 chunks.activation 应该都是 0(没 agent 跑过 / 重启后清空的),
    seed 这一格只是兜底。
    """
    inserted = 0
    now = time.time()
    with _lock:
        for cid, val in zip(chunk_ids, values):
            if cid < 0 or not val or val <= 0:
                continue
            if cid in _cache:
                continue
            if len(_cache) >= _MAX_ENTRIES:
                break
            _cache[cid] = {
                "value": float(val),
                "last_bump": now,
                "bump_count": 0,
            }
            inserted += 1
    if inserted:
        logger.debug("attention_cache seeded %d entries from chunks.activation",
                     inserted)
    return inserted


# ───── 内部 ─────

def _decay_view(entry: dict, now: float) -> float:
    """计算"已衰减后的当前值" — 半衰期 _DECAY_HALFLIFE_SECONDS。"""
    age = max(0.0, now - entry["last_bump"])
    halvings = age / _DECAY_HALFLIFE_SECONDS
    return entry["value"] * (0.5 ** halvings)


def _evict_oldest_locked() -> None:
    """简单 FIFO 兜底,删前 100 条 last_bump 最老的。"""
    items = sorted(_cache.items(), key=lambda kv: kv[1]["last_bump"])
    for cid, _ in items[:100]:
        _cache.pop(cid, None)


__all__ = [
    "bump_activation",
    "get_activation",
    "get_activations",
    "decay_all",
    "snapshot_stats",
    "reset_for_test",
    "seed_from_chunks",
]
