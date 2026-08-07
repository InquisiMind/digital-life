"""语音输出通道（TTS）—— 本地播放，不依赖飞书。

使用 edge-tts（微软神经网络语音，云端合成、本地播放）。
中文男声默认 zh-CN-YunxiNeural（云希），质量远超 macOS say。
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# edge-tts 神经语音（默认云希 = 年轻自然中文男声）
DEFAULT_VOICE = "zh-CN-YunxiNeural"
# 是否启用 TTS（环境变量 / 实例配置控制）
DEFAULT_ENABLED = os.getenv("DIGITAL_LIFE_TTS", "0") == "1"


def _clean_text_for_tts(text: str) -> str:
    """清理文本里的标记，让 TTS 读起来自然。

    - 去掉 <at user_id="ou_xxx"></at> 标签 → 纯文本
    - 去掉 markdown 格式标记（**、#、`、| 等）
    - 去掉过长空白
    """
    # 去 @ 标签
    text = re.sub(r"<at[^>]*>.*?</at>", "", text)
    text = re.sub(r"<at[^>]*/>", "", text)
    # 去 markdown 粗体/代码/标题
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\|", " ", text)  # 表格分隔符
    # 去多余空白
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def speak(text: str, *, voice: str = DEFAULT_VOICE, rate: str = "+0%") -> bool:
    """用 edge-tts 合成语音 + afplay 播放。返回是否成功。

    异步播放（在后台线程跑），不阻塞调用方。
    edge-tts 是微软云端神经网络语音，中文质量极好。

    Args:
        text: 要播放的文本（会自动清理标记）
        voice: edge-tts 语音名（zh-CN-YunxiNeural 云希 / zh-CN-YunyangNeural 云扬 等）
        rate: 语速调整（"+0%" 正常 / "+10%" 稍快）
    """
    clean = _clean_text_for_tts(text)
    if not clean:
        return False

    def _play():
        tmp_mp3 = None
        try:
            # edge-tts 合成到临时 mp3
            tmp_mp3 = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False, dir="/tmp")
            tmp_mp3.close()
            result = subprocess.run(
                ["edge-tts", "--voice", voice, "--rate", rate,
                 "--text", clean, "--write-media", tmp_mp3.name],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                timeout=30,
            )
            if result.returncode != 0:
                err = result.stderr.decode()[:200] if result.stderr else "unknown"
                logger.warning("edge-tts failed: %s", err)
                # 降级到 macOS say
                _fallback_say(clean, voice)
                return
            # afplay 播放
            subprocess.run(["afplay", tmp_mp3.name],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
            logger.info("voice TTS played: %d chars (voice=%s)", len(clean), voice)
        except subprocess.TimeoutExpired:
            logger.warning("voice TTS timeout")
        except FileNotFoundError:
            logger.warning("edge-tts not found, fallback to say")
            _fallback_say(clean, voice)
        except Exception as exc:
            logger.warning("voice TTS failed: %s", exc)
            _fallback_say(clean, voice)
        finally:
            if tmp_mp3:
                try:
                    os.unlink(tmp_mp3.name)
                except Exception:
                    pass

    threading.Thread(target=_play, daemon=True).start()
    return True


# edge-tts voice → macOS say voice 的降级映射
_SAY_FALLBACK = {
    "zh-CN-YunxiNeural": "Reed",
    "zh-CN-YunyangNeural": "Reed",
    "zh-CN-YunjianNeural": "Rocko",
    "zh-CN-XiaoxiaoNeural": "Tingting",
    "zh-CN-XiaoyiNeural": "Tingting",
}


def _fallback_say(text: str, edge_voice: str) -> None:
    """edge-tts 不可用时降级到 macOS say。"""
    say_voice = _SAY_FALLBACK.get(edge_voice, "Reed")
    try:
        subprocess.run(["/usr/bin/say", "-v", say_voice, text],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
    except Exception:
        pass


def is_tts_enabled(instance_id: str | None = None) -> bool:
    """检查 TTS 是否启用。

    优先读实例 app.yaml 的 perception.tts_enabled，fallback 到环境变量。
    """
    if instance_id:
        try:
            import yaml

            cfg_path = Path(__file__).resolve().parents[2] / "apps" / instance_id / "config" / "app.yaml"
            if cfg_path.exists():
                cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
                perc = cfg.get("perception") or {}
                return bool(perc.get("tts_enabled", False))
        except Exception:
            pass
    return DEFAULT_ENABLED


def get_tts_voice(instance_id: str | None = None) -> str:
    """读取配置的 TTS 语音。"""
    if instance_id:
        try:
            import yaml

            cfg_path = Path(__file__).resolve().parents[2] / "apps" / instance_id / "config" / "app.yaml"
            if cfg_path.exists():
                cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
                perc = cfg.get("perception") or {}
                voice = (perc.get("tts_voice") or "").strip()
                if voice:
                    return voice
        except Exception:
            pass
    return DEFAULT_VOICE
