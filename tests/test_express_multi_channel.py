"""express_to_human 多通道扇出测试。

验证 channels 参数的扇出语义（mock _express_one，不触网络）：
  - channels 缺省/字符串 → 单通道原路径透传
  - channels 数组 → 依次扇出，汇总结果
  - 任一通道被拦截（sent=False + 拦截上下文）→ 中止剩余 + 返回拦截原语
  - 普通发送失败 → 不影响其他通道继续
"""
from __future__ import annotations

import json
from unittest.mock import patch

from interfaces.tools import action_tools as at


def _run(args):
    out = at._handle_express_to_human(args, session_id="test-sess")
    return json.loads(out) if isinstance(out, str) else out


def test_no_channels_goes_single_path():
    """缺省 channels → 直接调 _express_one（单通道原路径）。"""
    with patch.object(at, "_express_one", return_value='{"sent": true, "channel": "x"}') as m:
        out = _run({"text": "hi", "channel": "feishu:dm:ou_1"})
        m.assert_called_once()
        assert out["sent"] is True


def test_channels_string_treated_as_single():
    with patch.object(at, "_express_one", return_value='{"sent": true}') as m:
        _run({"text": "hi", "channels": "voice:speaker"})
        m.assert_called_once()


def test_multi_channel_fanout_all_ok():
    """两个通道都成功 → 汇总 sent + 顺序扇出。"""
    calls = []

    def fake(args, channel, **ctx):
        calls.append(channel)
        return json.dumps({"sent": True, "channel": channel})

    with patch.object(at, "_express_one", side_effect=fake):
        out = _run({"text": "hi", "channels": ["voice:speaker", "feishu:dm:ou_1"]})
    assert calls == ["voice:speaker", "feishu:dm:ou_1"]
    assert out["sent"] is True and out["multi"] is True
    assert len(out["results"]) == 2


def test_interception_aborts_remaining_channels():
    """第一个通道被拦截 → 剩余通道不发（拦截有事件消费副作用）。"""
    calls = []

    def fake(args, channel, **ctx):
        calls.append(channel)
        if channel == "voice:speaker":
            return json.dumps({"sent": False, "recent_chat_log": "...", "result_summary": "有未读"})
        return json.dumps({"sent": True, "channel": channel})

    with patch.object(at, "_express_one", side_effect=fake):
        out = _run({"text": "hi", "channels": ["voice:speaker", "feishu:dm:ou_1", "feishu:group:oc_2"]})
    assert calls == ["voice:speaker"]  # 只发了第一个
    assert out["sent"] is False
    assert out.get("recent_chat_log")  # 拦截上下文保留（模型要重写）
    assert out["aborted_channels"] == ["feishu:dm:ou_1", "feishu:group:oc_2"]


def test_send_failure_does_not_block_others():
    """第一个通道普通失败（非拦截）→ 后续通道继续发。"""
    calls = []

    def fake(args, channel, **ctx):
        calls.append(channel)
        if channel == "voice:speaker":
            return json.dumps({"sent": False, "error": "TTS down"})
        return json.dumps({"sent": True, "channel": channel})

    with patch.object(at, "_express_one", side_effect=fake):
        out = _run({"text": "hi", "channels": ["voice:speaker", "feishu:dm:ou_1"]})
    assert calls == ["voice:speaker", "feishu:dm:ou_1"]
    assert out["sent"] is True  # 有一路成功
    assert "1/2" in out["result_summary"]


def test_channel_arg_passed_per_iteration():
    """扇出时每轮把对应的 channel 传给 _express_one（且移除 chat_id 抢优先）。"""
    seen = []

    def fake(args, channel, **ctx):
        seen.append((args.get("channel"), channel, "chat_id" in args))
        return json.dumps({"sent": True})

    with patch.object(at, "_express_one", side_effect=fake):
        _run({"text": "hi", "chat_id": "oc_9", "channels": ["voice:speaker", "feishu:group:oc_1"]})
    # 每轮 args.channel 与传入 channel 一致；chat_id 已剥离
    assert seen == [("voice:speaker", "voice:speaker", False),
                    ("feishu:group:oc_1", "feishu:group:oc_1", False)]
