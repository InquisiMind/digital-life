"""网页语音会话（web_voice）测试。

验证：
  - 回声闸门：TTS 标记活跃时确认句被丢弃、不 emit；标记消失/过期则放行
  - 会话注入：成句走 emit_segment_to_instance 且 payload 带 quick_first_reply
  - Hub 单槽位：第二个会话被拒
  - voice_output 播放标记生命周期（写/清/打断清）
"""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from infrastructure.perception import web_voice
from infrastructure.perception.config import PerceptionConfig
from infrastructure.perception.web_voice import (
    WebVoiceHub,
    WebVoiceSession,
    is_tts_active,
    tts_marker_path,
)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """感知配置（不触盘的凭据 + 重定向 project root 到 tmp）。"""
    monkeypatch.setattr(
        "infrastructure.config.get_project_root", lambda: tmp_path, raising=False
    )
    web_voice._project_root_for_test = tmp_path  # noqa: SLF001 防御性：不依赖真实仓库
    return PerceptionConfig(
        enabled=True,
        asr_provider="glm",
        stream_asr_provider="iflytek_rtasr",
        stream_asr_app_id="app",
        stream_asr_api_key="key",
        stream_asr_api_secret="secret",
    )


@pytest.fixture
def marker(cfg, tmp_path, monkeypatch):
    """把 TTS 标记路径指到 tmp，并返回写/清助手。"""
    monkeypatch.setattr(
        "infrastructure.perception.web_voice.tts_marker_path",
        lambda iid: tmp_path / "run" / f"tts_active_{iid}",
    )

    def _write(iid="iid-x", age_s=0.0):
        p = tmp_path / "run" / f"tts_active_{iid}"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
        if age_s:
            old = time.time() - age_s
            import os

            os.utime(p, (old, old))
        return p

    return _write


# ── 回声闸门 ────────────────────────────────────────────────────────────────


def test_tts_marker_active(marker):
    marker()
    assert is_tts_active("iid-x") is True


def test_tts_marker_absent(marker):
    assert is_tts_active("iid-x") is False


def test_tts_marker_stale_treated_inactive(marker):
    """worker 崩溃没清标记 → mtime 超窗视为不活跃（句子放行）。"""
    marker(age_s=web_voice.TTS_MARKER_MAX_AGE_S + 5)
    assert is_tts_active("iid-x") is False


# ── 会话：成句注入 + 回声丢弃 ───────────────────────────────────────────────


def _make_session(cfg, pushed):
    with patch.object(WebVoiceSession, "start", return_value=True):
        session = WebVoiceSession("iid-x", config=cfg, push_client=pushed.append)
    return session


def test_sentence_emitted_with_quick_first_reply(cfg, marker):
    """正常成句 → emit 带 quick_first_reply 的 perception_signal + 下行 sentence。"""
    pushed: list[dict] = []
    session = _make_session(cfg, pushed)
    with patch(
        "infrastructure.perception.voice_router.emit_segment_to_instance",
        return_value=7,
    ) as mock_emit, patch.object(
        web_voice, "is_tts_active", return_value=False
    ):
        session._on_sentence("帮我查个东西。")
        session._flush_pending(force=True)
    mock_emit.assert_called_once()
    args, kwargs = mock_emit.call_args
    assert args[0] == "iid-x"
    assert args[1] == "帮我查个东西。"
    ep = kwargs["extra_payload"]
    assert ep["quick_first_reply"] is True and "voice_mode_note" in ep
    assert {"type": "sentence", "text": "帮我查个东西。"} in pushed
    # event_sent 带 utterance 全文（2026-10-30：前端落用户消息气泡的数据源）
    assert {"type": "event_sent", "event_id": 7, "text": "帮我查个东西。"} in pushed


