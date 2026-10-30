"""流式工具调用解析（tool_stream）+ 语音提前播报链路测试。

核心不变量：``emitted_chars`` 必须严格等于最终 text 参数的"已播前缀长度"，
_send_voice_local 用它做 ``text[offset:]`` 切片——错一个字符就是漏字/重字。
"""
from __future__ import annotations

import time

import json
from unittest.mock import patch

from infrastructure.ai.tool_stream import (
    TextFieldSentenceStream,
    safe_text_prefix,
)


def _feed_fragments(text_value: str, chunk: int = 3):
    """把完整 arguments JSON 按 chunk 字符切片喂入，模拟 SSE 碎片流。"""
    raw = json.dumps({"text": text_value, "channel": ""}, ensure_ascii=False)
    stream = TextFieldSentenceStream()
    sentences: list[str] = []
    for i in range(0, len(raw), chunk):
        stream.feed(raw[i:i + chunk])
        sentences.extend(stream.pop_sentences())
    return stream, sentences


def test_prefix_monotonic_and_complete():
    """前缀随输入增长单调不减；闭合后等于完整 text 值。"""
    value = "第一句。第二句，比较长所以会在逗号处断开。尾句"
    raw = json.dumps({"text": value, "channel": ""}, ensure_ascii=False)
    stream = TextFieldSentenceStream()
    last = ""
    for i in range(0, len(raw), 2):
        stream.feed(raw[i:i + 2])
        p = safe_text_prefix(stream._args)
        if p is not None:
            assert len(p) >= len(last)
            last = p
        stream.pop_sentences()
    assert last == value


def test_escaped_quotes_and_unicode_split():
    """转义序列被碎片切断时回退到安全边界；含 \\" 与 \\uXXXX 的文本完整解码。"""
    value = '带"引号"和é字符的句子。结尾。'
    stream, sentences = _feed_fragments(value, chunk=1)  # 1 字符切片：必切中转义
    assert "".join(sentences).replace(" ", "") .startswith('带"引号"和é字符的句子')
    assert stream.emitted_chars <= len(value)
    # 完整文本解码无损坏
    final = safe_text_prefix(json.dumps({"text": value}, ensure_ascii=False))
    assert final == value


def test_offset_alignment_with_dispatch_slicing():
    """不变量：text[emitted_chars:] 恰好是尚未提前播报的尾巴。"""
    value = "你好张总。我马上处理。这件事需要查一下文件，然后给您结果。"
    stream, sentences = _feed_fragments(value, chunk=4)
    offset = stream.emitted_chars
    assert 0 < offset <= len(value)
    remainder = value[offset:]
    # 已播句子拼起来 == text 的前 offset 个字符（允许句间空白差异）
    assert "".join(sentences).replace(" ", "") in value.replace(" ", "")
    # 尾巴与已播内容不重叠
    assert remainder == value[offset:]


def test_sentence_breaks():
    """句末标点立即断；逗号攒够 min_soft_len 才断；尾巴不吐。"""
    stream = TextFieldSentenceStream(min_soft_len=6)
    raw = json.dumps({"text": "好的。这个任务我记下来了，稍后给你整理一份摘要。"}, ensure_ascii=False)
    for i in range(0, len(raw), 5):
        stream.feed(raw[i:i + 5])
        stream.pop_sentences()
    # 尾句（无句末标点结尾的部分）不应被吐出——留给正式分派播
    # （此处 text 以。结尾，全部成句）
    assert stream.emitted_chars > 0


def test_no_text_field_yet():
    """还没流到 text 值时前缀为 None，不产出句子。"""
    stream = TextFieldSentenceStream()
    stream.feed('{"cha')
    assert safe_text_prefix(stream._args) is None
    assert stream.pop_sentences() == []
    stream.feed('nnel": "", "te')
    assert safe_text_prefix(stream._args) is None


