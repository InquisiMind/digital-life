"""Token 预算闸门测试（设计文档 二十三章）。

cron 在 pop_due_events 之后、wake dispatch 之前调 should_allow_wake：
  - 小时未超 → 允许
  - 小时超阈值 → 拒绝（除非是 HIGH_PRIORITY_KINDS 中的真人消息）
  - 日超阈值 → 拒绝（同样高优先级穿透）
  - 拒绝时事件被推到下个窗口（delay_pending_events，5 min backoff）

产品语义：数字生命醒来时系统先问"这个小时还能烧么？"
"""
from __future__ import annotations

import os
from unittest.mock import patch


def test_under_budget_allowed():
    """预算未超 → 任何 reason 都允许。"""
    from infrastructure.budget import should_allow_wake, get_budget_state
    # 默认 budget：hour 20万, day 200万. Tracker 没数据 → used 0 → 放行
    for r in ("timer", "routine", "initiative", "task_reminder", "message",
              "group_message", "birth"):
        ok, reason, _ = should_allow_wake(r, "test-budget1")
        assert ok is True, f"{r} 应允许, reason={reason}"


def test_hour_limit_blocks_non_priority():
    """小时超阈值 → 非 HIGH_PRIORITY 拒绝；HIGH_PRIORITY 穿透。"""
    from infrastructure.budget import should_allow_wake
    from infrastructure.budget.budget_gate import get_budget_state, BudgetState

    # mock: hour 已超额
    fake_state = BudgetState(
        hour_used=250_000, hour_limit=200_000,
        day_used=500_000, day_limit=2_000_000,
        hour_resets_at="2030-01-01T14:00",
        day_resets_at="2030-01-02T00:00",
    )
    with patch(
        "infrastructure.budget.budget_gate.get_budget_state",
        return_value=fake_state,
    ):
        # 普通 reason 被拒
        ok, reason, _ = should_allow_wake("timer", "x")
        assert ok is False
        assert "hourly" in reason

        # 高优先级穿透
        ok2, reason2, _ = should_allow_wake("message", "x")
        assert ok2 is True
        ok3, reason3, _ = should_allow_wake("group_message", "x")
        assert ok3 is True
        ok4, reason4, _ = should_allow_wake("birth", "x")
        assert ok4 is True


def test_daily_limit_blocks_non_priority():
    """日超阈值 → 同样拒绝 + HIGH_PRIORITY 穿透。"""
    from infrastructure.budget import should_allow_wake
    from infrastructure.budget.budget_gate import BudgetState

    fake = BudgetState(
        hour_used=100_000, hour_limit=200_000,
        day_used=2_500_000, day_limit=2_000_000,
        hour_resets_at="x", day_resets_at="x",
    )
    with patch(
        "infrastructure.budget.budget_gate.get_budget_state",
        return_value=fake,
    ):
        ok, reason, _ = should_allow_wake("timer", "x")
        assert ok is False
        assert "daily" in reason

        # 真人消息穿透
        ok2, _, _ = should_allow_wake("message", "x")
        assert ok2 is True


def test_env_overrides_limits(tmp_path, monkeypatch):
    """DIGITAL_LIFE_TOKEN_HOURLY_LIMIT / DAILY_LIMIT 可以 override 默认值。"""
    monkeypatch.setenv("DIGITAL_LIFE_TOKEN_HOURLY_LIMIT", "999")
    monkeypatch.setenv("DIGITAL_LIFE_TOKEN_DAILY_LIMIT", "9999")
    from infrastructure.budget.budget_gate import _limits
    h, d = _limits()
    assert h == 999
    assert d == 9999


def test_budget_state_set_1_instance_doesnt_leak_to_another(tmp_path):
    """不同 instance_id 的 token 使用量互不影响（多实例并行）。"""
    from infrastructure.budget.token_tracker import (
        TokenUsageTracker, reset_token_tracker_for_test,
    )
    reset_token_tracker_for_test()

    t = TokenUsageTracker(tmp_path / "state.db")
    t.record(instance_id="A", input_tokens=100_000, output_tokens=10_000)
    # 实例 A 烧了 11 万，B 是 0
    assert t.usage_last_hour("A") == 110_000
    assert t.usage_last_hour("B") == 0