def test_sentence_dropped_while_tts_active(cfg, marker):
    """TTS 播放中、内容像小张刚说的话（高覆盖）= 残余回声 → 丢弃，不 emit。"""
    marker("iid-x")
    pushed: list[dict] = []
    session = _make_session(cfg, pushed)
    session.note_agent_reply("小张自己说的话被麦克风收回去。")
    with patch(
        "infrastructure.perception.voice_router.emit_segment_to_instance"
    ) as mock_emit:
        session._on_sentence("小张自己说的话被麦克风收回去。")
        session._flush_pending(force=True)
    mock_emit.assert_not_called()
    assert session._dropped_echo == 1
    assert any(m.get("type") == "echo_dropped" for m in pushed)


def test_after_barge_in_sentence_passes(cfg, marker):
    """打断场景：barge-in 清标记后，真人句放行（stop_playback 清标记语义）。"""
    marker("iid-x")
    pushed: list[dict] = []
    session = _make_session(cfg, pushed)
    marker_path = web_voice.tts_marker_path("iid-x")  # 模块属性（已被 monkeypatch）
    marker_path.unlink()  # 模拟 stop_playback 立即清标记
    with patch(
        "infrastructure.perception.voice_router.emit_segment_to_instance",
        return_value=8,
    ) as mock_emit:
        session._on_sentence("停，我有话说。")
        session._flush_pending(force=True)
    mock_emit.assert_called_once()


def test_partial_pushed_as_subtitle(cfg):
    pushed: list[dict] = []
    session = _make_session(cfg, pushed)
    session._on_partial("正在说的半句")
    assert {"type": "partial", "text": "正在说的半句"} in pushed


# ── Hub 单槽位 ──────────────────────────────────────────────────────────────


def test_hub_new_connection_evicts_old(cfg):
    """逐旧接管：第二个连接赢；被逐旧的连接 stop 时不误杀新会话。"""
    hub = WebVoiceHub()

    class FakeSession:
        token_seq = 0
        def __init__(self, instance_id, config=None, push_client=None, barge_in_enabled=True, ambient=False):
            FakeSession.token_seq += 1
            self.token = FakeSession.token_seq
            self.instance_id = instance_id
            self.started_at = 0.0
            self._sentences = 0
            self.stop_called = 0
        def start(self):
            return True
        def stop(self):
            self.stop_called += 1
            return {"ok": True}
        def apply_settings(self, settings):
            pass

    import infrastructure.perception.web_voice as wv_mod
    with patch.object(wv_mod, "WebVoiceSession", FakeSession), patch(
        "infrastructure.perception.config.load_config", return_value=cfg
    ):
        ok1, msg1, tok1 = hub.start_session("iid-x", lambda m: None)
        first = hub._session
        ok2, msg2, tok2 = hub.start_session("iid-x", lambda m: None)
        second = hub._session
        assert ok1 and ok2 and tok2 != tok1
        assert first.stop_called == 1            # 旧会话被停
        out = hub.stop_session(token=tok1)       # 旧连接断开 → no-op
        assert out.get("evicted") is True
        assert second.stop_called == 0           # 新会话未被误杀
        assert hub._session is second
        hub.stop_session(token=tok2)


def test_hub_rejects_unconfigured_instance():
    """实例与全局都没有凭据时才拒绝（全局兜底后这是唯一拒绝路径）。"""
    hub = WebVoiceHub()
    hub._global_asr_creds = lambda: {"app_id": "", "api_key": "", "api_secret": ""}
    empty = PerceptionConfig(enabled=False)
    with patch(
        "infrastructure.perception.config.load_config", return_value=empty
    ):
        ok, msg, tok = hub.start_session("iid-x", lambda m: None)
    assert ok is False
    assert "stream ASR not configured" in msg


# ── voice_output 播放标记生命周期 ───────────────────────────────────────────


