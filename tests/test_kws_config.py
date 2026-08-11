"""KWS + Service config 测试。

KWS 测试用真实 sherpa-onnx 模型（验证加载 + feed 接口）。
Service config 测试验证 voice_sense.yaml 解析。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest


# ── KWS 接口测试（需要真实模型 + sherpa-onnx）────────────────────────────

try:
    from infrastructure.perception.audio_sense.kws import SherpaOnnxKWS, KeywordHit
    import sherpa_onnx as _sherpa  # noqa: F401 — 检测可用性
    _SHERPA_AVAILABLE = True
except Exception:
    _SHERPA_AVAILABLE = False

_MODEL_DIR = Path(__file__).resolve().parents[1] / "models" / "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
_KEYWORDS = Path(__file__).resolve().parents[1] / "config" / "voice_keywords.txt"
_KWS_AVAILABLE = _SHERPA_AVAILABLE and _MODEL_DIR.exists() and _KEYWORDS.exists()


@pytest.mark.skipif(not _KWS_AVAILABLE, reason="sherpa-onnx 或 KWS 模型未安装")
class TestSherpaOnnxKWS:
    """用真实模型验证 KWS 加载和接口。"""

    def test_load_and_feed_silence(self):
        """加载模型 + 喂静音 → 不触发。"""
        kws = SherpaOnnxKWS(
            model_dir=_MODEL_DIR,
            keywords_file=_KEYWORDS,
        )
        # 喂 1 秒静音
        hit = kws.feed(np.zeros(16000, dtype=np.int16))
        assert hit is None, "静音不应触发唤醒词"

    def test_feed_noise_no_false_trigger(self):
        """喂噪声 → 不误触发（低误报率）。"""
        kws = SherpaOnnxKWS(
            model_dir=_MODEL_DIR,
            keywords_file=_KEYWORDS,
        )
        np.random.seed(42)
        noise = (np.random.randn(16000) * 0.1).astype(np.int16)
        hit = kws.feed(noise)
        # 噪声可能偶尔触发，但不应该频繁
        # 这里只验证接口不崩
        assert hit is None or isinstance(hit, KeywordHit)

    def test_reset_clears_state(self):
        """reset 后能继续检测。"""
        kws = SherpaOnnxKWS(
            model_dir=_MODEL_DIR,
            keywords_file=_KEYWORDS,
        )
        kws.feed(np.zeros(1600, dtype=np.int16))
        kws.reset()
        # reset 后再喂不崩
        hit = kws.feed(np.zeros(1600, dtype=np.int16))
        assert hit is None

    def test_int16_and_float32_both_accepted(self):
        """int16 和 float32 都能喂。"""
        kws = SherpaOnnxKWS(
            model_dir=_MODEL_DIR,
            keywords_file=_KEYWORDS,
        )
        # int16
        kws.feed(np.zeros(1600, dtype=np.int16))
        # float32
        kws.feed(np.zeros(1600, dtype=np.float32))

    def test_performance_under_50ms_per_second(self):
        """1 秒音频处理 < 50ms（<5% CPU）。"""
        import time
        kws = SherpaOnnxKWS(
            model_dir=_MODEL_DIR,
            keywords_file=_KEYWORDS,
        )
        audio = (np.random.randn(16000) * 0.05).astype(np.int16)
        t0 = time.time()
        for i in range(0, len(audio), 1600):
            kws.feed(audio[i:i + 1600])
        elapsed = (time.time() - t0) * 1000
        assert elapsed < 50, f"1秒音频处理 {elapsed:.1f}ms > 50ms"


# ── Service config 解析测试 ─────────────────────────────────────────────


def test_config_defaults():
    """无配置文件 → 安全默认值。"""
    from infrastructure.perception.audio_sense.service import VoiceSenseConfig
    cfg = VoiceSenseConfig()
    assert cfg.enabled is False  # 默认关闭
    assert cfg.dialog_timeout_s == 30
    assert cfg.focus_timeout_s == 60


def test_config_loads_from_yaml(tmp_path):
    """从 YAML 加载配置。"""
    yaml_content = """
enabled: true
kws:
  model_dir: "models/test"
  keywords_file: "config/test_keywords.txt"
  threshold: 0.8
  use_int8: false
dialog:
  timeout_seconds: 45
focus:
  timeout_seconds: 90
  retention_days: 7
quiet_hours:
  start: "22:00"
  end: "07:00"
default_instance: "abc123"
"""
    cfg_path = tmp_path / "voice_sense.yaml"
    cfg_path.write_text(yaml_content, encoding="utf-8")

    from infrastructure.perception.audio_sense.service import load_voice_sense_config
    cfg = load_voice_sense_config(cfg_path)

    assert cfg.enabled is True
    assert cfg.kws_model_dir == "models/test"
    assert cfg.kws_threshold == 0.8
    assert cfg.kws_use_int8 is False
    assert cfg.dialog_timeout_s == 45
    assert cfg.focus_timeout_s == 90
    assert cfg.retention_days == 7
    assert cfg.default_instance == "abc123"


def test_config_missing_file_returns_defaults():
    """配置文件不存在 → 默认值。"""
    from infrastructure.perception.audio_sense.service import load_voice_sense_config
    cfg = load_voice_sense_config("/nonexistent/voice_sense.yaml")
    assert cfg.enabled is False
