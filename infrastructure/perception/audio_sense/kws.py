"""L4 唤醒词检测 —— sherpa-onnx KeywordSpotter 封装。

持续接收 PCM 流，检测唤醒词（zero/alpha 的中文近似音）。
命中时触发回调，由 router 决定后续状态转换。

可插拔设计：WakeWordDetector 接口抽象，sherpa-onnx 是本期实现。
后续可替换为专用唤醒词模型（openWakeWord 等），router 层零改动。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000


@dataclass
class KeywordHit:
    """唤醒词命中结果。"""

    keyword: str        # 命中的关键词原文（如 "塞罗" = zero 的近似音）
    raw_text: str       # sherpa-onnx 返回的原始文本


class WakeWordDetector(Protocol):
    """唤醒词检测器接口（可插拔）。

    所有实现提供统一的 feed 接口：喂 PCM 块，返回命中或 None。
    router 层不关心底层是 sherpa-onnx 还是其他模型。
    """

    def feed(self, pcm: np.ndarray) -> KeywordHit | None:
        """喂一个 PCM 块（int16 或 float32），返回命中或 None。"""
        ...

    def reset(self) -> None:
        """重置检测器状态（状态转换时调，清空累积的音频上下文）。"""
        ...


class SherpaOnnxKWS:
    """sherpa-onnx KeywordSpotter 封装。

    本期实现：基于 zipformer-wenetspeech 中文 KWS 模型（int8，~5MB）。
    持续接收 PCM 流，检测 keywords.txt 里配置的唤醒词。

    性能（实测）：1 秒音频 ~22ms 处理（2.2% CPU），噪声不误触发。
    """

    def __init__(
        self,
        model_dir: str | Path,
        keywords_file: str | Path,
        *,
        use_int8: bool = True,
        num_threads: int = 1,
        keywords_threshold: float = 0.5,
        keywords_score: float = 1.0,
    ) -> None:
        """Args:
            model_dir: KWS 模型目录（含 encoder/decoder/joiner/tokens）。
            keywords_file: 关键词文件（拼音 token + @原词 格式）。
            use_int8: 用 int8 量化模型（更小更快，精度略低）。
            num_threads: ONNX 推理线程数（1 够用，避免抢 CPU）。
            keywords_threshold: 触发阈值（越高越严格，默认 0.5）。
            keywords_score: 关键词 boost 分数（提高关键词 vs 非关键词的概率）。
        """
        import sherpa_onnx

        self._sherpa_onnx = sherpa_onnx
        model_dir = Path(model_dir)
        # 官方示例：encoder/joiner 用 int8（快），decoder 用 fp32（精度）。
        # 全 int8 在某些模型上检测率低。
        enc_suffix = "int8." if use_int8 else ""
        joi_suffix = "int8." if use_int8 else ""
        enc = _find_model_file(model_dir, "encoder", enc_suffix)
        dec = _find_model_file(model_dir, "decoder", "")  # decoder 用 fp32
        joi = _find_model_file(model_dir, "joiner", joi_suffix)
        tok = model_dir / "tokens.txt"

        self._kws = sherpa_onnx.KeywordSpotter(
            encoder=str(enc),
            decoder=str(dec),
            joiner=str(joi),
            tokens=str(tok),
            keywords_file=str(keywords_file),
            num_threads=num_threads,
            provider="cpu",
            keywords_threshold=keywords_threshold,
            keywords_score=keywords_score,
        )
        self._stream = self._kws.create_stream()
        self._last_result = ""
        logger.info("SherpaOnnxKWS loaded: model=%s keywords=%s", model_dir.name, keywords_file)

    def feed(self, pcm: np.ndarray) -> KeywordHit | None:
        """喂 PCM 块（int16 或 float32），返回命中或 None。

        sherpa-onnx 要求 float32 输入；int16 自动转换。
        """
        # int16 → float32
        if pcm.dtype == np.int16:
            audio = pcm.astype(np.float32) / 32768.0
        else:
            audio = pcm.astype(np.float32)

        self._stream.accept_waveform(SAMPLE_RATE, audio)
        while self._kws.is_ready(self._stream):
            self._kws.decode_stream(self._stream)

        result = self._kws.get_result(self._stream)
        if result and result != self._last_result:
            self._last_result = result
            # 命中后重置 stream（sherpa-onnx 推荐：reset 后才能检测下一个词）
            try:
                self._kws.reset_stream(self._stream)
            except Exception:
                # 某些版本没有 reset_stream，重建 stream
                self._stream = self._kws.create_stream()
            logger.info("KWS hit: %s", result)
            return KeywordHit(keyword=result.strip(), raw_text=result.strip())
        return None

    def reset(self) -> None:
        """重置检测状态。"""
        try:
            self._kws.reset_stream(self._stream)
        except Exception:
            self._stream = self._kws.create_stream()
        self._last_result = ""


def _find_model_file(model_dir: Path, component: str, suffix: str) -> Path:
    """在模型目录里找指定组件（encoder/decoder/joiner）的 onnx 文件。

    兼容不同版本文件名模式：
      encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx
      encoder-epoch-12-avg-2-chunk-16-kws-fp32.onnx
    """
    pattern = f"{component}-*{suffix}onnx"
    matches = list(model_dir.glob(pattern))
    if not matches:
        # 回退：不限定 suffix
        matches = list(model_dir.glob(f"{component}-*.onnx"))
        # 排除非 suffix 版本（如果 use_int8=True 但只有 fp32，用 fp32）
    if not matches:
        raise FileNotFoundError(f"找不到 {component} 模型文件 in {model_dir}")
    return matches[0]