def test_voice_output_marker_lifecycle(tmp_path, monkeypatch):
    """入队写标记 → 播放注册保持 → 停播（barge-in）立即清。"""
    import queue as queue_mod
    import subprocess

    from infrastructure.perception import voice_output

    marker_file = tmp_path / "tts_active_iid-x"
    monkeypatch.setattr(
        voice_output, "_tts_marker_path", lambda: marker_file
    )
    # speak() 走 _ensure_player 前需要文本有效；直接测底层三函数 + stop
    proc = subprocess.Popen(
        ["/bin/sleep", "30"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    voice_output._register_playback(proc)
    assert marker_file.exists()

    voice_output._unregister_playback(proc)
    assert not marker_file.exists()  # 无播放 + 队列空 → 清

    # 打断路径：注册中 stop_playback 清标记
    voice_output._register_playback(proc)
    assert marker_file.exists()
    voice_output.stop_playback(grace_seconds=0.5)
    assert not marker_file.exists()


# ── VAD 集成（2026-10-30：silero 声学层）───────────────────────────────────


class _FakeVAD:
    """可控 VAD 替身：speech 序列按帧弹出，模型缺失场景 available=False。"""

    def __init__(self, available=True):
        self.available = available
        self.speech = False

    def feed(self, pcm: bytes):
        return self.speech if self.available else None


def test_vad_speech_refreshes_endpoint_timer(cfg, marker):
    """VAD 人声帧刷新语末计时（比"转写出新字"更直接、不受转写延迟影响）。"""
    session = _make_session(cfg, [])
    vad = _FakeVAD()
    session._vad = vad
    with patch.object(web_voice, "is_tts_active", return_value=False):
        before = session._last_speech_at
        vad.speech = True
        session._on_sentence("第一句。")  # sentence 本身也刷新
        session.feed_pcm(b"\x00" * 1280)  # VAD=True 的帧
        assert session._last_speech_at >= before


def test_vad_endpointing_fast_flush(cfg, marker):
    """VAD 静音超 1s → 提前 flush（不等满 hold_s 慢窗）。"""
    pushed: list[dict] = []
    session = _make_session(cfg, pushed)
    session._vad = _FakeVAD()
    with patch(
        "infrastructure.perception.voice_router.emit_segment_to_instance",
        return_value=9,
    ) as mock_emit, patch.object(web_voice, "is_tts_active", return_value=False):
        session._on_sentence("说完了。")
        # VAD 说话 → 静音已超过 1s（直接把时间戳拨到 1.1s 前）
        session._last_vad_speech_at = time.time() - 1.1
        session._last_speech_at = time.time() - 1.1  # 文本侧同样静默
        session._flush_pending(force=False)
    mock_emit.assert_called_once()


def test_vad_unavailable_falls_back_to_hold_window(cfg, marker):
    """VAD 不可用 → 退回固定 hold 窗口语义（快路不生效）。"""
    session = _make_session(cfg, [])
    session._vad = _FakeVAD(available=False)
    with patch(
        "infrastructure.perception.voice_router.emit_segment_to_instance",
        return_value=10,
    ) as mock_emit, patch.object(web_voice, "is_tts_active", return_value=False):
        session._on_sentence("还在等慢窗。")
        # 静音 1.2s（>VAD 快路 1.0s，但 < hold_s 2.0s）→ 不 flush
        session._last_speech_at = time.time() - 1.2
        session._flush_pending(force=False)
        mock_emit.assert_not_called()
        # 满 hold_s → flush
        session._last_speech_at = time.time() - session.sentence_hold_s - 0.1
        session._flush_pending(force=False)
        mock_emit.assert_called_once()


# ── 假打断恢复（2026-10-30：误判停播 2.5s 无后续人声 → 续播剩余段）────────


@pytest.fixture
def vo_state():
    """隔离 voice_output 模块级恢复状态（测试互不污染）。"""
    from infrastructure.perception import voice_output

    saved = (
        voice_output._pending_recovery,
        list(voice_output._web_segment_log),
        voice_output._now_playing_seq,
        voice_output._now_playing_text,
    )
    voice_output._pending_recovery = {}
    voice_output._web_segment_log = []
    yield voice_output
    (
        voice_output._pending_recovery,
        voice_output._web_segment_log,
        voice_output._now_playing_seq,
        voice_output._now_playing_text,
    ) = saved


def _barge_in(session, partial="明显不一样的真人插话"):
    """驱动一次覆盖率级 barge-in（停播成功 → 预约恢复）。"""
    session.note_agent_reply("小张正在播报的很长的一段话。")
    with patch.object(web_voice, "is_tts_active", return_value=True), patch(
        "infrastructure.perception.voice_output.stop_playback", return_value=True
    ):
        session._maybe_barge_in(partial)


def test_false_interrupt_recovery_resumes(cfg, marker, vo_state):
    """误判打断：2.5s 内无后续人声文本 → 续播剩余段 + 下发 tts_resume。"""
    pushed: list[dict] = []
    session = _make_session(cfg, pushed)
    _barge_in(session)
    assert session._recovery_at > 0  # 已预约
    with patch.object(
        vo_state, "play_activity", return_value=42
    ), patch.object(
        vo_state, "collect_recovery_texts", return_value=["第二段。", "第三段。"]
    ) as m_collect, patch.object(
        vo_state, "speak", return_value=True
    ) as m_speak, patch.object(
        vo_state, "unmute_playback"
    ) as m_unmute, patch.object(
        web_voice, "web_tts_voice", return_value="xf:xiaoyan"
    ), patch.object(
        web_voice, "web_tts_rate", return_value="+0%"
    ):
        session._recovery_baseline_activity = 42  # 停播后无新播放
        session._recovery_at = time.time() - 0.01  # 到点
        session._maybe_fire_recovery()
    m_collect.assert_called_once_with(session._interrupted_at_seq)
    assert [c.args[0] for c in m_speak.call_args_list] == ["第二段。", "第三段。"]
    assert m_speak.call_args.kwargs["voice"] == "xf:xiaoyan"
    m_unmute.assert_called_once()
    assert any(m.get("type") == "tts_resume" and m["count"] == 2 for m in pushed)
    assert session._recovery_at == 0  # 一次性消费


def test_recovery_cancelled_by_confirmed_sentence(cfg, marker, vo_state):
    """白字（确认句）= 用户确凿在说话 → 取消恢复。"""
    session = _make_session(cfg, [])
    _barge_in(session)
    with patch.object(web_voice, "is_tts_active", return_value=False), patch(
        "infrastructure.perception.voice_router.emit_segment_to_instance",
        return_value=11,
    ):
        session._on_sentence("我真的在说话。")
    assert session._recovery_at == 0


def test_recovery_cancelled_by_new_partial_chars(cfg, marker, vo_state):
    """停播窗口内 partial 出新字 = 用户还在说 → 到点不续播。"""
    session = _make_session(cfg, [])
    _barge_in(session)
    with patch.object(web_voice, "is_tts_active", return_value=False):
        session._on_partial("新的内容出现了")
    with patch.object(vo_state, "collect_recovery_texts") as m_collect:
        session._recovery_at = time.time() - 0.01
        session._maybe_fire_recovery()
    m_collect.assert_not_called()


def test_stop_command_never_schedules_recovery(cfg, marker, vo_state):
    """喊停（停止指令）是显式意图 → 不预约恢复。"""
    session = _make_session(cfg, [])
    with patch("infrastructure.perception.voice_output.stop_playback") as m_stop:
        session._maybe_barge_in("停")
    m_stop.assert_called_once()
    assert session._recovery_at == 0


def test_collect_recovery_texts_merges_and_consumes(vo_state):
    """浏览器回报段号之后的已下发段 + 服务端未播段，按序合并，取走即清。"""
    vo_state._web_segment_log = [(1, "第一段。"), (2, "第二段。"), (3, "第三段。")]
    vo_state._pending_recovery = {5: "还在服务端的第五段。"}
    assert vo_state.collect_recovery_texts(2) == ["第三段。", "还在服务端的第五段。"]
    assert vo_state.collect_recovery_texts(2) == []  # 一次性消费
    # 无浏览器回报（本地模式）：从已下发段全量恢复
    vo_state._web_segment_log = [(7, "本地段。")]
    assert vo_state.collect_recovery_texts(None) == ["本地段。"]


def test_stop_playback_captures_pending_texts(vo_state, cfg, tmp_path, monkeypatch):
    """stop_playback 捕获队列/ready 里未播的文本到恢复区（空停不冲掉旧捕获）。"""
    monkeypatch.setattr(
        "infrastructure.config.get_project_root", lambda: tmp_path, raising=False
    )
    vo_state._tts_queue.put((10, "排队中的文本。", "xf:xiaoyan", "+0%", 0, "iid-x"))
    with patch("infrastructure.perception.web_voice.get_web_voice_hub") as m_hub:
        m_hub.return_value.stop_web_tts.return_value = False
        assert vo_state.stop_playback() is True
    assert vo_state._pending_recovery == {10: "排队中的文本。"}
    # 空停（什么都没在播）不覆盖已捕获的恢复文本
    with patch("infrastructure.perception.web_voice.get_web_voice_hub") as m_hub:
        m_hub.return_value.stop_web_tts.return_value = False
        vo_state.stop_playback()
    assert vo_state._pending_recovery == {10: "排队中的文本。"}


# ── 动态断句窗口（2026-10-30：长叙述放宽静音窗，短问答快封口）──────────────


def test_endpoint_window_scales_with_utterance_length(cfg, marker):
    """长叙述（10s）静音窗 ≈1.6s：1.2s 静音不封口、1.7s 封口。"""
    session = _make_session(cfg, [])
    session._vad = _FakeVAD()
    now = time.time()
    session._vad_speech_started_at = now - 10.0
    session._last_vad_speech_at = now - 1.2  # 这轮话说了 ~8.8s
    with patch(
        "infrastructure.perception.voice_router.emit_segment_to_instance",
        return_value=12,
    ) as mock_emit:
        session._on_sentence("说了很长的一段话。")
        session._last_speech_at = now - 0.3  # 文本慢窗未过：只有 VAD 快路能封口
        session._last_vad_speech_at = now - 1.2
        session._flush_pending(force=False)
        mock_emit.assert_not_called()  # 1.2s < 动态窗 ~1.6s → 不封口
        session._last_vad_speech_at = now - 1.7
        session._flush_pending(force=False)
        mock_emit.assert_called_once()


def test_endpoint_window_short_utterance_stays_fast(cfg, marker):
    """短话（<1s）窗口保持 1.0s 底线：1.2s 静音即封口（快对话节奏）。"""
    session = _make_session(cfg, [])
    session._vad = _FakeVAD()
    now = time.time()
    session._vad_speech_started_at = now - 1.2
    session._last_vad_speech_at = now - 1.2
    with patch(
        "infrastructure.perception.voice_router.emit_segment_to_instance",
        return_value=13,
    ) as mock_emit:
        session._on_sentence("嗯。")
        session._last_speech_at = now - 0.3  # 同上：只让 VAD 快路裁决
        session._flush_pending(force=False)
    mock_emit.assert_called_once()
    assert session._vad_speech_started_at == 0  # 封口重置，下一轮重新计时


def test_xf_voice_speak_chunks_long_sentence(cfg, tmp_path, monkeypatch):
    """xf 音色长文本入队时按句级切块（保韵律连读；2026-10-30 逗号碎块改句级），
    非 xf 音色保持整句。"""
    from infrastructure.perception import voice_output

    monkeypatch.setattr(
        "infrastructure.config.get_project_root", lambda: tmp_path, raising=False
    )
    monkeypatch.setattr(voice_output, "_ensure_player", lambda: None)
    while not voice_output._tts_queue.empty():
        voice_output._tts_queue.get_nowait()
    long_text = "浩普，北京今天晴，最高二十度、夜里最低四度，早晨挺冷的，另外明天气温明显往下掉。"

    assert voice_output.speak(long_text, voice="xf:xiaoyan") is True
    items = []
    while not voice_output._tts_queue.empty():
        items.append(voice_output._tts_queue.get_nowait())
    assert len(items) > 1, "长句应拆成多块"
    assert all(len(it[1]) <= voice_output._XF_CHUNK_MAX for it in items)
    assert "".join(it[1] for it in items) == voice_output._clean_text_for_tts(long_text)
    assert [it[0] for it in items] == list(range(items[0][0], items[0][0] + len(items)))
    assert all(it[2] == "xf:xiaoyan" for it in items)

    # 非 xf 音色：保持整句一次入队（kokoro 引擎内部自己拆并行）
    assert voice_output.speak(long_text, voice="zm_030") is True
    single = []
    while not voice_output._tts_queue.empty():
        single.append(voice_output._tts_queue.get_nowait())
    assert len(single) == 1


# ── 轮次链路计时（2026-10-30 zhp：说完→第一声，干活不计）──────────────────


def test_turn_timing_flow(cfg, marker):
    """ASR 确认开表 → 七锚点收集 → 首块推达（1.5s 兜底）收表下发分解。"""
    pushed: list[dict] = []
    session = _make_session(cfg, pushed)
    session.set_binary_push(lambda b: None)
    with patch(
        "infrastructure.perception.voice_router.emit_segment_to_instance",
        return_value=20,
    ), patch.object(web_voice, "is_tts_active", return_value=False):
        session._last_vad_speech_at = time.time() - 1.0  # 1s 前最后一帧人声
        session._on_sentence("查一下天气。")              # 锚点①ASR 开表
        assert session._turn_timing is not None
        session._flush_pending(force=True)               # 锚点②flush/③event
        session.note_thinking_delta("想一想")             # 锚点④wake（消费）
        session.note_agent_step("web_search", "start")   # 干活开始
        session.note_agent_step("web_search", "end")     # 干活结束（≈0s）
        session.note_turn_anchor("speak")                # 锚点⑤模型首句
        session.note_turn_anchor("synth")                # 锚点⑥前：合成取块
    assert session.push_tts_audio(b"wav", "第一声。", 0) is True  # 锚点⑥push
    assert session._timing_emit_at > 0                   # 等起播回报
    session._push_turn_timing()                          # 兜底收表（无回报路径）
    tm = next(m for m in pushed if m.get("type") == "turn_timing")
    assert tm["asr"] == 1.0                              # 语音完成→ASR确认 1s
    assert set(tm) >= {"asr", "endpoint", "event", "consume", "gen", "synth", "play", "total", "work"}
    assert tm["gen"] >= 0 and tm["total"] >= tm["asr"]
    assert session._turn_timing is None                  # 收表即清


def test_turn_timing_excludes_tool_work(cfg, marker):
    """工具干活时长从"生成"段扣除、单列为 work。"""
    pushed: list[dict] = []
    session = _make_session(cfg, pushed)
    session.set_binary_push(lambda b: None)
    with patch(
        "infrastructure.perception.voice_router.emit_segment_to_instance",
        return_value=21,
    ), patch.object(web_voice, "is_tts_active", return_value=False):
        session._last_vad_speech_at = time.time()
        session._on_sentence("嗯。")
        session._flush_pending(force=True)
        session.note_thinking_delta("想")
        session.note_agent_step("web_search", "start")
        time.sleep(0.15)                                  # 干活 0.15s
        session.note_agent_step("web_search", "end")
        session.note_agent_step("express_to_human", "start")
    session.note_turn_anchor("speak")
    session.push_tts_audio(b"wav", "回话。", 1)
    session._push_turn_timing()
    tm = next(m for m in pushed if m.get("type") == "turn_timing")
    assert tm["work"] >= 0.1                              # 干活被单列
    assert tm["gen"] < tm["total"]                        # 且被扣出链路
