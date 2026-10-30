"""Silero VAD（语音活动检测）——2026-10-30 zhp 拍板接入，业界标准三层
endpointing 的声学层。

背景：此前"听感"全靠能量阈值（挡不住人声频杂音）+ 讯飞转写出字（事后
文本，慢且不可控），灰字/拖事件/误触发都是这一个根。Silero 是训练过的
语音检测器（640KB ONNX，CPU 实时 RTF≈0.0005，sherpa-onnx 已内置运行时），
帧级区分人声与键盘/空调/纯音调杂音。

职责边界（与文本层的分工）：
- VAD 管"有没有人声"：PCM 门控（杂音不喂讯飞）、语末计时刷新、
  endpointing 快路（说完更早成事件）。
- 文本层管"说的是不是回声"：打断判定仍走 partial 与播报文本比对
  （VAD 分不清真人说话和 TTS 回声——回声防线保留在文本层）。

模型缺失/加载失败 → 全链路自动降级回能量门控，绝不阻塞语音。
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

_MODEL_PATH = Path(__file__).resolve().parents[2] / "models" / "vad" / "silero_vad.onnx"

# silero 窗口 512 样本（32ms @16k）；sherpa 要求按窗对齐喂
_WINDOW = 512


class SpeechVAD:
    """每会话一个实例（内含流式语音段状态），模型进程级共享懒加载。"""

    _shared_model_ready = False
    _shared_lock = threading.Lock()

    def __init__(self, threshold: float = 0.5) -> None:
        self._vad = None
        self._buf = np.zeros(0, dtype=np.float32)
        self._last_speech = False
        if not SpeechVAD._ensure_model():
            return
        try:
            import sherpa_onnx

            cfg = sherpa_onnx.SileroVadModelConfig(
                model=str(_MODEL_PATH),
                threshold=threshold,
                # 段级参数对帧级判定影响小；取常用默认
                min_silence_duration=0.25,
                min_speech_duration=0.25,
                max_speech_duration=20.0,
            )
            self._vad = sherpa_onnx.VoiceActivityDetector(
                sherpa_onnx.VadModelConfig(silero_vad=cfg)
            )
        except Exception:
            logger.warning("silero VAD init failed — fallback energy gate", exc_info=True)
            self._vad = None

    @staticmethod
    def _ensure_model() -> bool:
        if SpeechVAD._shared_model_ready:
            return True
        with SpeechVAD._shared_lock:
            if SpeechVAD._shared_model_ready:
                return True
            ok = _MODEL_PATH.exists()
            if not ok:
                logger.warning(
                    "silero VAD model not found at %s — fallback energy gate", _MODEL_PATH
                )
            SpeechVAD._shared_model_ready = ok
            return ok

    @property
    def available(self) -> bool:
        return self._vad is not None

    def feed(self, pcm_16k_s16le: bytes) -> bool | None:
        """喂任意长度 PCM（int16 字节），返回本段最后一窗是否人声。

        None = VAD 不可用（调用方降级）；bool = 帧级判定。
        """
        if self._vad is None:
            return None
        samples = np.frombuffer(pcm_16k_s16le, dtype=np.int16).astype(np.float32) / 32768.0
        self._buf = np.concatenate([self._buf, samples]) if self._buf.size else samples
        n = (self._buf.size // _WINDOW) * _WINDOW
        if n == 0:
            return self._last_speech
        chunk, self._buf = self._buf[:n], self._buf[n:]
        for i in range(0, n, _WINDOW):
            self._vad.accept_waveform(chunk[i : i + _WINDOW])
            self._last_speech = self._vad.is_speech_detected()
        return self._last_speech