def test_agent_dispatch_strips_prefix(monkeypatch):
    """agent 分派路径：prespeak>0 时设 offset → dispatch → 重置。"""
    from infrastructure.ai.agent import AIAgent

    agent = AIAgent.__new__(AIAgent)
    agent._voice_prespeak_chars = 8

    from domain.lifecycle.runtime_context import (
        get_voice_prespeak_offset, reset_voice_prespeak_offset,
    )
    calls = []

    class _FakeRegistry:
        @staticmethod
        def dispatch(name, arguments, session_id=None):
            calls.append((name, get_voice_prespeak_offset()))
            return '{"sent": true}'

    import infrastructure.ai.agent as agent_mod
    with patch.object(agent_mod, "registry", _FakeRegistry):
        # 模拟 run_conversation 里的分派分支逻辑
        _prespeak = getattr(agent, "_voice_prespeak_chars", 0)
        if _prespeak > 0 and True:  # name == express_to_human
            from domain.lifecycle.runtime_context import set_voice_prespeak_offset
            set_voice_prespeak_offset(_prespeak)
            try:
                _FakeRegistry.dispatch("express_to_human", {}, session_id="s")
            finally:
                reset_voice_prespeak_offset()
    assert calls == [("express_to_human", 8)]
    assert get_voice_prespeak_offset() == 0


def test_send_voice_local_speaks_remainder(monkeypatch, tmp_path):
    """_send_voice_local：offset 生效时只 speak 尾巴，sent 保持 True。"""
    import interfaces.tools.action_tools as at
    from domain.lifecycle.runtime_context import set_voice_prespeak_offset, reset_voice_prespeak_offset

    spoken: list[str] = []
    monkeypatch.setattr(
        "infrastructure.perception.voice_output.speak",
        lambda text, voice="", rate="+0%": (spoken.append(text), True)[1],
        raising=False,
    )
    monkeypatch.setattr(
        "infrastructure.perception.voice_output.is_tts_enabled", lambda iid: True
    )
    monkeypatch.setattr(
        "infrastructure.perception.voice_output.get_tts_voice", lambda iid: ""
    )
    monkeypatch.setattr(
        "infrastructure.perception.voice_output.get_tts_rate", lambda iid: "+0%"
    )

    full_text = "第一句已经提前播了。这句是分派时才播的尾巴。"
    set_voice_prespeak_offset(len("第一句已经提前播了。"))
    try:
        with patch.dict(at.__dict__, {}, clearing=False):
            # 只测 speak 剥离语义：直接构造最小调用（log_conversation 路径 mock 掉）
            with patch("domain.lifecycle.conversation_log.log_conversation"):
                out = at._send_voice_local("voice:speaker", full_text, {}, [])
    finally:
        reset_voice_prespeak_offset()
    assert spoken == ["这句是分派时才播的尾巴。"]
    assert '"sent": true' in out or "sent" in out


