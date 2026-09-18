"""effort 状态机 v3 测试（think_cycle）。

语义验证（对应 think_effort_design.md 三层入口）：
  A 入口 - 人类消息快答：wake/mid-session 注入后第一次调用 minimal（关 think），后续恢复 config 档
  B 入口 - think 工具：模型主动升档，TTL 轮数耗尽自动回落；新人类消息打断后 fast_pending 保留
  C 入口 - stuck 兜底：连续 2 轮基础设施异常（工具异常/超时信号词）→ 强制 high；
          业务错误（工具正常返回失败语义）不算 stuck；通信类工具豁免
"""
from __future__ import annotations

import infrastructure.ai.think_cycle as tc


def _new():
    return tc.new_state()


# ── A 入口：人类消息快答 ──────────────────────────

def test_a1_wake_first_call_fast():
    st = _new()
    tc.init_for_wake(st, human_message=True)
    effort, why = tc.decide(st, config_effort="medium")
    assert effort == "minimal" and why == "fast_first_reply"


def test_a2_second_call_back_to_config():
    st = _new()
    tc.init_for_wake(st, human_message=True)
    tc.decide(st, config_effort="medium")
    effort, why = tc.decide(st, config_effort="medium")
    assert effort == "medium" and why == "config"


def test_a3_mid_session_inject_fast():
    """mid-session 人类消息注入（on_human_message）→ 下一次调用快答。"""
    st = _new()
    tc.init_for_wake(st, human_message=False)  # timer 唤醒，无人类消息
    tc.on_human_message(st)
    effort, why = tc.decide(st, config_effort="medium")
    assert effort == "minimal" and why == "fast_first_reply"


def test_a4_non_human_wake_untouched():
    """timer/routine 唤醒：零干预，直接 config 档。"""
    st = _new()
    tc.init_for_wake(st, human_message=False)
    effort, why = tc.decide(st, config_effort="high")
    assert effort == "high" and why == "config"


# ── B 入口：think 工具 override ────────────────────

def test_b1_override_beats_config():
    st = _new()
    tc.set_active_state(st)  # B 入口走 active_state 桥, 先绑定
    tc.init_for_wake(st, human_message=False)
    tc.request_override(effort="high", ttl=2)
    effort, why = tc.decide(st, config_effort="low")
    assert effort == "high" and why == "think_override"


def test_b2_ttl_expiry_falls_back():
    st = _new()
    tc.set_active_state(st)  # B 入口走 active_state 桥, 先绑定
    tc.init_for_wake(st, human_message=False)
    tc.request_override(effort="high", ttl=2)
    tc.decide(st, config_effort="medium")  # 消耗轮 1
    tc.decide(st, config_effort="medium")  # 消耗轮 2
    effort, why = tc.decide(st, config_effort="medium")
    assert effort == "medium" and why == "config"
    assert st["override"] is None


def test_b3_clear_override():
    st = _new()
    tc.set_active_state(st)  # B 入口走 active_state 桥, 先绑定
    tc.request_override(effort="high", ttl=5)
    tc.clear_override()
    assert st["override"] is None


def test_b4_human_interrupt_keeps_override_then_fast_pending():
    """人类消息打断 think_override：override 保留（B>A），轮耗尽后 fast_pending 兜底快答。"""
    st = _new()
    tc.set_active_state(st)  # B 入口走 active_state 桥, 先绑定
    tc.init_for_wake(st, human_message=False)
    tc.request_override(effort="high", ttl=1)
    tc.on_human_message(st)
    effort, why = tc.decide(st, config_effort="medium")
    assert effort == "high" and why == "think_override"  # override 优先于快答
    effort, why = tc.decide(st, config_effort="medium")  # TTL 耗尽
    assert effort == "minimal" and why == "fast_first_reply"  # fast_pending 兜底


# ── C 入口：stuck 兜底 ────────────────────────────

def test_c1_consecutive_infra_errors_escalate():
    st = _new()
    tc.init_for_wake(st, human_message=False)
    tc.on_tool_result(st, "terminal", {"ok": False, "error": "timeout"})
    tc.on_tool_result(st, "execute_code", {"ok": False, "error": "connection refused"})
    effort, why = tc.decide(st, config_effort="low")
    assert effort == "high" and why == "stuck_escalation"


def test_c2_business_error_not_stuck():
    """工具正常返回失败语义（业务错误）不算基础设施异常。"""
    st = _new()
    tc.init_for_wake(st, human_message=False)
    tc.on_tool_result(st, "app_stock_quote", {"ok": False, "error": "市场未开盘"})
    tc.on_tool_result(st, "app_stock_quote", {"ok": False, "error": "代码不存在"})
    effort, why = tc.decide(st, config_effort="medium")
    assert effort == "medium" and why == "config"


def test_c3_communication_tools_exempt():
    """通信类工具（express/social）异常豁免——外网波动不代表模型卡住。"""
    st = _new()
    tc.init_for_wake(st, human_message=False)
    tc.on_tool_result(st, "express_to_human", {"ok": False, "error": "timeout"})
    tc.on_tool_result(st, "sense_social_feed", {"ok": False, "error": "timeout"})
    effort, why = tc.decide(st, config_effort="medium")
    assert effort == "medium" and why == "config"


def test_c4_human_message_resets_stuck_counter():
    st = _new()
    tc.init_for_wake(st, human_message=False)
    tc.on_tool_result(st, "terminal", {"ok": False, "error": "timeout"})
    tc.on_human_message(st)  # 打断: 重置 stuck + 挂起 fast
    tc.on_tool_result(st, "terminal", {"ok": False, "error": "timeout"})
    e1, _ = tc.decide(st, config_effort="medium")
    assert e1 == "minimal"  # A 入口: 人类消息后第一次调用 = 快答
    e2, why2 = tc.decide(st, config_effort="medium")
    assert e2 == "medium" and why2 == "config"  # stuck 已被重置, 只 1 轮异常不够升级


# ── 桥接与注册 ────────────────────────────────────

def test_d1_think_tool_registered():
    from interfaces.tools.registry import registry
    names = registry.get_all_tool_names()
    assert "think" in names


def test_d2_priority_c_overrides_a_then_default():
    """优先级综合：C > B > A > config default。"""
    st = _new()
    tc.set_active_state(st)  # B 入口走 active_state 桥, 先绑定
    tc.init_for_wake(st, human_message=True)  # A 挂起
    tc.on_tool_result(st, "terminal", {"ok": False, "error": "timeout"})
    tc.on_tool_result(st, "terminal", {"ok": False, "error": "timeout"})  # C 触发
    tc.request_override(effort="low", ttl=3)  # B 试图降档
    effort, why = tc.decide(st, config_effort="medium")
    assert effort == "high" and why == "stuck_escalation"  # C 最高优先
