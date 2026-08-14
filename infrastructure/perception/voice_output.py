"""语音输出通道（TTS）—— 本地播放，不依赖飞书。

引擎策略（2026-08-14 定）：默认 macOS Siri 神经语音（say，本地合成，
零网络零抖动，~1s 出声）；edge-tts（微软云端，音质上限更高但端点
间歇抖动严重——半小时内每条都重试）作为可选高音质配置
（app.yaml perception.tts_engine: edge / tts_voice: zh-CN-YunxiNeural）。

voice 参数兼容两种形式：
  - "zh-CN-YunxiNeural" 等 edge-tts 名 → edge 引擎
  - "Reed"/"Rocko"/"Eddy" 等 macOS 声音名 → say 引擎
未配置时默认 say + Reed（Siri 神经男声，本地）。
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

# 默认引擎：macOS Siri 神经语音（本地、稳定、快）；可选 "edge"（云端高音质）
DEFAULT_ENGINE = os.getenv("DIGITAL_LIFE_TTS_ENGINE", "say")
# say 默认声：Reed = Siri 神经中文男声；edge 默认声：云希
DEFAULT_SAY_VOICE = "Reed"
DEFAULT_EDGE_VOICE = "zh-CN-YunxiNeural"
# 是否启用 TTS（环境变量 / 实例配置控制）
DEFAULT_ENABLED = os.getenv("DIGITAL_LIFE_TTS", "0") == "1"


def _looks_like_edge_voice(voice: str) -> bool:
    """edge-tts 声音名形如 zh-CN-YunxiNeural（含 '-' 且全 ASCII）。"""
    return bool(voice) and "-" in voice and voice.isascii()


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


def speak(text: str, *, voice: str = "", rate: str = "+0%") -> bool:
    """语音播报一段文本（排队，异步）。返回是否成功入队。

    多次调用自动排队串行播放（zero 可以分多次表达，不会叠音）；
    stop_playback() 打断当前播放并丢弃全部未播的排队项。

    Args:
        text: 要播放的文本（会自动清理标记）
        voice: 声音名。macOS 声（"Reed"/"Rocko"/"Eddy"…）→ say 本地合成；
            edge-tts 名（"zh-CN-YunxiNeural"…）→ 云端合成（高音质，有抖动）。
            空串 = 引擎默认声（say→Reed / edge→云希）。
        rate: 语速调整（"+0%" 正常 / "+10%" 稍快；仅 edge 引擎生效）
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
    """合成并播放一段。按 voice 形式分流引擎：

    - macOS 声（"Reed" 等 / 默认）：say 本地合成——零网络零抖动 ~1s 出声，
      无重试无降级（本地合成不会"端点抖动"，失败即系统级问题）
    - edge-tts 声（"zh-CN-XxxNeural"）：云端合成，快速 2 次（0/1s）扛瞬时
      抖动；仍失败且中文为主 → 立即降级 say 出声 + 后台长退避探测恢复；
      英文为主不降级（say 读英文怪），后台重试成功补播
    """
    # ── say 本地快路径（默认引擎）──
    if not _looks_like_edge_voice(voice or DEFAULT_SAY_VOICE):
        say_voice = voice or DEFAULT_SAY_VOICE
        try:
            proc = subprocess.Popen(["/usr/bin/say", "-v", say_voice, text],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _register_playback(proc)
            try:
                proc.wait(timeout=300)
            finally:
                _unregister_playback(proc)
            if proc.returncode == 0:
                logger.info("voice say played: %d chars (voice=%s)", len(text), say_voice)
            else:
                logger.info("voice say ended early (rc=%s, likely barge-in)", proc.returncode)
        except Exception as exc:
            logger.warning("voice say failed: %s", exc)
        return

    # ── edge-tts 云路径 ──
    voice = voice or DEFAULT_EDGE_VOICE
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


# edge-tts voice → macOS say voice 的降级映射（Siri 神经声，男声保持男声）
_SAY_FALLBACK = {
    "zh-CN-YunxiNeural": "Reed",
    "zh-CN-YunyangNeural": "Rocko",
    "zh-CN-YunjianNeural": "Rocko",
    "zh-CN-XiaoxiaoNeural": "Flo",
    "zh-CN-XiaoyiNeural": "Flo",
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
    """读取配置的 TTS 语音（决定引擎：macOS 声名 → say，edge-tts 名 → 云端）。

    未配置时按引擎默认：say → Reed（Siri 神经男声，本地稳定）；
    显式配置 zh-CN-* 名则用 edge-tts（高音质，容忍抖动）。
    """
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
    if DEFAULT_ENGINE == "edge":
        return DEFAULT_EDGE_VOICE
    return DEFAULT_SAY_VOICE