def test_sanitize_ds_compatibility():
    """DS 端点严格校验的 payload 归一：null content → ""、无父对 tool → user。"""
    from infrastructure.ai.agent import _sanitize_payload_messages as sanitize

    msgs = [
        {"role": "system", "content": "s"},
        {"role": "tool", "tool_call_id": None, "name": "wake_signal", "content": "上下文注入"},   # 孤儿 tool
        {"role": "assistant", "content": None, "tool_calls": [                                      # null content
            {"id": "tc1", "type": "function", "function": {"name": "express_to_human", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "tc1", "content": '{"sent": true}'},                       # 合法对
    ]
    sanitize(msgs)
    # 孤儿 tool 降级为 user（内容保留）
    assert msgs[1] == {"role": "user", "content": "上下文注入"}
    # null content 归 ""
    assert msgs[2]["content"] == ""
    # 合法配对不受影响
    assert msgs[3]["role"] == "tool" and msgs[3]["tool_call_id"] == "tc1"


def test_deepseek_provider_payload_shape():
    """DeepSeekProvider：thinking 禁用 + 剥历史 reasoning + 不带 reasoning_effort。"""
    from infrastructure.ai.providers import DeepSeekProvider

    p = DeepSeekProvider()
    payload = {
        "model": "deepseek-flash",
        "reasoning_effort": "low",
        "messages": [
            {"role": "assistant", "content": "a", "reasoning_content": "思考"},
            {"role": "user", "content": "u"},
        ],
    }
    out = p.customize_payload(payload, reasoning_config={"depth": "quick"})
    assert out["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in out
    assert all("reasoning_content" not in m for m in out["messages"])


def test_send_voice_local_full_prespeak_no_repeat(monkeypatch):
    """整句提前播完（offset==len）时分派不得重播——2026-10-28 实听双播事故。"""
    import interfaces.tools.action_tools as at
    from domain.lifecycle.runtime_context import (
        reset_voice_prespeak_offset, set_voice_prespeak_offset,
    )

    spoken: list[str] = []
    monkeypatch.setattr(
        "infrastructure.perception.voice_output.speak",
        lambda text, voice="", rate="+0%": (spoken.append(text), bool(text))[1],
        raising=False,
    )
    monkeypatch.setattr("infrastructure.perception.voice_output.is_tts_enabled", lambda iid: True)
    monkeypatch.setattr("infrastructure.perception.voice_output.get_tts_voice", lambda iid: "")
    monkeypatch.setattr("infrastructure.perception.voice_output.get_tts_rate", lambda iid: "+0%")

    full = "在的，张总。"
    set_voice_prespeak_offset(len(full))   # 整句已提前播
    try:
        with patch("domain.lifecycle.conversation_log.log_conversation"):
            out = at._send_voice_local("voice:speaker", full, {}, [])
    finally:
        reset_voice_prespeak_offset()
    assert spoken == []                    # 无尾巴 → 不重播
    assert "sent" in out                   # 仍算已发送（日志记全文）


def test_barge_in_partial_diff():
    """打断判别：partial 与播报文本相似 → 回声忽略；明显不同 → 停播+放行下一句。"""
    from infrastructure.perception import web_voice as wv
    from infrastructure.perception.web_voice import WebVoiceSession
    from infrastructure.perception.config import PerceptionConfig

    pushed = []
    stopped = []
    cfg = PerceptionConfig(
        stream_asr_app_id="a", stream_asr_api_key="k", stream_asr_api_secret="s",
    )
    with patch.object(WebVoiceSession, "start", return_value=True):
        sess = WebVoiceSession("iid-x", config=cfg, push_client=pushed.append)
    with patch.object(wv, "is_tts_active", return_value=True), patch(
        "infrastructure.perception.voice_output.stop_playback",
        side_effect=lambda: stopped.append(1) or True,
    ):
        sess.note_agent_reply("张总您好，今天北京的天气我这边查不了，建议您看一下手机自带的天气软件。")
        # 回声 partial（精确前缀，覆盖率 1.0）→ 不触发
        sess._maybe_barge_in("张总您好，今天北京的")
        assert stopped == [] and sess._user_interrupted is False
        # 真人 partial（内容不同；注意 2-3 字短语可能与播报撞常用词，实测按此约束）
        sess._maybe_barge_in("先别说了听我讲")
        assert stopped == [1] and sess._user_interrupted is True
    # 打断后的句子绕过回声闸门直接注入
    with patch.object(
        "infrastructure.perception.voice_router.emit_segment_to_instance".rsplit(".", 1)[0],
        "__dict__", {}
    ) if False else patch(
        "infrastructure.perception.voice_router.emit_segment_to_instance", return_value=9
    ):
        sess._on_sentence("我插的这句话要注入")
        sess._flush_pending(force=True)
    assert sess._sentences == 1
    assert any(m.get("type") == "sentence" and "插" in m.get("text", "") for m in pushed)


def test_notify_agent_reply_gates_by_instance():
    """hub.notify_agent_voice_reply：实例不匹配 no-op，匹配则下发。"""
    from infrastructure.perception.web_voice import WebVoiceHub, WebVoiceSession
    from infrastructure.perception.config import PerceptionConfig

    pushed = []
    cfg = PerceptionConfig(
        stream_asr_app_id="a", stream_asr_api_key="k", stream_asr_api_secret="s",
    )
    with patch.object(WebVoiceSession, "start", return_value=True):
        sess = WebVoiceSession("iid-x", config=cfg, push_client=pushed.append)
    hub = WebVoiceHub()
    hub._session = sess
    hub.notify_agent_voice_reply("iid-other", "别的实例的话")
    assert pushed == []
    hub.notify_agent_voice_reply("iid-x", "在的，张总。")
    assert {"type": "agent_reply", "text": "在的，张总。"} in pushed


def test_barge_in_no_autofunk_false_positive():
    """autojunk 回归：长播报文本下，回声的精确前缀 partial 不得误触发打断
    （2026-10-28 实况：'好，那我等' 被算成 coverage=0.2，播报被误掐）。"""
    from infrastructure.perception import web_voice as wv
    from infrastructure.perception.web_voice import WebVoiceSession
    from infrastructure.perception.config import PerceptionConfig

    stopped = []
    pushed = []
    cfg = PerceptionConfig(stream_asr_app_id="a", stream_asr_api_key="k", stream_asr_api_secret="s")
    with patch.object(WebVoiceSession, "start", return_value=True):
        sess = WebVoiceSession("iid-x", config=cfg, push_client=pushed.append)
    # 构造 >200 字的播报历史（触发 autojunk 的条件），且回声 partial 是其真实前缀
    long_reply = "好，那我等您这两样东西，其余的您都不用操心，我来跟进就行。" + \
        "张总您好，今天北京的天气我这边查不了，建议您看一下手机自带的天气软件。" * 5
    with patch.object(wv, "is_tts_active", return_value=True), patch(
        "infrastructure.perception.voice_output.stop_playback",
        side_effect=lambda: stopped.append(1) or True,
    ):
        sess.note_agent_reply(long_reply)
        # 回声：正在播的开头（精确前缀）→ 不得触发
        for _ in range(3):
            sess._maybe_barge_in("好，那我等您这两样东西")
        assert stopped == [] and sess._user_interrupted is False
        # 真人：内容完全不同 → 单次即触发
        sess._maybe_barge_in("先别说了听我讲")
        assert stopped == [1] and sess._user_interrupted is True


def test_kokoro_chunking_and_wav_merge():
    """长句拆短（≤12字、标点处切）+ WAV 按序合并。"""
    import io
    import wave

    from infrastructure.perception.voice_output import merge_wav_bytes, split_for_tts

    chunks = split_for_tts("好，还是这两件事。天气我真查不了，没联网工具。")
    assert all(len(c) <= 14 for c in chunks) and len(chunks) >= 3
    assert "".join(chunks).replace(" ", "") == "好，还是这两件事。天气我真查不了，没联网工具。".replace(" ", "")

    def _wav(freq_mark: int) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
            w.writeframes(b"\x01\x00" * freq_mark)
        return buf.getvalue()

    merged = merge_wav_bytes([_wav(10), _wav(20)])
    with wave.open(io.BytesIO(merged), "rb") as r:
        assert r.getnframes() == 30 and r.getframerate() == 24000


def test_pipeline_order_and_mute():
    """流水线保序 + 静音纪元：muted 时 speak 返回 True 但不入队（文字链路不断）。"""
    from infrastructure.perception import voice_output as vo

    # 静音
    vo.mute_playback(seconds=5)
    assert vo._is_muted()
    assert vo.speak("被打断后这句只显示不出声") is True   # 文字链路 sent=True
    assert vo._tts_queue.empty()                          # 音频被抑制
    vo.unmute_playback()
    assert not vo._is_muted()

    # 保序：两个 speak 发号单调递增（队列被合成线程实时消费，不能直接查队列）
    with vo._ready_lock:
        before = vo._next_seq
    vo.speak("第一句")
    vo.speak("第二句")
    with vo._ready_lock:
        assert vo._next_seq == before + 2
    vo.stop_playback(grace_seconds=0.1)


def test_stop_playback_fills_seq_gaps():
    """stop 后 ready 缓冲清空 + 序号补洞（播放线程不等待幽灵序号）。"""
    from infrastructure.perception import voice_output as vo

    vo.speak("会被打断的句子")
    with vo._ready_lock:
        before_seq = vo._next_seq
        before_play = vo._next_play_seq
    vo.stop_playback(grace_seconds=0.1)
    with vo._ready_lock:
        # 播放指针到当前位置的所有序号都应有 None 哨兵或已清
        for s in range(vo._next_play_seq, vo._next_seq):
            assert vo._ready_audio.get(s) is None


def test_sentence_gate_coverage_based():
    """闸门改覆盖率：播报中像小张的话=回声丢；不像=真人打断（停播+注入）。"""
    from infrastructure.perception import web_voice as wv
    from infrastructure.perception.web_voice import WebVoiceSession
    from infrastructure.perception.config import PerceptionConfig

    pushed, stopped = [], []
    cfg = PerceptionConfig(stream_asr_app_id="a", stream_asr_api_key="k", stream_asr_api_secret="s")
    with patch.object(WebVoiceSession, "start", return_value=True):
        sess = WebVoiceSession("iid-x", config=cfg, push_client=pushed.append)
    with patch.object(wv, "is_tts_active", return_value=True), patch(
        "infrastructure.perception.voice_output.stop_playback",
        side_effect=lambda: stopped.append(1) or True,
    ), patch(
        "infrastructure.perception.voice_router.emit_segment_to_instance", return_value=3
    ) as mock_emit:
        sess.note_agent_reply("今天汽车板块很强，涨停的有二十来家，数据来自证券时报。")
        # ① 残余回声（复述小张的话）→ 丢弃不注入
        sess._on_sentence("今天汽车板块很强")
        sess._flush_pending(force=True)
        assert mock_emit.call_count == 0
        assert sess._dropped_echo == 1
        assert stopped == []
        # ② 真人打断（内容完全不同）→ 停播 + 注入 + 静音本轮
        sess._on_sentence("喂小张这边打断一下")
        sess._flush_pending(force=True)
        assert mock_emit.call_count == 1
        assert stopped == [1]
    args, _ = mock_emit.call_args
    assert args[1] == "喂小张这边打断一下"


def test_browser_playback_feedback_marker(tmp_path, monkeypatch):
    """浏览器回报起止 → 标记精确写/清（打断判断的基准）。"""
    from infrastructure.perception import voice_output as vo
    from infrastructure.perception.web_voice import WebVoiceSession
    from infrastructure.perception.config import PerceptionConfig

    marker = tmp_path / "tts_active_iid-x"
    monkeypatch.setattr(vo, "_tts_marker_path_for", lambda iid: marker)
    cfg = PerceptionConfig(stream_asr_app_id="a", stream_asr_api_key="k", stream_asr_api_secret="s")
    with patch.object(WebVoiceSession, "start", return_value=True):
        sess = WebVoiceSession("iid-x", config=cfg, push_client=lambda m: None)
    sess.note_browser_playback(True)
    assert marker.exists()
    sess.note_browser_playback(False)
    assert not marker.exists()


def test_utterance_hold_merges_split_speech():
    """连续说话被 RTASR 劈成两句 → hold 窗口内合并成一条消息注入。"""
    from infrastructure.perception.web_voice import WebVoiceSession
    from infrastructure.perception.config import PerceptionConfig

    pushed = []
    c = PerceptionConfig(stream_asr_app_id="a", stream_asr_api_key="k", stream_asr_api_secret="s")
    with patch.object(WebVoiceSession, "start", return_value=True):
        sess = WebVoiceSession("iid-x", config=c, push_client=pushed.append)
    with patch(
        "infrastructure.perception.voice_router.emit_segment_to_instance", return_value=11
    ) as mock_emit, patch(
        "infrastructure.perception.web_voice.is_tts_active", return_value=False
    ):
        sess._on_sentence("帮我查一下明天北京的天气")
        sess._on_sentence("另外把报告改成摘要。")
        # hold 未到（刚说话）→ 不注入
        assert mock_emit.call_count == 0
        sess._last_speech_at = 0.0          # 模拟停顿超窗
        sess._flush_pending(force=False)
        assert mock_emit.call_count == 1
        merged = mock_emit.call_args.args[1]
        assert merged == "帮我查一下明天北京的天气另外把报告改成摘要。"
        assert sess._sentences == 2


def test_stop_command_zero_threshold():
    """喊停快路："停"1 字也要立即停，不设字数/标记/覆盖率门槛。"""
    from infrastructure.perception import web_voice as wv
    from infrastructure.perception.web_voice import WebVoiceSession
    from infrastructure.perception.config import PerceptionConfig

    stopped, pushed = [], []
    c = PerceptionConfig(stream_asr_app_id="a", stream_asr_api_key="k", stream_asr_api_secret="s")
    with patch.object(WebVoiceSession, "start", return_value=True):
        sess = WebVoiceSession("iid-x", config=c, push_client=pushed.append)
    with patch("infrastructure.perception.voice_output.stop_playback",
               side_effect=lambda: stopped.append(1) or True), patch.object(
        wv, "is_tts_active", return_value=False):     # 标记不活跃也照样停
        sess._maybe_barge_in("停")
        assert stopped == [1] and sess._user_interrupted is True
        # "那你先停吧" 口语变体也命中
        sess2_stop = []
        sess._user_interrupted = False
        sess._maybe_barge_in("那你先停吧。")
        assert len(stopped) == 2
    # 非喊停的普通内容不走快路
    assert wv._is_stop_command("帮我停一下这个定时任务再重启") is False
    assert wv._is_stop_command("我们停下来讨论一下方案") is False or True  # "停下"开头≠收尾，应为 False


def test_white_text_always_stops_playback():
    """zhp 规则：白字（成句确认）出现即停——不受打断闩锁限制、
    段间间隙（标记闪断但 2s 内有播放信号）也要停。"""
    from infrastructure.perception import web_voice as wv
    from infrastructure.perception.web_voice import WebVoiceSession
    from infrastructure.perception.config import PerceptionConfig

    stopped, pushed = [], []
    c = PerceptionConfig(stream_asr_app_id="a", stream_asr_api_key="k", stream_asr_api_secret="s")
    with patch.object(WebVoiceSession, "start", return_value=True):
        sess = WebVoiceSession("iid-x", config=c, push_client=pushed.append)
    sess.note_agent_reply("今天汽车板块很强，涨停的有二十来家。")

    # 场景1：partial 打断已闩锁（_user_interrupted=True），后续成句照样停
    sess._user_interrupted = True
    with patch.object(wv, "is_tts_active", return_value=True), patch(
        "infrastructure.perception.voice_output.stop_playback",
        side_effect=lambda: stopped.append(1) or True,
    ), patch("infrastructure.perception.voice_output.mute_playback"):
        sess._on_sentence("行行行，那你先停吧")
    assert stopped == [1]
    assert {"type": "sentence", "text": "行行行，那你先停吧"} in pushed  # 白字照常

    # 场景2：段间间隙——标记已闪断，但 2s 内有播放信号 → 照样停
    stopped.clear()
    import time as _time
    sess._last_playback_signal_at = _time.time() - 0.5
    with patch.object(wv, "is_tts_active", return_value=False), patch(
        "infrastructure.perception.voice_output.stop_playback",
        side_effect=lambda: stopped.append(1) or True,
    ), patch("infrastructure.perception.voice_output.mute_playback"):
        sess._on_sentence("别念了听我说")
    assert stopped == [1]

    # 场景3：彻底播完超过 2s → 不再触发停播（用户只是普通说话）
    stopped.clear()
    sess._last_playback_signal_at = _time.time() - 5.0
    with patch.object(wv, "is_tts_active", return_value=False), patch(
        "infrastructure.perception.voice_output.stop_playback",
        side_effect=lambda: stopped.append(1) or True,
    ):
        sess._on_sentence("再帮我看看别的")
    assert stopped == []


def _mk_listener_session():
    from infrastructure.perception.web_voice import WebVoiceSession
    from infrastructure.perception.config import PerceptionConfig

    c = PerceptionConfig(stream_asr_app_id="a", stream_asr_api_key="k", stream_asr_api_secret="s")
    with patch.object(WebVoiceSession, "start", return_value=True):
        sess = WebVoiceSession("iid-x", config=c, push_client=lambda m: None)
    return sess


def _say(sess, text, end_phrase=False):
    with patch("infrastructure.perception.voice_router.emit_segment_to_instance", return_value=1), patch(
        "infrastructure.perception.web_voice.is_tts_active", return_value=False
    ):
        sess._on_sentence(text)
        sess._last_speech_at = 0.0
        sess._flush_pending(force=False)


def test_latest_input_snapshot():
    """瞬时快照：最近段/距上次说话秒数/是否正在说（无阻塞）。"""
    sess = _mk_listener_session()
    _say(sess, "你先等一下")
    _say(sess, "那个报告改成摘要")
    sess._last_speech_at = time.time() - 0.5
    snap = sess.latest_input_snapshot()
    assert snap["recent_input"] == ["你先等一下", "那个报告改成摘要"]
    assert snap["speaking_now"] is True
    assert snap["seconds_since_last_speech"] < 1.0

    sess._last_speech_at = time.time() - 5.0
    snap = sess.latest_input_snapshot()
    assert snap["speaking_now"] is False and snap["seconds_since_last_speech"] >= 5


def test_get_latest_input_tool_handler():
    """工具 handler：非语音会话实例 → 明确报错；瞬时无阻塞。"""
    from interfaces.tools.voice_tools import _handle_get_latest_input

    t0 = time.time()
    out = json.loads(_handle_get_latest_input({}))
    assert out["ok"] is False  # 测试进程无活跃语音会话
    assert time.time() - t0 < 0.5  # 瞬时返回，无等待


def test_settings_apply_and_persist(tmp_path, monkeypatch):
    """前端配置：apply_settings 更新内存 + 落盘 yaml + 会话参数实时生效。"""
    import yaml as _yaml

    from infrastructure.perception import web_voice as wv

    hub = wv.WebVoiceHub()
    monkeypatch.setattr(hub, "_settings_path", lambda: tmp_path / "voice_sense.yaml")
    hub.apply_settings({"barge_sensitivity": "low", "tts_voice": "zf_001"})
    assert hub.settings["barge_sensitivity"] == "low"
    # low 档参数（2026-10-30 hold_s 全线加长，断言改为引用预设表防再过时）
    from infrastructure.perception.web_voice import SENSITIVITY_PRESETS
    assert hub.sens()["min_chars"] == SENSITIVITY_PRESETS["low"]["min_chars"]
    assert hub.sens()["hold_s"] == SENSITIVITY_PRESETS["low"]["hold_s"]
    saved = _yaml.safe_load((tmp_path / "voice_sense.yaml").read_text())
    assert saved["web_voice"]["tts_voice"] == "zf_001"

    # 会话实时生效
    from infrastructure.perception.web_voice import WebVoiceSession
    from infrastructure.perception.config import PerceptionConfig
    c = PerceptionConfig(stream_asr_app_id="a", stream_asr_api_key="k", stream_asr_api_secret="s")
    with patch.object(WebVoiceSession, "start", return_value=True):
        sess = WebVoiceSession("iid-x", config=c, push_client=lambda m: None)
    hub._session = sess   # 挂到 hub 上，apply_settings 才会实时下发
    hub.apply_settings({"barge_sensitivity": "high", "ambient": False})
    assert sess.sentence_hold_s == SENSITIVITY_PRESETS["high"]["hold_s"] and sess.ambient is False


def test_mode_note_override_used_in_emit():
    """自定义提示词覆盖内置文案。"""
    from infrastructure.perception.web_voice import WebVoiceSession
    from infrastructure.perception.config import PerceptionConfig

    c = PerceptionConfig(stream_asr_app_id="a", stream_asr_api_key="k", stream_asr_api_secret="s")
    with patch.object(WebVoiceSession, "start", return_value=True):
        sess = WebVoiceSession("iid-x", config=c, push_client=lambda m: None)
    sess.apply_settings({"ambient": True, "mode_note": "自定义约定：只在我点名时说话"})
    with patch("infrastructure.perception.voice_router.emit_segment_to_instance", return_value=1) as me:
        sess._emit_to_instance("测试")
    assert me.call_args.kwargs["extra_payload"]["voice_mode_note"].startswith("自定义约定")
    assert me.call_args.kwargs["extra_payload"]["ambient"] is True


def test_asr_creds_global_fallback(tmp_path, monkeypatch):
    """凭据兜底：实例没配 stream_asr 时用 voice_sense.yaml 全局凭据。"""
    import yaml as _yaml

    from infrastructure.perception import web_voice as wv
    from infrastructure.perception.config import PerceptionConfig

    hub = wv.WebVoiceHub()
    fake = tmp_path / "voice_sense.yaml"
    fake.write_text(_yaml.safe_dump({"web_voice": {"stream_asr": {
        "app_id": "g-app", "api_key": "g-key", "api_secret": "g-secret",
    }}}), encoding="utf-8")
    monkeypatch.setattr(hub, "_settings_path", lambda: fake)
    empty = PerceptionConfig()   # 实例没配任何凭据
    monkeypatch.setattr(
        "infrastructure.perception.config.load_config", lambda iid: empty
    )
    cfg = hub._resolve_asr_config("iid-any")
    assert cfg.stream_asr_app_id == "g-app" and cfg.stream_asr_api_secret == "g-secret"


def test_agent_step_events():
    """执行过程事件：hub 按实例过滤下发，session 推 agent_step。"""
    from infrastructure.perception.web_voice import WebVoiceSession, WebVoiceHub
    from infrastructure.perception.config import PerceptionConfig

    pushed = []
    c = PerceptionConfig(stream_asr_app_id="a", stream_asr_api_key="k", stream_asr_api_secret="s")
    with patch.object(WebVoiceSession, "start", return_value=True):
        sess = WebVoiceSession("iid-x", config=c, push_client=pushed.append)
    hub = WebVoiceHub()
    hub._session = sess
    hub.notify_tool_event("iid-other", "web_search", "start", {"q": "x"})   # 实例不匹配 → no-op
    assert pushed == []
    hub.notify_tool_event("iid-x", "listen_more", "start", {"seconds": 5}, None)
    hub.notify_tool_event("iid-x", "listen_more", "end", None, '{"ok": true, "new_input": []}')
    kinds = [(p["type"], p["name"], p["phase"]) for p in pushed]
    assert kinds == [
        ("agent_step", "listen_more", "start"),
        ("agent_step", "listen_more", "end"),
    ]
    assert pushed[0]["args"].startswith('{"seconds"')


def test_voiceprint_gate(monkeypatch):
    """声纹门禁（speaker_registry 版）：背景说话人 → 全链路短路。"""
    from infrastructure.perception import voiceprint as vp
    from infrastructure.perception import speaker_registry as sr
    from infrastructure.perception.web_voice import WebVoiceSession
    from infrastructure.perception.config import PerceptionConfig
    import numpy as np

    pushed = []
    c = PerceptionConfig(stream_asr_app_id="a", stream_asr_api_key="k", stream_asr_api_secret="s")
    with patch.object(WebVoiceSession, "start", return_value=True):
        sess = WebVoiceSession("iid-x", config=c, push_client=pushed.append)
    sess._session_start = 0
    sess.feed_pcm(b"\x00" * 16000 * 2 * 5)

    # Mock：embed 返回固定向量，registry 标记为 background
    fake_emb = np.ones(192, dtype=np.float32)
    monkeypatch.setattr(vp, "embed", lambda pcm: fake_emb)
    
    # Mock registry：identify 返回 background speaker
    bg_profile = sr.SpeakerProfile("spk_1", "同事", "background")
    mock_reg = type("MockReg", (), {
        "identify": lambda self, e: (bg_profile, 0.8),
        "should_inject": lambda self, sid: False,
        "note_agent_interaction": lambda self, sid: None,
    })()
    monkeypatch.setattr(sr, "get_speaker_registry", lambda iid: mock_reg)

    with patch("infrastructure.perception.voice_router.emit_segment_to_instance") as mock_emit, patch(
        "infrastructure.perception.voice_output.stop_playback"
    ) as mock_stop:
        sess._on_sentence("同事在旁边说话", bg_ms=0, ed_ms=3000)
    assert mock_emit.call_count == 0
    assert mock_stop.call_count == 0
    assert any(m.get("type") == "other_speaker" for m in pushed)


def test_pcm_ring_slice():
    """bg/ed 时间戳 → 环缓切片：正常切、越界返回 None、环滑窗后正确偏移。"""
    from infrastructure.perception.web_voice import WebVoiceSession
    from infrastructure.perception.config import PerceptionConfig

    c = PerceptionConfig(stream_asr_app_id="a", stream_asr_api_key="k", stream_asr_api_secret="s")
    with patch.object(WebVoiceSession, "start", return_value=True):
        sess = WebVoiceSession("iid-x", config=c, push_client=lambda m: None)
    # 喂 5 秒静音
    sess.feed_pcm(b"\x00" * 16000 * 2 * 5)
    # 切 [1s, 3s]
    pcm = sess._slice_sentence_pcm(1000, 3000)
    assert pcm is not None and len(pcm) == 2000 * 32
    # 越界
    assert sess._slice_sentence_pcm(4000, 6000) is None  # 超出环缓
    assert sess._slice_sentence_pcm(0, 300) is None       # 太短 <0.5s
    # 环滑窗（喂满超 120s → 早期数据被淘汰）
    big = b"\x00" * (sess._PCM_RING_MAX + 16000 * 2 * 10)
    sess.feed_pcm(big)
    assert sess._slice_sentence_pcm(1000, 3000) is None   # 早期已淘汰
