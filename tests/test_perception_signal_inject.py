"""perception_signal 的 mid-session 注入测试（feature 003）。

回归点：perception_signal 必须和 message/group_message 一样走"展示全文 +
自动消费"路径（_inject_signalled_events 的 auto_consume 分支），而不是落到
manual_events 只显示"ID+类型"。否则模型在会话里看不到感知内容。

历史上 perception_signal 一度被当成"其它事件类型"只显示摘要 ID，模型需要
主动调 sense_event_detail 才能看详情——但感知信号已经是预处理好的结构化
理解，应当直接展示。这个测试锁住该行为。
"""
from __future__ import annotations

from infrastructure.ai.agent import _AUTO_CONSUME_SIGNAL_KINDS, _render_signal_message


def test_perception_signal_is_auto_consume_kind():
    """perception_signal 在 auto-consume 集合里。"""
    assert "perception_signal" in _AUTO_CONSUME_SIGNAL_KINDS
    assert "message" in _AUTO_CONSUME_SIGNAL_KINDS
    assert "group_message" in _AUTO_CONSUME_SIGNAL_KINDS


def test_perception_signal_not_in_manual_path():
    """routine/timer 等"真正需要二次查询"的事件不在 auto-consume 里。

    对比确认：perception_signal 的归类和 message 一致，和 routine 不同。
    """
    assert "routine" not in _AUTO_CONSUME_SIGNAL_KINDS
    assert "timer" not in _AUTO_CONSUME_SIGNAL_KINDS


def test_render_perception_signal_uses_yaml_template():
    """渲染 perception_signal 走 yaml 模板：transcript 正文 + 注入头 + 事件 id。

    2026-10-28 语音会话改造：模板从 hotkey 摘要版（summary/media_path/sense_media
    提示）换成语音听觉信号版（{transcript} + {voice_mode_note}）；
    2026-10-30 语音合批再补 {_merged_texts_block}。
    """
    ev = {
        "event_id": 999,
        "kind": "perception_signal",
        "display_name": "人类呼唤",
        "payload": {
            "source": "voice_session",
            "summary": "屏幕显示某股票行情现价10.5元",
            "transcript": "你好小张，帮我看下行情",
            "media_path": "/tmp/cap.png",
            "ok": True,
        },
    }
    out = _render_signal_message(ev)
    # transcript 正文在（不是只显示 ID+类型）
    assert "你好小张，帮我看下行情" in out
    # event id 在
    assert "#999" in out


def test_render_perception_signal_merged_lines_visible():
    """语音合批渲染（2026-10-30）：_merged_texts 的逐句内容+时间标注进注入文本。"""
    ev = {
        "event_id": 1000,
        "kind": "perception_signal",
        "payload": {
            "source": "voice_session",
            "transcript": "今天怎么样",
            "reply_channel": "voice",
            "at": "2026-10-30T11:00:09+08:00",
            "_merged_count": 3,
            "_merged_texts": [
                {"sender": "", "text": "你好", "at": "2026-10-30T11:00:01+08:00"},
                {"sender": "", "text": "你在么", "at": "2026-10-30T11:00:05+08:00"},
            ],
        },
    }
    out = _render_signal_message(ev)
    assert "今天怎么样" in out, "主行 = 最新一句"
    assert "你好" in out and "你在么" in out, "合并历史逐句可见"
    assert "11:00:01" in out and "11:00:05" in out, "逐条时间标注"
    assert "之前合并的 2 条消息" in out


def test_render_perception_signal_includes_transcript_when_present():
    """payload 带 transcript 时，yaml 模板的 {transcript} 占位应被填充。"""
    ev = {
        "event_id": 2,
        "kind": "perception_signal",
        "payload": {
            "source": "hotkey_both",
            "summary": "画面+声音",
            "transcript": "用户说了重要的话",
            "media_path": "/tmp/m.mp4",
        },
    }
    out = _render_signal_message(ev)
    # transcript 内容应出现在渲染结果
    assert "用户说了重要的话" in out
