"""实时语音会话（VoiceSession）冒烟测试。

验证阶段 1 的核心契约：
  1. VADSegmenter：feed 不足 512 的尾块能补零、超长块能切片
  2. write_wav：int16 PCM 正确落盘成可读 wav
  3. VoiceSession：注入 FakeVAD 模拟分段 → 触发 on_segment → 写 wav → ASR(mock) → 追加文档
  4. 文档结构：header + 每段 block + 结束标记
  5. 完整生命周期：start → stop 幂等、子进程被清理

设计原则：
  - 不依赖真实麦克风（CI 无声卡）→ 用 FakeVAD 注入确定性的段
  - 不依赖真实 ASR 网络 → mock transcribe_file
  - 不依赖 Silero 权重对合成信号的判定（Silero 对合成音不敏感，实测真实录音也
    多为环境噪声不触发）→ VADSegmenter 只测"接口契约"（分块、补零、状态机
    via 真实 ONNX + 极低阈值 + 手工高概率帧注入）

注意：VADSegmenter 的构造需要 silero_vad.onnx（silero-vad pip 包自带）。
      若环境没装，这些用例 skip；VoiceSession 逻辑用 FakeVAD 不依赖它。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

# 检测 silero onnx 是否可用（决定 VADSegmenter 用例是否 skip）
try:
    from infrastructure.perception.voice_session import _find_silero_onnx

    _SILERO_AVAILABLE = True
    _SILERO_PATH = _find_silero_onnx()
except Exception:
    _SILERO_AVAILABLE = False
    _SILERO_PATH = None


# ── write_wav：int16 PCM → wav ─────────────────────────────────────────────


def test_write_wav_roundtrip(tmp_path):
    """write_wav 写出的文件能用标准 wave 模块读回，数据一致。"""
    import wave

    from infrastructure.perception.voice_session import write_wav

    # 1 秒 16k 正弦波 int16
    sr = 16000
    t = np.arange(sr) / sr
    audio = (np.sin(2 * np.pi * 220 * t) * 16000).astype(np.int16)
    path = tmp_path / "seg.wav"

    write_wav(path, audio, sample_rate=sr)

    # 读回验证
    with wave.open(str(path), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == sr
        frames = wf.getnframes()
        read_back = np.frombuffer(wf.readframes(frames), dtype="<i2")

    assert len(read_back) == len(audio)
    np.testing.assert_array_equal(read_back, audio)


# ── VADSegmenter：接口契约（需要 silero onnx）──────────────────────────────


@pytest.mark.skipif(not _SILERO_AVAILABLE, reason="silero_vad.onnx 未安装")
class TestVADSegmenterContract:
    """VADSegmenter 的接口/健壮性契约。

    不验证"能不能识别真语音"（那需要真实语音样本，CI 不稳定），只验证：
      - feed 接受任意长度块（< 512 补零、> 512 切片、空块不崩）
      - on_segment 在段结束时被调用
      - finish() flush 尾巴
      - 过短段被丢弃
    """

    def _make(self, on_segment, **kw):
        from infrastructure.perception.voice_session import VADSegmenter

        # 用极低阈值 + 极短判定，让合成高幅信号能触发（不依赖语音特征）
        defaults = dict(
            threshold=0.0,           # 任意信号都算"speech"
            silence_frames=3,
            min_speech_frames=1,
            min_speech_frames_to_keep=1,
            max_speech_seconds=999,
            model_path=_SILERO_PATH,
        )
        defaults.update(kw)
        return VADSegmenter(on_segment, **defaults)

    def test_feed_handles_short_tail(self):
        """不足 512 样本的尾块不报错（内部补零）。"""
        segs = []
        seg = self._make(lambda a: segs.append(a))
        # 一段大于 512 的信号 + 一个 10 样本的小尾巴
        seg.feed(np.ones(600, dtype=np.float32))
        seg.feed(np.ones(10, dtype=np.float32))
        seg.finish()
        # threshold=0 → 所有帧都算 speech → 应该至少触发一次（finish 时 flush）
        assert len(segs) >= 1

    def test_feed_splits_long_block(self):
        """超过 512 的块被切成多帧，全部喂进 VAD 不丢。"""
        segs = []
        seg = self._make(lambda a: segs.append(a))
        # 5000 样本一块（≈10 帧）
        seg.feed(np.full(5000, 0.5, dtype=np.float32))
        seg.finish()
        assert len(segs) >= 1

    def test_feed_empty_does_not_crash(self):
        """空块不崩。"""
        segs = []
        seg = self._make(lambda a: segs.append(a))
        seg.feed(np.array([], dtype=np.float32))
        seg.finish()
        assert segs == []

    def test_finish_flushes_in_flight_speech(self):
        """正在说话时调 finish → 尾段被 flush 出来。"""
        segs = []
        seg = self._make(lambda a: segs.append(a))
        seg.feed(np.full(2000, 0.5, dtype=np.float32))  # 持续"说话"，没有静音收尾
        seg.finish()
        assert len(segs) == 1, "finish 应 flush 正在进行的段"

    def test_short_segment_discarded(self):
        """min_speech_frames_to_keep 之外的过短段被丢弃。"""
        segs = []
        seg = self._make(lambda a: segs.append(a), min_speech_frames_to_keep=100)
        # 只喂几帧，远不够 100 帧
        seg.feed(np.full(1000, 0.5, dtype=np.float32))
        seg.finish()
        assert segs == [], "过短段应被丢弃"

    def test_int16_input_accepted(self):
        """int16 输入也能喂（内部转 float）。"""
        segs = []
        seg = self._make(lambda a: segs.append(a))
        seg.feed(np.full(2000, 16000, dtype=np.int16))
        seg.finish()
        assert len(segs) >= 1

    def test_on_speech_start_fires_once_per_utterance(self):
        """SILENCE→SPEECH 首次跳变时 on_speech_start 被调用一次，不重复触发。"""
        starts = []
        seg = self._make(lambda a: None, on_speech_start=lambda: starts.append(1))
        # 持续喂"语音"帧 → 触发一次 speech start
        seg.feed(np.full(3000, 0.5, dtype=np.float32))
        assert len(starts) == 1, f"首次说话应触发1次，实际{len(starts)}"
        # 继续喂（还在 IN_SPEECH）→ 不应再触发
        seg.feed(np.full(3000, 0.5, dtype=np.float32))
        assert len(starts) == 1, "同一段话内不应重复触发"

    def test_on_speech_start_not_fired_on_silence(self):
        """只有静音时 on_speech_start 不应被调用。

        注：threshold=0 时静音也被判为 speech，所以这里用正常 threshold=0.5
        + 真实全零静音（Silero prob≈0.001 < 0.5）验证。
        """
        from infrastructure.perception.voice_session import VADSegmenter

        starts = []
        seg = VADSegmenter(
            lambda a: None,
            on_speech_start=lambda: starts.append(1),
            threshold=0.5, silence_frames=3, min_speech_frames=2,
            model_path=_SILERO_PATH,
        )
        seg.feed(np.zeros(5000, dtype=np.int16))  # 真实静音
        assert len(starts) == 0, "静音不应触发 on_speech_start"

    def test_on_speech_start_fires_again_after_silence_gap(self):
        """说完一段、静音收尾后，第二段新开口应再次触发 on_speech_start。

        threshold=0 时合成静音也被判为 speech（prob≈0.001 ≥ 0），无法用合成
        数据造出真正的静音间隔。所以这里直接驱动状态机：手动 emit + reset
        模拟"第一段结束"，再喂第二段验证再次触发。
        """
        starts = []
        seg = self._make(
            lambda a: None,
            on_speech_start=lambda: starts.append(1),
            silence_frames=2, min_speech_frames=1, min_speech_frames_to_keep=1,
        )
        # 第一段话
        seg.feed(np.full(1500, 0.5, dtype=np.float32))
        assert len(starts) == 1, "第一段应触发1次"
        assert seg._in_speech is True
        # 手动模拟"静音收尾 → 第一段结束"（_emit_segment + _reset_speech）
        seg._emit_segment()
        seg._reset_speech()
        assert seg._in_speech is False, "reset 后应回到 SILENCE"
        # 第二段话（SILENCE→SPEECH 再次跳变）
        seg.feed(np.full(1500, 0.5, dtype=np.float32))
        assert len(starts) == 2, f"第二段应再次触发，实际{len(starts)}"


# ── VoiceSession：完整生命周期（用 FakeVAD 注入段）──────────────────────────


class _FakeSegmenter:
    """绕过真实 VAD：直接在 start() 后往 on_segment 灌入预定义的音频段。

    让 VoiceSession 的"分段→写 wav→ASR→落盘"链路可确定性测试，
    不依赖麦克风/语音样本/Silero 判定。
    """

    def __init__(self, on_segment, *, segments, delay=0.0):
        self._on_segment = on_segment
        self._segments = list(segments)  # list[np.ndarray int16]
        self._delay = delay

    def feed(self, chunk):
        pass  # FakeVAD 不消费实时流

    def finish(self):
        pass

    def emit_all(self):
        """测试驱动：依次把预定义段回调出去。"""
        import time

        for seg in self._segments:
            if self._delay:
                time.sleep(self._delay)
            self._on_segment(seg)


@pytest.fixture
def _mock_asr():
    """mock transcribe_file，避免真实网络调用。返回固定的段文本。"""
    calls: list[str] = []

    def _fake(path, *, config, segment_paths=None):
        calls.append(str(path))
        idx = len(calls)
        return {"ok": True, "text": f"段{idx}的转写", "segments": 1, "error": ""}

    with patch(
        "infrastructure.perception.voice_session.transcribe_file",
        side_effect=_fake,
    ) as m:
        yield m, calls


def test_voice_session_lifecycle_and_doc(tmp_path, _mock_asr):
    """端到端：注入 2 段 → 写 2 个 wav + 调 2 次 ASR + 文档含 2 段转写。

    用 FakeVAD 绕过麦克风和 Silero，直接验证编排逻辑。
    """
    mock_asr, asr_calls = _mock_asr
    from infrastructure.perception.voice_session import VoiceSession

    # 把 media_dir 重定向到 tmp_path，避免污染真实 apps/
    instance_id = "test-voice-session-iid"
    fake_media = tmp_path / "apps" / instance_id / "data" / "perception"

    # 预定义 2 段音频（int16，够长让 write_wav 有内容）
    seg1 = (np.sin(2 * np.pi * 220 * np.arange(16000) / 16000) * 10000).astype(np.int16)
    seg2 = (np.sin(2 * np.pi * 440 * np.arange(8000) / 16000) * 8000).astype(np.int16)
    fake_segmenter = _FakeSegmenter(None, segments=[seg1, seg2])

    with patch("infrastructure.perception.voice_session.media_dir", return_value=fake_media):
        sess = VoiceSession(
            instance_id,
            segmenter=fake_segmenter,  # type: ignore[arg-type]
        )
        # 把 fake 的 on_segment 接到 sess 的真实回调
        fake_segmenter._on_segment = sess._on_segment

        sess.start()
        # 模拟 VAD 检测到 2 段
        fake_segmenter.emit_all()
        summary = sess.stop()

    # ── 断言：会话摘要 ──
    assert summary["segments"] == 2
    doc = Path(summary["doc_path"])
    assert doc.exists(), "会话文档必须落盘"

    # ── 断言：文档结构 ──
    text = doc.read_text(encoding="utf-8")
    assert "# 语音会话转录" in text, "文档应有 header"
    assert "段 1" in text and "段 2" in text, "每段都应记录"
    assert "段1的转写" in text and "段2的转写" in text, "ASR 结果应追加"
    assert "# 会话结束" in text and "共 2 段" in text, "stop 应写结束标记"

    # ── 断言：每段写了 wav 文件 ──
    seg_dir = Path(summary["seg_dir"])
    wavs = sorted(seg_dir.glob("seg_*.wav"))
    assert len(wavs) == 2, f"应有 2 个段 wav，实际 {len(wavs)}"

    # ── 断言：ASR 被调了 2 次（每段一次）──
    assert len(asr_calls) == 2, f"ASR 应调 2 次，实际 {len(asr_calls)}"


def test_voice_session_asr_failure_records_error(tmp_path, _mock_asr):
    """ASR 抛异常时，文档里记录失败占位，不崩会话。"""
    mock_asr, _ = _mock_asr
    mock_asr.side_effect = RuntimeError("network down")

    from infrastructure.perception.voice_session import VoiceSession

    instance_id = "test-asr-fail-iid"
    fake_media = tmp_path / "apps" / instance_id / "data" / "perception"
    seg1 = np.full(2000, 1000, dtype=np.int16)
    fake_segmenter = _FakeSegmenter(None, segments=[seg1])

    with patch("infrastructure.perception.voice_session.media_dir", return_value=fake_media):
        sess = VoiceSession(instance_id, segmenter=fake_segmenter)  # type: ignore[arg-type]
        fake_segmenter._on_segment = sess._on_segment
        sess.start()
        fake_segmenter.emit_all()
        summary = sess.stop()

    doc = Path(summary["doc_path"]).read_text(encoding="utf-8")
    assert "转写失败" in doc, "ASR 失败应在文档记录占位"
    assert summary["segments"] == 1


def test_voice_session_empty_transcript_handled(tmp_path, _mock_asr):
    """ASR 返回空文本时，文档记"（语音转写为空）"，不报错。"""
    mock_asr, _ = _mock_asr
    mock_asr.side_effect = None
    mock_asr.return_value = {"ok": False, "text": "", "segments": 0, "error": "empty"}

    from infrastructure.perception.voice_session import VoiceSession

    instance_id = "test-empty-iid"
    fake_media = tmp_path / "apps" / instance_id / "data" / "perception"
    seg1 = np.full(2000, 1000, dtype=np.int16)
    fake_segmenter = _FakeSegmenter(None, segments=[seg1])

    with patch("infrastructure.perception.voice_session.media_dir", return_value=fake_media):
        sess = VoiceSession(instance_id, segmenter=fake_segmenter)  # type: ignore[arg-type]
        fake_segmenter._on_segment = sess._on_segment
        sess.start()
        fake_segmenter.emit_all()
        summary = sess.stop()

    doc = Path(summary["doc_path"]).read_text(encoding="utf-8")
    assert "语音转写为空" in doc


def test_voice_session_stop_idempotent(tmp_path, _mock_asr):
    """重复 stop() 不报错、返回一致摘要。"""
    from infrastructure.perception.voice_session import VoiceSession

    instance_id = "test-stop-iid"
    fake_media = tmp_path / "apps" / instance_id / "data" / "perception"
    fake_segmenter = _FakeSegmenter(None, segments=[])

    with patch("infrastructure.perception.voice_session.media_dir", return_value=fake_media):
        sess = VoiceSession(instance_id, segmenter=fake_segmenter)  # type: ignore[arg-type]
        fake_segmenter._on_segment = sess._on_segment
        sess.start()
        s1 = sess.stop()
        s2 = sess.stop()

    assert s1["session_id"] == s2["session_id"]
    assert s1["segments"] == s2["segments"]


def test_voice_session_start_idempotent(tmp_path, _mock_asr):
    """重复 start() 不重复拉子进程。"""
    from infrastructure.perception.voice_session import VoiceSession

    instance_id = "test-start-iid"
    fake_media = tmp_path / "apps" / instance_id / "data" / "perception"
    fake_segmenter = _FakeSegmenter(None, segments=[])

    with patch("infrastructure.perception.voice_session.media_dir", return_value=fake_media):
        sess = VoiceSession(instance_id, segmenter=fake_segmenter)  # type: ignore[arg-type]
        fake_segmenter._on_segment = sess._on_segment
        sess.start()
        proc1 = sess._proc
        sess.start()  # 再次 start
        assert sess._proc is proc1, "重复 start 不应重启子进程"
        sess.stop()
