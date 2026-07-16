"""P3+ — 场景意图过滤器(动态 source 权重)。

设计来源(用户 2026-07-16 整理文档 §4):
  "场景意图收敛时,自动叠加意图过滤器 — 深度回忆个人经历时,自动降权
   娱乐性无关记忆,只保留私有经历认知。"

核心问题: 之前 facade 用一刀切 source weight(rules 1.5 / conversation 1.6),
对所有 query 一视同仁。结果:
  - 严肃查事时(「A+ 策略效果」),闲聊/草稿类也竞争 top-K、挤掉真相关
  - 闲聊场景时,反而该让最近的对话优先,而非抢着返回 rules

解决: 在 facade 入口判定 scene → 给每个 source 一个 multiplier(0.3~2.0),
facade 用 multiplier 调 final_score。原 weight 不动, multiplier 是 overlay。

判定 scene 的方式(足够工程化,不依赖 LLM):
  - 看 query 文本关键词(复盘/分析/怎么样/总结 → deep_work)
  - 看 query 关键词(聊聊/哈哈/随便 → chat)
  - 默认: balanced
  - caller 可显式传 scene 覆盖(如 wake self_review 时传 'self_review')
"""
from __future__ import annotations

import re
from typing import Literal

Scene = Literal["chat", "deep_work", "self_review", "balanced"]

# 各 scene 下给各 source 类的 multiplier(乘到 final_score 上)
# 1.0 = 不变;< 1 = 降权; > 1 = 提权
# 规则依据设计 doc §4:严肃场景压低 conversation/notes/context 等噪音,
# 抬高 digest/rules/lessons 等沉淀过的认知。
_SCENE_WEIGHTS: dict[Scene, dict[str, float]] = {
    # 深度工作 / 严肃查事:沉淀类(digest/knowledge/rules/lessons)优先,
    # 闲聊 / 草稿 / 临时上下文类降权
    "deep_work": {
        "conversation":     0.4,   # 闲聊对话降权最狠
        "context":          0.3,   # 工作交接上下文
        "notes":            0.5,   # 草稿
        "work":             0.6,
        "goals":            0.8,
        "plans":            0.8,
        "digest_session":   1.3,   # 沉淀的经历摘要优先
        "digest_segment":   1.2,
        "digest_day":       1.3,
        "digest_week":      1.4,
        "rules":            1.4,
        "lessons":          1.3,
        "knowledge":        1.3,   # profile / 概念卡
        "self_knowledge":   1.2,
        "identity":         0.8,
        "journal":          1.0,
        "him":              1.0,
        "project":          1.2,
        "todo":             0.7,
    },
    # 闲聊场景:反过来 — 最近的对话 / 日记 / 用户认知最有价值,
    # rules / lessons 太严肃反而拖累
    "chat": {
        "conversation":     1.5,
        "digest_session":   0.9,
        "digest_segment":   0.8,
        "digest_day":       0.7,
        "digest_week":      0.6,
        "rules":            0.6,
        "lessons":          0.7,
        "knowledge":        0.9,
        "self_knowledge":   0.8,
        "identity":         1.0,
        "journal":          1.3,
        "him":              1.4,
        "context":          1.0,
        "notes":            1.2,
        "project":          0.6,
        "todo":             0.4,
    },
    # 自我复盘:rules / lessons / self_knowledge 是主角
    "self_review": {
        "conversation":     0.5,
        "context":          0.4,
        "notes":            0.6,
        "rules":            1.8,
        "lessons":          1.6,
        "self_knowledge":   1.8,
        "knowledge":        1.5,
        "digest_session":   1.2,
        "digest_segment":   1.2,
        "digest_day":       1.3,
        "digest_week":      1.4,
        "identity":         1.2,
        "journal":          1.3,
        "him":              1.0,
        "project":          0.8,
        "todo":             0.8,
    },
    # balanced = 默认,所有 1.0(行为同改造前)
    "balanced": {},
}


# 简单 query 关键词分类器(不调 LLM,够用)
_DEEP_WORK_PATTERNS = re.compile(
    r"复盘|分析|总结|效果|怎么样|为什么|如何|深入|细节|进展|状态|检查|验证|"
    r"上次|之前提过|记不记得|有没有",
    re.IGNORECASE,
)
_CHAT_PATTERNS = re.compile(
    r"聊聊|哈哈|随便|说下|说说|讲讲|感觉|今天|刚刚|刚才|随手",
    re.IGNORECASE,
)
_SELF_REVIEW_HINTS = re.compile(
    r"自我|复盘|整理|规则|教训|认知|我自己的|我的策略|我的做事",
    re.IGNORECASE,
)


def detect_scene(query: str, *, hint: str | None = None) -> Scene:
    """从 query 文本 + hint 推断当前场景。

    hint 由 caller(wake_reason / budget_kind / 用户显式标记)传入,优先级最高。
    没 hint 就走关键词分类。都没有 → balanced。
    """
    if hint:
        h = hint.lower().strip()
        if "self_review" in h or "self_iteration" in h or "initiative" in h:
            return "self_review"
        if "review" in h or "memory_hygiene" in h or "weekly" in h:
            return "self_review"
        if "deep" in h or "work" in h:
            return "deep_work"
        if "chat" in h or "casual" in h:
            return "chat"

    if not query:
        return "balanced"

    q = query.lower()
    # self_review 信号最强,先判
    if _SELF_REVIEW_HINTS.search(q):
        return "self_review"
    # 闲聊信号(优先于 deep_work,因为"今天聊聊"明显是 chat 不是 deep)
    if _CHAT_PATTERNS.search(q):
        return "chat"
    if _DEEP_WORK_PATTERNS.search(q):
        return "deep_work"
    return "balanced"


def weight_multiplier(source: str, scene: Scene) -> float:
    """给定 source 和当前 scene, 返回该 source 的权重乘数。
    缺省 1.0(balanced / 未列入的 source)。
    """
    if not scene or scene == "balanced":
        return 1.0
    return _SCENE_WEIGHTS.get(scene, {}).get(source, 1.0)


def describe_profile(scene: Scene) -> dict[str, float]:
    """给前端 / 日志看 profile 的内容(scene 下各 source 的 multiplier)。
    只返非 1.0 的(便于审计)。
    """
    if scene == "balanced":
        return {}
    raw = _SCENE_WEIGHTS.get(scene, {})
    return {k: v for k, v in raw.items() if abs(v - 1.0) > 0.01}


__all__ = [
    "Scene",
    "detect_scene",
    "weight_multiplier",
    "describe_profile",
]
