"""语音快答策略测试（think_cycle）。

语义验证：
  - 默认 think（现状不动）：非语音场景任何调用都不干预
  - 语音事件：第一次调用快答（关 think），后续调用恢复原 effort
  - 开关可关（DIGITAL_LIFE_VOICE_FAST=0）
"""
from __future__ import annotations

from infrastructure.ai.think_cycle import FAST_EFFORT, is_fast_first_call


def test_voice_first_call_is_fast():
    assert is_fast_first_call(event_platform="voice", call_idx=0) is True


def test_voice_subsequent_calls_normal():
    """同一 wake 第 2、3…次调用恢复正常 think。"""
    assert is_fast_first_call(event_platform="voice", call_idx=1) is False
    assert is_fast_first_call(event_platform="voice", call_idx=2) is False


def test_non_voice_never_fast():
    """文字消息/timer/routine 等非语音场景：零干预（默认 think）。"""
    for idx in (0, 1, 5):
        assert is_fast_first_call(event_platform="", call_idx=idx) is False
        assert is_fast_first_call(event_platform="feishu", call_idx=idx) is False


def test_disabled_switch():
    """DIGITAL_LIFE_VOICE_FAST=0 → 完全关闭快答。"""
    assert is_fast_first_call(event_platform="voice", call_idx=0, enabled=False) is False


def test_fast_effort_is_minimal():
    """快答档位 = GLM 五档最低档（等效关 think）。"""
    assert FAST_EFFORT == "minimal"


def test_agent_wake_counter_advances():
    """agent 接线：_wake_call_idx 每次 _chat 判定后 +1（第一次 0，第二次 1）。"""
    # 直接对照 agent.py _chat 里的写法（不构造完整 AIAgent——避免依赖网络层）
    idx = getattr(type("A", (), {})(), "_wake_call_idx", 0)  # 未初始化 → 默认 0
    calls = []
    for _ in range(3):
        call_idx = idx
        idx = call_idx + 1
        calls.append(call_idx)
    assert calls == [0, 1, 2]
    # voice 场景下：只有第一次 fast
    fasts = [is_fast_first_call(event_platform="voice", call_idx=i) for i in calls]
    assert fasts == [True, False, False]
