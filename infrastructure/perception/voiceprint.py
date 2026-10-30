"""声纹门禁（2026-10-29 zhp：办公环境过滤同事说话，只转写/注入"你"的声音）。

模型：sherpa-onnx + 3D-Speaker ERes2NetV2 中文声纹嵌入（models/speaker-embed/）。
原理：注册你的声纹（15 秒录音）→ 每句成句后提取该句音频的嵌入向量 → 与注册
声纹比余弦相似度 → 过阈值 = 你说的，注入 agent；不过 = 别人在说，静默丢弃。

设计：
- 嵌入提取器进程级懒加载单例（模型 ~70MB ONNX，首次加载 ~1s）
- 注册声纹存 apps/<iid>/data/voiceprint.npy（numpy 向量）
- web_voice 在 _on_sentence 时用 RTASR 的 bg/ed 时间戳从 PCM 环形缓冲切片
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

_MODEL_PATH = Path(__file__).resolve().parents[2] / "models" / "speaker-embed" / "eres2netv2_zh.onnx"

# 余弦相似度判定阈值。实测：同一人不同句 ~0.6-0.8，不同人 ~0.1-0.3。
# 取 0.45 偏保守（宁可多过滤也不要误注入——同事的话不该到 agent 那里）。
DEFAULT_THRESHOLD = 0.45

_extractor = None
_extractor_lock = threading.Lock()
_extractor_failed = False  # 模型不可用（未下载/损坏）→ 门禁自动跳过，不阻塞链路


def _get_extractor():
    """懒加载声纹嵌入提取器（进程级单例）。不可用时返回 None（门禁跳过）。"""
    global _extractor, _extractor_failed
    if _extractor_failed:
        return None
    if _extractor is not None:
        return _extractor
    with _extractor_lock:
        if _extractor is not None:
            return _extractor
        if not _MODEL_PATH.exists():
            logger.warning(
                "voiceprint model not found at %s — gate disabled (download it to enable)",
                _MODEL_PATH,
            )
            _extractor_failed = True
            return None
        try:
            import sherpa_onnx

            cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=str(_MODEL_PATH), num_threads=2,
            )
            _extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
            logger.info("voiceprint extractor loaded (dim=%d)", _extractor.dim)
            return _extractor
        except Exception as exc:
            logger.warning("voiceprint extractor load failed — gate disabled: %s", exc)
            _extractor_failed = True
            return None


def embed(pcm_16k_s16le: bytes) -> np.ndarray | None:
    """提取一段 PCM 的声纹嵌入向量。失败返回 None。"""
    ext = _get_extractor()
    if ext is None:
        return None
    samples = np.frombuffer(pcm_16k_s16le, dtype=np.int16).astype(np.float32) / 32768.0
    if len(samples) < 16000 * 0.5:  # <0.5 秒不足以提取可靠嵌入
        return None
    try:
        # sherpa-onnx 2.x 是流式接口（v1 的 ext.extract(samples) 已不存在，
        # 2026-10-30 注册"成功却无声纹"的无感知失败就是它 AttributeError 被吞）
        stream = ext.create_stream()
        try:
            stream.accept_waveform(16000.0, samples)
        except TypeError:  # 旧版绑定无采样率参数
            stream.accept_waveform(samples)
        stream.input_finished()
        if not ext.is_ready(stream):
            logger.warning("voiceprint embed: stream not ready (audio too short?)")
            return None
        return ext.compute(stream)
    except Exception:
        # warning 而非 debug：提取失败=注册/门禁静默失效，必须可见
        logger.warning("voiceprint embed failed", exc_info=True)
        return None


def voiceprint_path(instance_id: str) -> Path:
    """注册声纹文件路径。"""
    from infrastructure.config import get_project_root

    return get_project_root() / "apps" / instance_id / "data" / "voiceprint.npy"


def is_enrolled(instance_id: str) -> bool:
    """该实例是否已注册声纹。"""
    return voiceprint_path(instance_id).exists()


def enroll(instance_id: str, pcm_16k_s16le: bytes) -> bool:
    """注册声纹（15 秒左右音频 → 存嵌入向量）。成功返回 True。"""
    emb = embed(pcm_16k_s16le)
    if emb is None:
        return False
    path = voiceprint_path(instance_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.save(path, emb)
    logger.info("voiceprint enrolled for %s (dim=%d)", instance_id[:8], len(emb))
    return True


def clear(instance_id: str) -> None:
    """清除注册声纹（门禁随之关闭）。"""
    try:
        voiceprint_path(instance_id).unlink(missing_ok=True)
    except Exception:
        pass


def verify(instance_id: str, pcm_16k_s16le: bytes, threshold: float = DEFAULT_THRESHOLD) -> float | None:
    """验证一段音频是否是注册用户的声音。返回相似度分数。

    - 返回 None = 无法验证（模型不可用/音频太短/未注册）→ 调用方应放行
    - 返回分数 ≥ threshold = 用户本人
    - 返回分数 < threshold = 他人
    """
    if not is_enrolled(instance_id):
        return None
    enrolled = np.load(voiceprint_path(instance_id))
    emb = embed(pcm_16k_s16le)
    if emb is None or enrolled is None:
        return None
    sim = float(np.dot(emb, enrolled) / (np.linalg.norm(emb) * np.linalg.norm(enrolled)))
    return sim
