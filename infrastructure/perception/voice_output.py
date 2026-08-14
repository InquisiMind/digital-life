"""语音输出通道（TTS）—— 本地播放，不依赖飞书。

使用 edge-tts（微软神经网络语音，云端合成、本地播放）。
中文男声默认 zh-CN-YunxiNeural（云希），质量远超 macOS say。
"""
from __future__ import annotations

import logging
import os
import queue
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
    """语音播报一段文本（排队，异步）。返回是否成功入队。

    多次调用自动排队串行播放（zero 可以分多次表达，不会叠音）；
    stop_playback() 打断当前播放并丢弃全部未播的排队项。
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
    _ensure_player()
    _tts_queue.put((clean, voice, rate, _generation))
    return True


# ── 播放队列：单线程串行合成+播放（多次表达自动排队）─────────────────────────
_tts_queue: "queue.Queue[tuple[str, str, str, int]]" = queue.Queue()
_player_lock = threading.Lock()
_player_thread: threading.Thread | None = None
# 打断代数：stop_playback 时 +1，使入队/合成中的旧代语音全部失效
_generation = 0


def _ensure_player() -> None:
    """懒启动播放线程（第一次 speak 时起，常驻）。"""
    with _player_lock:
        global _player_thread
        if _player_thread is None or not _player_thread.is_alive():
            _player_thread = threading.Thread(target=_player_loop, daemon=True)
            _player_thread.start()


# 端点健康状态：抖动期后探测恢复，避免每句都撞 2 次失败再降级
_endpoint_healthy = True
_endpoint_probe_lock = threading.Lock()


def _bg_recover_voice(voice: str, rate: str, gen: int, pending_text: str) -> None:
    """快速窗口失败后的后台恢复：长退避重试 edge-tts，成功即标记端点恢复。

    - pending_text 非空（英文文本没走 say 降级）：重试成功直接补播
      （英文不能用 Tingting 兜底，这条必须由 edge-tts 说出来）
    - pending_text 为空（中文已用 Tingting 说过了）：只做恢复探测，
      不补播——收益是下一句立刻回到正常音色 + 零额外等待
    """
    global _endpoint_healthy
    with _endpoint_probe_lock:
        _endpoint_healthy = False

    def _recover():
        try:
            for backoff in (8, 15, 30):
                time.sleep(backoff)
                if gen != _generation:
                    return
                tmp = None
                try:
                    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False, dir="/tmp")
                    tmp.close()
                    result = subprocess.run(
                        ["edge-tts", "--voice", voice, "--rate", rate,
                         "--text", pending_text or "嗯", "--write-media", tmp.name],
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                        timeout=20,
                    )
                    if result.returncode == 0 and os.path.getsize(tmp.name) > 0:
                        with _endpoint_probe_lock:
                            _endpoint_healthy = True
                        logger.info("edge-tts endpoint recovered (after %ss backoff)", backoff)
                        if pending_text and gen == _generation:
                            player = subprocess.Popen(["afplay", tmp.name],
                                                      stdout=subprocess.DEVNULL,
                                                      stderr=subprocess.DEVNULL)
                            _register_playback(player)
                            try:
                                player.wait(timeout=120)
                            finally:
                                _unregister_playback(player)
                            return  # tmp 由 player 用完，下面不再删
                        return
                except Exception:
                    pass
                finally:
                    if tmp and os.path.exists(tmp.name):
                        try:
                            os.unlink(tmp.name)
                        except Exception:
                            pass
            logger.warning("edge-tts still down after bg recovery attempts")
        except Exception:
            pass

    threading.Thread(target=_recover, daemon=True).start()


def _player_loop() -> None:
    """播放线程：串行处理队列（保序），过期代直接丢弃。"""
    while True:
        text, voice, rate, gen = _tts_queue.get()
        if gen != _generation:
            logger.info("TTS item dropped (interrupted while queued): %s", text[:30])
            continue
        _speak_one(text, voice, rate, gen)


def _speak_one(text: str, voice: str, rate: str, gen: int) -> None:
    """合成并播放一段。策略（快答优先，保真兜底）：

    - 快速 2 次（0/1s）扛瞬时抖动 → 成功即播（常规路径，~2s 内出声）
    - 仍失败且文本以中文为主：**立即降级 say(Tingting) 出声**（实时对话里
      87 秒后才冒一句话比音色突变更怪），**同时后台继续 edge-tts 重试**
      （8s/15s），成功则缓存 mp3 供后续播放复用（这句已用 Tingting 说
      过，不补播；收益是抖动期结束后音色立刻恢复且零延迟）
    - 英文为主文本不降级（say 读英文太怪），只做后台重试
    """
    tmp_mp3 = None
    try:
        tmp_mp3 = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False, dir="/tmp")
        tmp_mp3.close()
        played = False
        # 快速窗口：2 次（0/1s）。长退避移到降级后的后台线程做。
        for attempt, backoff in enumerate((0, 1), start=1):
            if backoff:
                time.sleep(backoff)
            if gen != _generation:
                return  # 合成等待期间被打断
            try:
                result = subprocess.run(
                    ["edge-tts", "--voice", voice, "--rate", rate,
                     "--text", text, "--write-media", tmp_mp3.name],
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
            # 快速窗口失败 → 中文立即 Tingting 出声（不等长退避）；
            # 同时后台继续 edge-tts 长重试（缓存成功产物 + 恢复探测）。
            cjk = _is_mostly_cjk(text)
            if cjk:
                logger.warning("edge-tts fast window failed, fallback to say (Tingting) now")
                _fallback_say(text, voice)
            else:
                logger.warning("edge-tts fast window failed (non-CJK), no say fallback")
            _bg_recover_voice(voice, rate, gen, text if not cjk else "")
            return
        if gen != _generation:
            return  # 播放前被打断，丢弃
        # afplay 播放（Popen 跟踪 → 可被 stop_playback 打断）
        player = subprocess.Popen(["afplay", tmp_mp3.name],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _register_playback(player)
        try:
            player.wait(timeout=120)
        finally:
            _unregister_playback(player)
        if player.returncode == 0:
            logger.info("voice TTS played: %d chars (voice=%s)", len(text), voice)
        else:
            logger.info("voice TTS playback ended early (rc=%s, likely barge-in)",
                        player.returncode)
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


# edge-tts voice → macOS say voice 的降级映射（中文场景统一 Tingting）
_SAY_FALLBACK = {
    "zh-CN-YunxiNeural": "Tingting",
    "zh-CN-YunyangNeural": "Tingting",
    "zh-CN-YunjianNeural": "Tingting",
    "zh-CN-XiaoxiaoNeural": "Tingting",
    "zh-CN-XiaoyiNeural": "Tingting",
}


# ── 播放打断（barge-in）──────────────────────────────────────────────────────
# 跟踪当前播放进程：用户开口/按快捷键时 stop_playback() 立即停掉 zero 的播报，
# 防止麦克风把 zero 自己的声音录进去（ASR 回声）。
_playback_lock = threading.Lock()
_playback_proc: subprocess.Popen | None = None


def stop_playback(grace_seconds: float = 2.0) -> bool:
    """打断全部语音输出：当前播放 + 所有排队未播的。返回是否有东西被打断。

    - 代数 +1：队列中未处理的项、正在合成中的项全部失效丢弃
    - 清空播放队列
    - 停掉当前正在播的进程（先 SIGTERM，``grace_seconds`` 内不退出再 SIGKILL）
    """
    global _generation
    _generation += 1
    dropped = 0
    while True:
        try:
            _tts_queue.get_nowait()
            dropped += 1
        except queue.Empty:
            break

    with _playback_lock:
        proc = _playback_proc
    interrupted = False
    if proc is not None and proc.poll() is None:
        interrupted = True
        try:
            proc.terminate()
            try:
                proc.wait(timeout=grace_seconds)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1)
        except Exception:
            pass
    if dropped or interrupted:
        logger.info("TTS interrupted (barge-in): playing=%s, queued dropped=%d",
                    interrupted, dropped)
        return True
    return False


def _register_playback(proc: subprocess.Popen) -> None:
    with _playback_lock:
        global _playback_proc
        _playback_proc = proc


def _unregister_playback(proc: subprocess.Popen) -> None:
    with _playback_lock:
        global _playback_proc
        if _playback_proc is proc:
            _playback_proc = None


def _fallback_say(text: str, edge_voice: str) -> None:
    """edge-tts 不可用时降级到 macOS say（同样可被 stop_playback 打断）。"""
    say_voice = _SAY_FALLBACK.get(edge_voice, "Tingting")
    try:
        proc = subprocess.Popen(["/usr/bin/say", "-v", say_voice, text],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _register_playback(proc)
        try:
            proc.wait(timeout=120)
        finally:
            _unregister_playback(proc)
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
