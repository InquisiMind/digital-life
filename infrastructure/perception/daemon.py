"""感知采集 daemon —— 线程化，集成进 instance 进程（feature 003）。

设计：作为 instance 进程内的一个常驻线程（参照 feishu_takeover / cron_loop 模式），
而非独立子进程。好处：
  - 生命周期自动跟随 instance（stop_event 一起停）
  - ContextVar 天然正确（同进程，emit/report 走对实例）
  - 与既有 daemon 线程模式一致

前提（macOS）：instance 进程需被授予"辅助功能/输入监控""屏幕录制""麦克风"权限。
首次运行时 pynput 会提示 not trusted；授权后重启 digital-life 生效。
详见 docs/operations/perception-setup.md。

对外只暴露 :func:`start_perception_daemon`（工厂）和 :class:`PerceptionDaemon`（实例）。
"""
from __future__ import annotations

import json
import logging
import threading
import time
import wave
from pathlib import Path
from typing import Any, Callable

from infrastructure.perception.config import PerceptionConfig, load_config, media_dir

logger = logging.getLogger(__name__)

DEFAULT_ENDPOINT = "http://localhost:8642/internal/perception/trigger"


# ── 用户反馈（macOS 通知 + 声音）──────────────────────────────────────────────

_SOUNDS = {
    "start": "/System/Library/Sounds/Glass.aiff",
    "stop": "/System/Library/Sounds/Basso.aiff",
    "done": "/System/Library/Sounds/Hero.aiff",
    "fail": "/System/Library/Sounds/Basso.aiff",
}


