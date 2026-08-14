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
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# edge-tts 神经语音（默认云希 = 年轻自然中文男声）
DEFAULT_VOICE = "zh-CN-YunxiNeural"
# 是否启用 TTS（环境变量 / 实例配置控制）
DEFAULT_ENABLED = os.getenv("DIGITAL_LIFE_TTS", "0") == "1"


def _clean_text_for_tts(text: str) -> str:
    """最小清理：只去掉 @ 标签和多余空白。

    技术细节（URL/路径/代码）的过滤应该由模型在生成回复时自己注意，
    不在后处理硬编码——否则会生硬地删掉内容。
    """
    # 去 @ 标签
    text = re.sub(r"<at[^>]*>.*?</at>", "", text)
    text = re.sub(r"<at[^>]*/>", "", text)
    # 去多余空白
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _is_mostly_cjk(text: str) -> bool:
    """文本是否以中文为主（用于 say 降级判定——Tingting 读中文可以，读英文很怪）。"""
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    letters = sum(1 for c in text if c.isalpha())
    return letters == 0 or cjk / letters >= 0.5


def _stderr_tail(stderr: bytes) -> str:
    """取 stderr 的最后一个异常行（traceback 头几行没信息量）。"""
    lines = (stderr or b"").decode(errors="replace").strip().splitlines()
    for line in reversed(lines):
        if line.strip() and not line.startswith(" "):
            return line.strip()[:120]
    return lines[-1][:120] if lines else "unknown"


def speak(text: str, *, voice: str = DEFAULT_VOICE, rate: str = "+0%") -> bool:
    """用 edge-tts 合成语音 + afplay 播放。返回是否成功。

    异步播放（在后台线程跑），不阻塞调用方。
    edge-tts 是微软云端神经网络语音，中文质量极好；免费端点有间歇抖动
    （NoAudioReceived 快速失败 / 偶发 WebSocket 挂死），用退避重试扛。

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
            tmp_mp3 = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False, dir="/tmp")
            tmp_mp3.close()
            # edge-tts 合成：3 次退避重试（1s / 3s）——抖动窗口通常几秒即过
            played = False
            backoffs = (0, 1, 3)
            for attempt, backoff in enumerate(backoffs, start=1):
                if backoff:
                    time.sleep(backoff)
                try:
                    result = subprocess.run(
                        ["edge-tts", "--voice", voice, "--rate", rate,
                         "--text", clean, "--write-media", tmp_mp3.name],
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                        timeout=20,
                    )
                except subprocess.TimeoutExpired:
                    logger.warning("edge-tts attempt %d timed out (20s)", attempt)
                    continue
                if result.returncode == 0 and os.path.getsize(tmp_mp3.name) > 0:
                    played = True
                    break
                logger.warning("edge-tts attempt %d failed: %s",
                               attempt, _stderr_tail(result.stderr))
            if not played:
                # 全部重试失败：中文文本降级 macOS Tingting（保住"有声音"）；
                # 英文为主的文本不降级（say 读英文太怪，不如不说）
                if _is_mostly_cjk(clean):
                    logger.warning("edge-tts all attempts failed, fallback to say (Tingting)")
                    _fallback_say(clean, voice)
                else:
                    logger.warning("edge-tts all attempts failed, skip TTS (non-CJK text)")
                return
            # afplay 播放
            subprocess.run(["afplay", tmp_mp3.name],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
            logger.info("voice TTS played: %d chars (voice=%s)", len(clean), voice)
        except FileNotFoundError:
            logger.warning("edge-tts not found, skip TTS")
        except Exception as exc:
            logger.warning("voice TTS failed: %s", exc)
        finally:
            if tmp_mp3:
                try:
                    os.unlink(tmp_mp3.name)
                except Exception:
                    pass

    threading.Thread(target=_play, daemon=True).start()
    return True


# edge-tts voice → macOS say voice 的降级映射（中文场景统一 Tingting）
_SAY_FALLBACK = {
    "zh-CN-YunxiNeural": "Tingting",
    "zh-CN-YunyangNeural": "Tingting",
    "zh-CN-YunjianNeural": "Tingting",
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
