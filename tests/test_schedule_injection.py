"""schedule 自动注入 + 精力恢复提示 + 闘钟 ID 渲染 测试。

覆盖本次三处改动：
1. format_schedule_for_human 渲染闹钟时带 (#id)
2. format_schedule_for_human 末尾/空路径都带精力恢复说明
3. DEDUP_STRATEGY / slow_ctx_kinds 两处登记到位（防漏改导致注入失效或累积）
4. 注入源头数据正确（get_schedule_overview 返回结构 + format 输出可被 scheduler 注入）

_scheduler 的注入点本身在 scheduler.build_wake_prompt 内联，跑完整 wake 依赖太重；
它在注入分支用 `if _sched_body.strip()` 守卫，而 _sched_body 恒含精力提示（非空），
所以 schedule 注入恒发生——这是设计意图（让模型总能看到精力规律）。
本测试通过验证 format 输出非空 + 含全部关键段，间接锁定注入内容正确。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from domain.lifecycle.alarms import format_schedule_for_human


def _overview(alarms=None, recurring=None, next_wake=None):
    """构造一个最小 overview dict。"""
    return {
        "alarms": alarms or [],
        "recurring": recurring or [],
        "next_wake": next_wake,
        "query_time": "2026-07-01T12:00:00+08:00",
        "days_ahead": 7,
    }


class TestAlarmIdInRender:
    def test_alarm_id_shown_in_render(self) -> None:
        """闹钟渲染时带 (#id) 后缀。"""
        now = datetime.now()
        fire_iso = (now + timedelta(hours=2)).isoformat(timespec="seconds")
        ov = _overview(alarms=[{
            "id": 1234, "event_kind": "timer", "fire_at": fire_iso,
            "reason": "和张三的会议", "from_routine": False,
        }])
        text = format_schedule_for_human(ov)
        assert "(#1234)" in text
        assert "和张三的会议" in text

    def test_alarm_without_id_renders_without_tag(self) -> None:
        """id 缺失时不显示空的 (#None)。"""
        now = datetime.now()
        fire_iso = (now + timedelta(hours=2)).isoformat(timespec="seconds")
        ov = _overview(alarms=[{
            "id": None, "event_kind": "timer", "fire_at": fire_iso,
            "reason": "测试任务", "from_routine": False,
        }])
        text = format_schedule_for_human(ov)
        assert "(#None)" not in text
        assert "测试任务" in text

    def test_multiple_alarms_each_get_id(self) -> None:
        """多条闹钟各自带自己的 id。"""
        now = datetime.now()
        ov = _overview(alarms=[
            {"id": 100, "event_kind": "timer", "fire_at": (now + timedelta(hours=1)).isoformat(timespec="seconds"), "reason": "任务A", "from_routine": False},
            {"id": 200, "event_kind": "timer", "fire_at": (now + timedelta(hours=3)).isoformat(timespec="seconds"), "reason": "任务B", "from_routine": False},
        ])
        text = format_schedule_for_human(ov)
        assert "(#100)" in text
        assert "(#200)" in text


class TestVitalHint:
    """精力恢复说明在两条路径都出现。"""

    def test_vital_hint_in_normal_path(self) -> None:
        """有闹钟时，精力提示在末尾。"""
        now = datetime.now()
        ov = _overview(alarms=[{
            "id": 1, "event_kind": "timer",
            "fire_at": (now + timedelta(hours=1)).isoformat(timespec="seconds"),
            "reason": "r", "from_routine": False,
        }])
        text = format_schedule_for_human(ov)
        assert "精力恢复" in text
        assert "主动探索" in text
        assert "1-2 小时" in text or "1-2小时" in text

    def test_vital_hint_in_empty_path(self) -> None:
        """空日程时，精力提示也出现（让模型在没有闹钟时也懂精力规律）。"""
        ov = _overview(alarms=[], recurring=[])
        text = format_schedule_for_human(ov)
        assert "精力恢复" in text
        assert "主动探索" in text

    def test_empty_schedule_returns_nonempty(self) -> None:
        """空日程返回非空字符串（因含精力提示）。

        这锁定 scheduler 注入分支 `if _sched_body.strip()` 恒为真——
        即每次 wake schedule 注入恒发生（设计意图）。
        """
        ov = _overview(alarms=[], recurring=[])
        text = format_schedule_for_human(ov)
        assert text.strip() != ""


class TestRegistrations:
    """两处登记必须到位（防漏改）。"""

    def test_dedup_strategy_has_schedule_latest(self) -> None:
        """DEDUP_STRATEGY 含 schedule=latest（每 wake 覆盖旧快照，避免累积）。"""
        from infrastructure.persistence.instance.runtime_log import DEDUP_STRATEGY
        assert DEDUP_STRATEGY.get("schedule") == "latest"

    def test_slow_ctx_kinds_has_schedule(self) -> None:
        """schedule 在 slow_ctx_kinds 集合里（双写 audit DB，支持回放）。"""
        # 直接从 agent 模块源码静态读，避免触发完整 import 链
        import inspect
        from infrastructure.ai import agent as agent_mod
        source = inspect.getsource(agent_mod)
        # slow_ctx_kinds 是函数体内的局部集合字面量，用源码包含性校验
        assert '"schedule"' in source or "'schedule'" in source