def _macos_notify(title: str, message: str) -> None:
    try:
        import subprocess

        script = f"display notification {repr(message)} with title {repr(title)}"
        subprocess.Popen(["osascript", "-e", script],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _play_sound(key: str) -> None:
    path = _SOUNDS.get(key)
    if not path:
        return
    try:
        import subprocess

        subprocess.Popen(["afplay", path],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _feedback(kind: str, *, message: str = "") -> None:
    """统一反馈入口。kind ∈ start/stop/done/fail。任一路失败静默。"""
    titles = {
        "start": ("感知 · 录制中", "开始录屏 + 录音"),
        "stop": ("感知 · 已结束", message or "正在处理..."),
        "done": ("感知 · 已送达", message or "感知信号已注入"),
        "fail": ("感知 · 失败", message or "请查看日志"),
    }
    title, msg = titles.get(kind, (kind, message))
    _macos_notify(title, msg)
    _play_sound(kind)


# ── 录制器 ───────────────────────────────────────────────────────────────────


class _Recorder:
    """一次"按下→再按下"之间的录制会话。

    录屏 = 按一定间隔截图落盘；录音 = sounddevice 持续录音到 wav。
    """

    def __init__(self, *, fps: float, max_seconds: int, out_dir: Path,
                 on_auto_stop: Callable[[], None] | None = None):
        self.fps = fps
        self.max_seconds = max_seconds
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.on_auto_stop = on_auto_stop
        self._recording = False
        self._start_ts = 0.0
        self._screen_thread: threading.Thread | None = None
        self._audio_thread: threading.Thread | None = None
        self._frames: list[Path] = []
        self._audio_path: Path | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

    @property
    def is_recording(self) -> bool:
        return self._recording

    def start(self) -> None:
        with self._lock:
            if self._recording:
                return
            self._recording = True
            self._start_ts = time.time()
            self._frames = []
            self._audio_path = None
            self._stop_event.clear()
            logger.info("recording STARTED")
            self._screen_thread = threading.Thread(target=self._screen_loop, daemon=True)
            self._audio_thread = threading.Thread(target=self._audio_loop, daemon=True)
            self._screen_thread.start()
            self._audio_thread.start()

    def stop(self) -> dict:
        with self._lock:
            if not self._recording:
                return {"frames": [], "audio": None, "duration": 0.0}
            self._recording = False
            self._stop_event.set()
            duration = time.time() - self._start_ts
        if self._screen_thread:
            self._screen_thread.join(timeout=5)
        if self._audio_thread:
            self._audio_thread.join(timeout=5)
        logger.info("recording STOPPED duration=%.1fs frames=%d", duration, len(self._frames))
        return {
            "frames": [str(p) for p in self._frames],
            "audio": str(self._audio_path) if self._audio_path else None,
            "duration": duration,
        }

    def _screen_loop(self) -> None:
        try:
            import mss  # type: ignore
        except ImportError:
            logger.warning("mss 未安装，跳过录屏（仅录音）")
            return
        interval = 1.0 / self.fps if self.fps > 0 else 1.0
        idx = 0
        try:
            with mss.mss() as sct:
                while not self._stop_event.is_set():
                    if time.time() - self._start_ts > self.max_seconds:
                        logger.info("reached max_capture_seconds, auto-stop")
                        if self.on_auto_stop:
                            threading.Thread(target=self.on_auto_stop, daemon=True).start()
                        break
                    import mss.tools  # type: ignore
                    # 截最前台窗口（而非整个屏幕，避免截到壁纸）
                    monitor = _frontmost_window_bounds() or (
                        sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
                    )
                    shot = sct.grab(monitor)
                    p = self.out_dir / f"frame_{idx:04d}.png"
                    try:
                        mss.tools.to_png(shot.rgb, shot.size, output=str(p))
                        self._frames.append(p)
                        idx += 1
                    except Exception as exc:
                        logger.debug("frame %d save failed: %s", idx, exc)
                    self._stop_event.wait(interval)
        except Exception as exc:
            logger.warning("screen loop error: %s", exc)

    def _audio_loop(self) -> None:
        try:
            import sounddevice as sd  # type: ignore
        except ImportError:
            logger.warning("sounddevice 未安装，跳过录音（仅录屏）")
            return
        sample_rate = 16000
        ts = int(time.time())
        audio_path = self.out_dir / f"audio_{ts}.wav"
        max_frames = int(self.max_seconds * sample_rate)
        try:
            self._audio_path = audio_path
            audio_path.parent.mkdir(parents=True, exist_ok=True)
            wf = wave.open(str(audio_path), "wb")
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            block = int(0.2 * sample_rate)
            recorded = 0
            stream = sd.InputStream(samplerate=sample_rate, channels=1, dtype="int16")
            stream.start()
            try:
                while not self._stop_event.is_set() and recorded < max_frames:
                    chunk, _ = stream.read(block)
                    wf.writeframes(chunk.tobytes())
                    recorded += len(chunk)
            finally:
                stream.stop()
                stream.close()
                wf.close()
        except Exception as exc:
            logger.warning("audio loop error: %s", exc)
            self._audio_path = None


# ── 上报 ─────────────────────────────────────────────────────────────────────


def _split_audio_file(audio_path: str, *, segment_seconds: float) -> list[str]:
    import shutil
    import subprocess

    if not shutil.which("ffmpeg"):
        return [audio_path]
    src = Path(audio_path)
    out_dir = src.parent / (src.stem + "_segs")
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = str(out_dir / "seg")
    try:
        subprocess.check_call(
            ["ffmpeg", "-y", "-i", audio_path, "-f", "segment",
             "-segment_time", str(int(segment_seconds)), "-c", "copy",
             f"{prefix}_%03d.wav"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
        )
        segs = sorted(out_dir.glob("seg_*.wav"))
        return [str(s) for s in segs if s.exists()]
    except Exception as exc:
        logger.warning("ffmpeg split failed: %s", exc)
        return [audio_path]


def _report_capture(capture: dict, *, endpoint: str, instance_id: str, source: str) -> dict:
    import httpx

    frames = capture.get("frames") or []
    audio = capture.get("audio")
    audio_segs = _split_audio_file(audio, segment_seconds=30.0) if audio else []
    body = {
        "instance_id": instance_id,
        "source": source,
        "frame_paths": frames,
        "audio_path": audio,
        "audio_segment_paths": audio_segs or None,
        "media_path": audio or (frames[0] if frames else ""),
    }
    try:
        with httpx.Client(timeout=300.0) as client:
            r = client.post(endpoint, json=body)
            r.raise_for_status()
            result = r.json()
        summary_head = (result.get("summary") or "")[:60]
        ok = result.get("perception_ok", result.get("ok", False))
        _feedback("done" if ok else "fail",
                  message=f"已收到（{summary_head}{'...' if summary_head else ''}）"
                          if ok else "视觉/处理未成功，事件已留队列")
        return result
    except Exception as exc:
        logger.error("report_capture failed: %s", exc)
        _feedback("fail", message=f"{type(exc).__name__}: {exc}"[:80])
        return {"ok": False, "error": str(exc)}


# ── 状态文件（最小可见性）────────────────────────────────────────────────────


def _write_state(instance_id: str, **fields: Any) -> None:
    try:
        d = media_dir(instance_id)
        state_path = d / "state.json"
        state = {}
        if state_path.exists():
            state = json.loads(state_path.read_text(encoding="utf-8"))
        state.update(fields)
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ── PerceptionDaemon ─────────────────────────────────────────────────────────

# pynput GlobalHotKeys 的 modifier 映射：扁平格式 → 尖括号格式。
# cmd+shift+z → <cmd>+<shift>+z；ctrl+alt+p → <ctrl>+<alt>+p
_PYNPUT_MODS = {"cmd", "cmd_l", "cmd_r", "ctrl", "ctrl_l", "ctrl_r",
                "shift", "shift_l", "shift_r", "alt", "alt_l", "alt_r",
                "option", "fn", "win", "cmd"}


def _to_pynput_combo(combo: str) -> str:
    """cmd+shift+z → <cmd>+<shift>+z（pynput GlobalHotKeys 要求 modifier 加尖括号）。"""
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    out = []
    for p in parts:
        if p in _PYNPUT_MODS:
            out.append(f"<{p}>")
        else:
            out.append(p)
    return "+".join(out)


class PerceptionDaemon:
    """一个实例的感知 daemon：监听快捷键 → 录制 → 上报。

    生命周期由 :func:`start_perception_daemon` 管理，随 instance 进程启停。
    """

    def __init__(self, instance_id: str, config: PerceptionConfig, *, endpoint: str = DEFAULT_ENDPOINT):
        self.instance_id = instance_id
        self.config = config
        self.endpoint = endpoint
        self._recorder = _Recorder(
            fps=config.frame_fps,
            max_seconds=config.max_capture_seconds,
            out_dir=media_dir(instance_id),
            on_auto_stop=lambda: self._finish_recording(auto=True),
        )
        self._helper_proc: Any = None  # Swift hotkey helper subprocess
        self._helper_reader: threading.Thread | None = None
        self._started_at = time.time()

    def _helper_path(self) -> Path:
        """Swift hotkey helper 的路径（scripts/hotkey_helper）。"""
        return Path(__file__).resolve().parents[2] / "scripts" / "hotkey_helper"

    def start(self) -> None:
        """启动快捷键监听（用 Carbon Swift helper，不依赖辅助功能权限）。

        helper 是一个编译好的 Swift 二进制，用 RegisterEventHotKey 注册全局热键。
        它输出 READY（注册成功）/ TRIGGERED（热键按下）到 stdout。
        daemon spawn 它并读 stdout。
        """
        combo = self.config.hotkey
        helper = self._helper_path()
        if not helper.exists():
            logger.warning("hotkey helper 不存在: %s（需要 swiftc 编译）", helper)
            return

        # 解析 hotkey: "cmd+shift+z" → key="z" mods="cmd+shift"
        parts = combo.split("+")
        key_part = parts[-1].strip()
        mod_part = "+".join(p.strip() for p in parts[:-1]) or "cmd"
        try:
            import subprocess

            self._helper_proc = subprocess.Popen(
                [str(helper), key_part, mod_part],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1,
            )
        except Exception as exc:
            logger.warning("hotkey helper 启动失败: %s", exc)
            return

        # 读 stdout 的线程：等 READY / TRIGGERED
        def _read_loop():
            assert self._helper_proc is not None
            for line in self._helper_proc.stdout:
                line = line.strip()
                if line.startswith("READY"):
                    logger.info("perception daemon ready: instance=%s hotkey=%s (Carbon)",
                                self.instance_id[:8], combo)
                elif line == "TRIGGERED":
                    logger.info("hotkey TRIGGERED: instance=%s", self.instance_id[:8])
                    self._toggle()

        self._helper_reader = threading.Thread(target=_read_loop, daemon=True)
        self._helper_reader.start()
        _write_state(self.instance_id, running=True, pid=_pid(),
                     started_at=self._started_at, hotkey=combo)
        logger.info("perception daemon started: instance=%s hotkey=%s (Carbon helper)",
                    self.instance_id[:8], combo)

    def stop(self) -> None:
        if self._helper_proc:
            try:
                self._helper_proc.terminate()
                self._helper_proc.wait(timeout=3)
            except Exception:
                try:
                    self._helper_proc.kill()
                except Exception:
                    pass
        # 录制中则强制结束
        if self._recorder.is_recording:
            self._finish_recording(auto=False)
        _write_state(self.instance_id, running=False, stopped_at=time.time())
        logger.info("perception daemon stopped: instance=%s", self.instance_id[:8])

    def _toggle(self) -> None:
        # 防抖：Carbon RegisterEventHotKey 在按住/松开 modifier 时可能连续触发，
        # 500ms 内的重复触发忽略（人类不可能 500ms 内按两次）。
        now = time.time()
        if hasattr(self, "_last_toggle_ts") and now - getattr(self, "_last_toggle_ts", 0) < 0.5:
            logger.debug("toggle debounced (interval=%.2fs)", now - getattr(self, "_last_toggle_ts", 0))
            return
        self._last_toggle_ts = now

        if self._recorder.is_recording:
            self._finish_recording(auto=False)
        else:
            self._recorder.start()
            _feedback("start")
            _write_state(self.instance_id, last_capture_at=time.time())

    def _finish_recording(self, *, auto: bool) -> None:
        if not self._recorder.is_recording:
            return
        cap = self._recorder.stop()
        _feedback("stop", message="已达最大时长，自动结束" if auto else "")
        has_video = bool(cap.get("frames"))
        has_audio = bool(cap.get("audio"))
        # 静音检测：录音文件存在但内容全 0（macOS 无麦克风权限时常见）→ 不送 ASR
        if has_audio and _is_silent(cap["audio"]):
            logger.warning(
                "录制的音频是静音（全0），可能缺少麦克风权限——跳过 ASR，仅走视觉。"
                "详见 docs/operations/perception-setup.md"
            )
            cap["audio"] = None
            has_audio = False
        src = "hotkey_both" if (has_video and has_audio) else ("hotkey_screen" if has_video else "hotkey_audio")
        threading.Thread(
            target=_report_capture,
            kwargs=dict(capture=cap, endpoint=self.endpoint,
                        instance_id=self.instance_id, source=src),
            daemon=True,
        ).start()


def _pid() -> int:
    import os

    return os.getpid()


def _is_silent(wav_path: str, *, threshold: int = 100) -> bool:
    """检测 wav 文件是否为静音（最大振幅 < threshold）。

    macOS 无麦克风权限时，sounddevice 返回全 0 样本——文件存在但内容是静音。
    这种情况跳过 ASR（省一次 API 调用 + 避免无意义转写）。
    """
    try:
        import wave

        with wave.open(wav_path, "rb") as wf:
            frames = wf.readframes(wf.getnframes())
        if not frames:
            return True
        # 快速检测：只看前 10000 字节的最大值（足够判断静音）
        import numpy as np

        data = np.frombuffer(frames[:20000], dtype=np.int16)
        return int(np.abs(data).max()) < threshold if len(data) else True
    except Exception:
        return False  # 检测失败不阻断（保守地认为非静音）


def _frontmost_window_bounds() -> dict | None:
    """获取最前台窗口的区域（left/top/width/height），供 mss.grab 用。

    用 Quartz CGWindowList 找当前最前台 app 的窗口区域，
    避免截到整个桌面（含壁纸）。失败返回 None（调用方 fallback 到全屏）。
    """
    try:
        import Quartz

        # 找最前台的 app
        ws = Quartz.NSWorkspace.sharedWorkspace()
        front_app = ws.frontmostApplication()
        if not front_app:
            return None
        pid = front_app.processIdentifier()

        # 查这个 PID 的窗口
        window_list = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        )
        for w in window_list:
            if w.get("kCGWindowOwnerPID") == pid and w.get("kCGWindowBounds"):
                b = w["kCGWindowBounds"]
                bounds = {
                    "left": int(b["X"]),
                    "top": int(b["Y"]),
                    "width": int(b["Width"]),
                    "height": int(b["Height"]),
                }
                if bounds["width"] > 100 and bounds["height"] > 100:
                    return bounds
    except Exception as exc:
        logger.debug("frontmost window bounds failed: %s", exc)
    return None


# ── 工厂（instance 启动时调用）────────────────────────────────────────────────


def start_perception_daemon(instance_id: str, *, endpoint: str = DEFAULT_ENDPOINT) -> PerceptionDaemon | None:
    """启动一个实例的感知 daemon。返回 daemon 对象（可 stop）；不启动返回 None。

    照 feishu_takeover.start_takeover_daemon 范式：
      - perception.enabled 为 false → 返回 None（静默跳过）
      - 缺 pynput 等依赖 → daemon.start() 内部 log warning，但仍返回对象
        （listener 起不来但对象存在，stop 安全）
    """
    cfg = load_config(instance_id)
    if not cfg.enabled:
        logger.debug("perception disabled for %s, skipping", instance_id[:8])
        return None
    daemon = PerceptionDaemon(instance_id, cfg, endpoint=endpoint)
    daemon.start()
    return daemon
