"""LiveTranscriber 实时增量转写测试（纯逻辑，mock VAD + mock ASR）。

验证核心承诺：
  - 停顿切割：VAD 段回调 → 攒批 → 派发转写
  - 停止时只等尾巴：stop_and_finalize flush 尾段 + 等 worker 排空
  - 拼接保序：结果按 seg_idx 排序
  - prompt 续链：下一段转写带上一段文本
  - 失败安全：转写异常/无语音 → 返回空串（调用方 fallback）
"""
from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from infrastructure.perception.live_transcribe import LiveTranscriber


def _speech_pcm(seconds: float, *, amplitude: int = 8000, sr: int = 16000) -> np.ndarray:
    """合成"语音"：440Hz 正弦 int16（VAD mock 不看内容，仅计数用）。"""
    t = np.arange(int(seconds * sr)) / sr
    wave_ = (np.sin(2 * np.pi * 440 * t) * amplitude).astype(np.int16)
    return wave_


class FakeVAD:
    """VAD mock：feed 全部缓存，测试用 flush_segments() 手动模拟"停顿切段"。"""

    def __init__(self, on_segment):
        self._on_segment = on_segment
        self.fed = 0
        self.finished = False

    def feed(self, pcm):
        self.fed += len(pcm)

    def finish(self):
        self.finished = True

    def emit(self, audio: np.ndarray) -> None:
        """模拟一次停顿切段。"""
        self._on_segment(audio)


class TestLiveTranscriber:
    def _make(self, tmp_path, *, transcribe_fn=None, **kw):
        audio = tmp_path / "audio_1.wav"
        audio.write_bytes(b"\x00" * 44)  # 假 wav 头
        vad = FakeVAD(on_segment=lambda a: None)
        lt = LiveTranscriber(
            audio, config=None,  # type: ignore[arg-type] 测试不触云端
            transcribe_fn=transcribe_fn, vad=vad, poll_interval=0.01, **kw,
        )
        # 重新绑定 FakeVAD 回调（构造后替换，捕获 lt 的内部方法）
        vad._on_segment = lt._on_vad_segment
        return lt, vad

    def test_stop_and_finalize_joins_segments_in_order(self, tmp_path):
        """两段已转 + 尾段 flush → 按序拼接，无重复。"""
        calls = []

        def fake_asr(audio_bytes, *, filename, config, prompt):
            calls.append((filename, prompt))
            return f"text-{len(calls)}"

        lt, vad = self._make(tmp_path, transcribe_fn=fake_asr, min_segment_seconds=0.0)
        lt.start()
        vad.emit(_speech_pcm(1.0))   # 段1 → 立即派发（min=0）
        vad.emit(_speech_pcm(2.0))   # 段2 → 立即派发
        time.sleep(0.1)              # worker 消化
        text = lt.stop_and_finalize(timeout=5)
        assert "text-1" in text and "text-2" in text
        assert text.index("text-1") < text.index("text-2")

    def test_prompt_chaining_between_segments(self, tmp_path):
        """第二段转写收到第一段的文本作为 prompt（跨段连贯）。"""
        prompts = []

        def fake_asr(audio_bytes, *, filename, config, prompt):
            prompts.append(prompt)
            return f"seg{len(prompts)}"

        lt, vad = self._make(tmp_path, transcribe_fn=fake_asr, min_segment_seconds=0.0)
        lt.start()
        vad.emit(_speech_pcm(1.0))
        time.sleep(0.1)
        vad.emit(_speech_pcm(1.0))
        lt.stop_and_finalize(timeout=5)
        assert prompts[0] == ""
        assert prompts[1] == "seg1"

    def test_min_segment_seconds_buffers_small_segments(self, tmp_path):
        """小于 min_segment_seconds 的段先攒着，不派发；攒够才一起发。"""
        n_calls = []

        def fake_asr(audio_bytes, *, filename, config, prompt):
            n_calls.append(filename)
            return "x"

        lt, vad = self._make(tmp_path, transcribe_fn=fake_asr, min_segment_seconds=5.0)
        lt.start()
        vad.emit(_speech_pcm(2.0))  # 2s < 5s → 攒
        time.sleep(0.05)
        assert n_calls == []        # 未派发
        vad.emit(_speech_pcm(2.0))  # 累计 4s < 5s → 仍攒
        time.sleep(0.05)
        assert n_calls == []
        vad.emit(_speech_pcm(2.0))  # 累计 6s ≥ 5s → 派发一批
        time.sleep(0.1)
        assert len(n_calls) == 1
        lt.stop_and_finalize(timeout=5)

    def test_finalize_flushes_tail_buffer(self, tmp_path):
        """stop 时还在攒的 buffer（不足 min）也要 flush 成尾段转写。"""
        got = []

        def fake_asr(audio_bytes, *, filename, config, prompt):
            got.append(len(audio_bytes))
            return "tail"

        lt, vad = self._make(tmp_path, transcribe_fn=fake_asr, min_segment_seconds=99.0)
        lt.start()
        vad.emit(_speech_pcm(1.0))  # 永远攒不够 → 只能靠 finalize flush
        text = lt.stop_and_finalize(timeout=5)
        assert len(got) == 1          # 尾段被转写了
        assert text == "tail"

    def test_transcribe_failure_returns_empty(self, tmp_path):
        """所有段转写失败 → 返回空串（daemon fallback 整文件）。"""
        def bad_asr(audio_bytes, *, filename, config, prompt):
            raise RuntimeError("asr down")

        lt, vad = self._make(tmp_path, transcribe_fn=bad_asr, min_segment_seconds=0.0)
        lt.start()
        vad.emit(_speech_pcm(1.0))
        assert lt.stop_and_finalize(timeout=5) == ""

    def test_no_speech_returns_empty(self, tmp_path):
        """全程无语音 → 空串，不调 ASR。"""
        called = []

        def fake_asr(audio_bytes, *, filename, config, prompt):
            called.append(1)
            return "x"

        lt, _ = self._make(tmp_path, transcribe_fn=fake_asr)
        lt.start()
        assert lt.stop_and_finalize(timeout=5) == ""
        assert called == []

    def test_tail_loop_reads_growing_wav(self, tmp_path):
        """tail 线程真的从增长中的 wav 读 PCM 喂 VAD（集成冒烟）。"""
        audio = tmp_path / "grow.wav"
        audio.write_bytes(b"\x00" * 44)

        fed = []

        class RecordingVAD(FakeVAD):
            def feed(self, pcm):
                fed.append(len(pcm))
                # 满 0.2s（3200 样本）模拟切段，触发 flush → 无需 finalize
                super().feed(pcm)

        lt = LiveTranscriber(
            audio, config=None,  # type: ignore[arg-type]
            transcribe_fn=lambda *a, **k: "ok",
            poll_interval=0.01, min_segment_seconds=0.0,
        )
        vad = RecordingVAD(on_segment=lt._on_vad_segment)
        lt._vad_ext = vad
        lt.start()

        # 模拟录音进程增量写 PCM（3 块 0.2s）
        for _ in range(3):
            with open(audio, "ab") as f:
                f.write(_speech_pcm(0.2).tobytes())
            time.sleep(0.05)

        assert sum(fed) == pytest.approx(9600, abs=1600)  # 全部样本被读到
        lt.stop_and_finalize(timeout=5)
        assert vad.finished
