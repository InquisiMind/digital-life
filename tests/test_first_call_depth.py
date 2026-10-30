"""首轮速答降档（first_call_depth）机制测试。

语音会话事件带 quick_first_reply → scheduler 传 first_call_depth="quick" →
本 wake 第一次 LLM 调用降档，之后的调用回到人的基本档。
"""
from __future__ import annotations

from infrastructure.ai.agent import AIAgent


def _bare_agent(depth: str, first_call_depth: str = "") -> AIAgent:
    """绕过 __post_init__（工具装载/目录创建），只喂本机制用到的两个属性。"""
    agent = AIAgent.__new__(AIAgent)
    agent.reasoning_config = {"depth": depth}
    agent._first_call_depth_pending = (first_call_depth or "").strip()
    return agent


def test_first_call_quick_then_standard():
    """第一次调用 quick（速答），第二次起回基本档（干活可以思考）。"""
    agent = _bare_agent("standard", first_call_depth="quick")
    first = agent._effective_reasoning_config()
    assert first == {"depth": "quick"}
    second = agent._effective_reasoning_config()
    assert second == {"depth": "standard"}
    third = agent._effective_reasoning_config()
    assert third == {"depth": "standard"}


def test_no_override_uses_base_depth():
    agent = _bare_agent("deep")
    assert agent._effective_reasoning_config() == {"depth": "deep"}
    assert agent._effective_reasoning_config() == {"depth": "deep"}


def test_override_keeps_depth_map():
    """覆盖只改 depth；配置层的 depth_map（网关值域覆盖）原样保留。"""
    agent = _bare_agent("standard", first_call_depth="quick")
    agent.reasoning_config = {"depth": "standard", "depth_map": {"deep": "max"}}
    out = agent._effective_reasoning_config()
    assert out == {"depth": "quick", "depth_map": {"deep": "max"}}


def test_scheduler_flag_scan_shape():
    """scheduler 的 payload 扫描语义：任一待唤醒事件带 quick_first_reply 即命中。"""
    events = [
        {"payload": {"reply_channel": "voice"}},
        {"payload": {"quick_first_reply": True, "reply_channel": "voice"}},
    ]
    assert any(bool((ev.get("payload") or {}).get("quick_first_reply")) for ev in events)
    assert not any(
        bool((ev.get("payload") or {}).get("quick_first_reply"))
        for ev in [{"payload": {}}, {}]
    )
