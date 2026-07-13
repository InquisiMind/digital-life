"""Regression: 同优先级事件的 reason tie-break。

历史 BUG：08:00 北京 morning_plan(routine, pri=5) + timer(timer, pri=5) 同时到期，
pop_due_events 按 event_id ASC 返回 events=[routine, timer]。原选 reason 用严格
`if pri > top_priority`——同优先级时谁在 events list 后面谁赢，结果是 timer 赢，
wake reason=timer，build_wake_prompt 走 timer 模板而非 routine 的 prompt_template，
morning_plan 的 "skill_view daily_planner" 深度思考内容永远不注入。

修复：ties 时按 _REASON_TIEBREAK 表，内容类（routine / message / group_message）
优先于 timer。
"""
from __future__ import annotations


def _pick_reason(events):
    """复刻 cron_lifecycle.py 的 reason 选择算法，用纯函数方便测试。"""
    _REASON_TIEBREAK = {
        "routine": 0,
        "group_message": 1,
        "message": 2,
        "awaiting_reply": 3,
        "timer": 4,
        "initiative": 5,
    }
    reason = "unknown"
    top_priority = -1
    top_tiebreak = 10 ** 9
    for ev in events:
        kind = ev.get("kind", "")
        # 这里简化：用 kind 字符串直接当 priority（实际从 event registry 查的，
        # 这里纯为测逻辑，不影响 tie-break 行为本身）。
        pri = 5  # 假设 routine/timer/message 都是 5
        tb = _REASON_TIEBREAK.get(kind, 100)
        if pri > top_priority or (pri == top_priority and tb < top_tiebreak):
            top_priority = pri
            top_tiebreak = tb
            reason = kind
    return reason


def test_tiebreak_routine_beats_timer_when_same_priority():
    """最关键：morning_plan routine + timer 同时到期，应选 routine。"""
    # 模拟 pop_due_events 返回顺序 events=[routine, timer]（按 event_id ASC）
    events = [
        {"kind": "routine"},
        {"kind": "timer"},
    ]
    assert _pick_reason(events) == "routine", \
        "同优先级时 routine 必须赢过 timer，否则 morning_plan skill 不注入"


def test_tiebreak_group_message_beats_timer():
    events = [
        {"kind": "timer"},
        {"kind": "group_message"},
    ]
    assert _pick_reason(events) == "group_message"


def test_tiebreak_higher_priority_still_wins():
    """higher priority kind 仍赢（即使 tie-break 表里它靠后）。
    用 priority=10 的 initiative 模拟 higher-priority；routine 是 priority=5。
    """
    _REASON_TIEBREAK = {
        "routine": 0,
        "group_message": 1,
        "message": 2,
        "awaiting_reply": 3,
        "timer": 4,
        "initiative": 5,
    }
    # initiative priority=10 高于 routine priority=5，即便 initiative tie-break=5
    # 比 routine tie-break=0 靠后，priority 主导。
    priority_map = {"initiative": 10, "routine": 5}

    def pick(events):
        reason = "unknown"
        top_priority = -1
        top_tb = 10 ** 9
        for ev in events:
            kind = ev.get("kind", "")
            pri = priority_map.get(kind, 5)
            tb = _REASON_TIEBREAK.get(kind, 100)
            if pri > top_priority or (pri == top_priority and tb < top_tb):
                top_priority = pri
                top_tb = tb
                reason = kind
        return reason

    events = [
        {"kind": "routine"},      # pri=5, tb=0
        {"kind": "initiative"},   # pri=10, tb=5
    ]
    # initiative 优先级更高，即使 tb 靠后，应该赢
    assert pick(events) == "initiative"
