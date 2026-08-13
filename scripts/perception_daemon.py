#!/usr/bin/env python3
"""感知采集 daemon（spec US1 / FR-001~FR-003）。

独立子进程，在人类登录电脑后常驻。监听全局快捷键：
  - 按一次 → 开始录屏（定时截图）+ 录音
  - 再按一次 → 结束，抽帧、分段音频，POST 给 master 的 perception endpoint

为什么是独立进程（spec FR-001）：
  digital-life 主进程是后台 master，没有登录用户的 GUI 会话；
  屏幕录制/麦克风/全局快捷键都需要绑定到登录会话（macOS 权限 per-app per-session）。
  daemon 崩溃不影响主意识；主意识重启不影响 daemon。

依赖（按需，缺失时降级）：
  - pynput（全局快捷键）— 缺失则退化成 CLI 模式（回车切换）
  - mss（截图）— 缺失则只录音
  - sounddevice + wave（录音）— 缺失则只录屏
  - ffmpeg/ffprobe（音频分段时长探测）— 可选

用法：
  python scripts/perception_daemon.py [--endpoint http://localhost:8642/...]
                                      [--instance INSTANCE_ID]
                                      [--hotkey cmd+shift+p]
                                      [--mode hotkey|cli]

环境变量（覆盖默认）：
  PERCEPTION_ENDPOINT / PERCEPTION_INSTANCE / PERCEPTION_HOTKEY / PERCEPTION_MODE
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

# 允许 import 项目模块（daemon 独立运行）
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from infrastructure.perception.config import DEFAULT_MAX_CAPTURE_SECONDS, DEFAULT_FRAME_FPS, DEFAULT_HOTKEY, load_config
from infrastructure.perception.frames import select_frame_timestamps

logger = logging.getLogger("perception_daemon")

DEFAULT_ENDPOINT = "http://localhost:8642/internal/perception/trigger"


# ── 录制器 ───────────────────────────────────────────────────────────────────


class Recorder:
    """一次"按下→再按下"之间的录制会话（spec FR-002）。

    录屏 = 按一定间隔截图落盘；录音 = sounddevice 持续录音到 wav。
    结束后产出：图片帧路径列表 + 音频文件路径。
    """

    def __init__(self, *, fps: float, max_seconds: int, out_dir: Path):
        self.fps = fps
        self.max_seconds = max_seconds
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)

        self._recording = False
        self._start_ts = 0.0
        self._screen_thread: threading.Thread | None = None
        self._audio_thread: threading.Thread | None = None
        self._frames: list[Path] = []
        self._audio_path: Path | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        # 超时自动停止时的回调（run_daemon 注册，用于发反馈 + 上报）。
        # 设为 None 时超时只静默停录制循环。
        self.on_auto_stop: typing.Callable[[], None] | None = None

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
        # 等线程退出
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
        """按 fps 定时截图，直到 stop 或超时。"""
        try:
            import mss  # type: ignore
            import mss.tools  # type: ignore
        except ImportError:
            logger.warning("mss 未安装，跳过录屏（仅录音）")
            return
        interval = 1.0 / self.fps if self.fps > 0 else 1.0
        idx = 0
        try:
            with mss.mss() as sct:
                monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
                while not self._stop_event.is_set():
                    if time.time() - self._start_ts > self.max_seconds:
                        logger.info("reached max_capture_seconds, auto-stop")
                        # 触发自动停止回调（发反馈 + 上报），由 run_daemon 注册。
                        # 用守护线程调用，避免 screen_loop 自己阻塞自己。
                        if self.on_auto_stop:
                            threading.Thread(target=self.on_auto_stop, daemon=True).start()
                        break
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
        """持续录音到 wav，直到 stop。"""
        try:
            import sounddevice as sd  # type: ignore
        except ImportError:
            logger.warning("sounddevice 未安装，跳过录音（仅录屏）")
            return
        sample_rate = 16000
        ts = int(time.time())
        audio_path = self.out_dir / f"audio_{ts}.wav"
        try:
            # 估算最大块数（按 max_seconds）
            max_frames = int(self.max_seconds * sample_rate)
            self._audio_path = audio_path
            audio_path.parent.mkdir(parents=True, exist_ok=True)
            wf = wave.open(str(audio_path), "wb")
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)

            # 分块录，便于响应 stop
            block = int(0.2 * sample_rate)  # 0.2s 一块
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


# ── 用户反馈：macOS 通知 + 系统声音 + CLI 输出 ────────────────────────────────
# 录制是"后台默默进行"的，人类按完快捷键看不到状态。这里给三路反馈：
#   1. macOS 通知中心弹窗（osascript，系统自带零依赖）
#   2. 系统声音（afplay，开始/结束用不同声，闭眼也知道状态）
#   3. CLI 模式额外 print（用户盯着终端时）
# 任一路失败都静默——反馈不能阻塞或打断采集主流程。

_SOUNDS = {
    "start": "/System/Library/Sounds/Glass.aiff",    # 清脆"叮"= 开始
    "stop": "/System/Library/Sounds/Basso.aiff",     # 低沉 = 结束
    "done": "/System/Library/Sounds/Hero.aiff",      # 完成 = 上报成功
    "fail": "/System/Library/Sounds/Basso.aiff",     # 失败
}


def _macos_notify(title: str, message: str, subtitle: str = "") -> None:
    """弹 macOS 通知中心通知。非 macOS / osascript 失败 → 静默。"""
    try:
        script = f'display notification {repr(message)} with title {repr(title)}'
        if subtitle:
            script += f' subtitle {repr(subtitle)}'
        subprocess.Popen(
            ["osascript", "-e", script],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def _play_sound(key: str) -> None:
    """播放系统声音（非阻塞）。失败静默。"""
    path = _SOUNDS.get(key)
    if not path:
        return
    try:
        subprocess.Popen(
            ["afplay", path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def feedback(kind: str, *, message: str = "", cli: bool = False) -> None:
    """统一的用户反馈入口。

    kind:
      - "start": 开始录制（🔴）
      - "stop":  结束录制（⏹）
      - "done":  上报成功（✅）
      - "fail":  上报失败（❌）
    """
    titles = {
        "start": ("🔴 录制中", "开始录屏 + 录音"),
        "stop": ("⏹ 已结束", "正在处理..."),
        "done": ("✅ 已送达", message or "感知信号已注入"),
        "fail": ("❌ 上报失败", message or "请查看日志"),
    }
    title, msg = titles.get(kind, (kind, message))
    _macos_notify(title, msg)
    _play_sound(kind)
    if cli:
        print(f"  {title} — {msg}")


# ── 触发与上报 ───────────────────────────────────────────────────────────────


def _split_audio_file(audio_path: str, *, segment_seconds: float) -> list[str]:
    """用 ffmpeg 把音频切成 ≤ segment_seconds 的分段，返回分段路径列表。

    ffmpeg 不可用时返回 [audio_path]（整文件，由 ASR 端兜底）。
    """
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


def report_capture(
    capture: dict,
    *,
    endpoint: str,
    instance_id: str,
    source: str,
    cli: bool = False,
) -> dict:
    """把一次录制结果 POST 给 master perception endpoint。"""
    import httpx

    frames = capture.get("frames") or []
    audio = capture.get("audio")
    audio_segs = []
    if audio:
        audio_segs = _split_audio_file(audio, segment_seconds=30.0)

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
        # 上报成功反馈
        summary_head = (result.get("summary") or "")[:60]
        ok = result.get("perception_ok", result.get("ok", False))
        feedback("done" if ok else "fail",
                 message=f"zero 已收到（{summary_head}{'...' if summary_head else ''})"
                         if ok else "视觉/处理未成功，事件已留队列",
                 cli=cli)
        return result
    except Exception as exc:
        logger.error("report_capture failed: %s", exc)
        feedback("fail", message=f"{type(exc).__name__}: {exc}"[:80], cli=cli)
        return {"ok": False, "error": str(exc)}


# ── 主循环 ───────────────────────────────────────────────────────────────────


def run_daemon(
    *,
    endpoint: str,
    instance_id: str,
    hotkey: str,
    mode: str,
    max_capture_seconds: int,
    fps: float,
) -> None:
    cfg = load_config(instance_id or None)
    out_dir = _REPO_ROOT / "var" / "perception_capture"
    recorder = Recorder(fps=fps, max_seconds=max_capture_seconds, out_dir=out_dir)
    is_cli = mode == "cli"

    def finish_recording(auto: bool = False) -> None:
        """结束录制 + 反馈 + 异步上报。toggle 和超时自动停止都走这里。"""
        if not recorder.is_recording:
            return
        cap = recorder.stop()
        feedback("stop", message="已达最大时长，自动结束" if auto else "", cli=is_cli)
        has_video = bool(cap.get("frames"))
        has_audio = bool(cap.get("audio"))
        if has_video and has_audio:
            src = "hotkey_both"
        elif has_video:
            src = "hotkey_screen"
        else:
            src = "hotkey_audio"
        threading.Thread(
            target=report_capture,
            kwargs=dict(capture=cap, endpoint=endpoint, instance_id=instance_id,
                        source=src, cli=is_cli),
            daemon=True,
        ).start()

    # 超时自动停止时回调（_screen_loop 检测到 max_capture_seconds 触发）
    recorder.on_auto_stop = lambda: finish_recording(auto=True)

    def toggle() -> None:
        if recorder.is_recording:
            finish_recording(auto=False)
        else:
            recorder.start()
            feedback("start", cli=is_cli)

    if mode == "cli":
        _run_cli_loop(toggle)
    else:
        _run_hotkey_loop(hotkey, toggle)


def _run_cli_loop(toggle) -> None:
    print("=== 感知 daemon（CLI 模式）===")
    print("按回车切换 开始/结束 录制；输入 q 退出。")
    while True:
        try:
            cmd = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break
        if cmd == "q":
            break
        toggle()


def _run_hotkey_loop(hotkey: str, toggle) -> None:
    try:
        from pynput import keyboard  # type: ignore
    except ImportError:
        logger.error("pynput 未安装，回退到 CLI 模式。pip install pynput 后可用全局快捷键。")
        _run_cli_loop(toggle)
        return

    # 解析 "cmd+shift+p" → [Key.cmd, Key.shift, 'p']
    parts = [p.strip().lower() for p in hotkey.split("+") if p.strip()]
    mods_map = {
        "cmd": keyboard.Key.cmd,
        "cmd_l": keyboard.Key.cmd_l,
        "cmd_r": keyboard.Key.cmd_r,
        "ctrl": keyboard.Key.ctrl,
        "shift": keyboard.Key.shift,
        "alt": keyboard.Key.alt,
        "option": keyboard.Key.alt,
        "fn": keyboard.Key.fn,
    }
    expected_mods = set()
    expected_key: str | None = None
    for p in parts:
        if p in mods_map:
            expected_mods.add(mods_map[p])
        else:
            expected_key = p

    pressed_mods: set = set()

    def on_press(key):
        if key in expected_mods or key in set(mods_map.values()):
            pressed_mods.add(key)
        else:
            k = getattr(key, "char", None) or getattr(key, "name", None)
            if (
                expected_key
                and k
                and k.lower() == expected_key
                and expected_mods
                and expected_mods.issubset(pressed_mods | _matching_mods(pressed_mods, mods_map))
            ):
                toggle()

    def on_release(key):
        pressed_mods.discard(key)

    print(f"=== 感知 daemon（快捷键模式：{hotkey}）===")
    print("按一次开始录屏录音，再按一次结束并上报。Ctrl+C 退出。")
    with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
        listener.join()


def _matching_mods(pressed: set, mods_map: dict) -> set:
    """把 pressed 里的变体键（cmd_l/cmd_r）归一化到 expected 集合。"""
    out = set()
    for k in pressed:
        out.add(k)
        if k in (mods_map["cmd_l"], mods_map["cmd_r"], mods_map["cmd"]):
            out.add(mods_map["cmd"])
        if k in (mods_map["shift"],):
            out.add(mods_map["shift"])
        if k in (mods_map["ctrl"],):
            out.add(mods_map["ctrl"])
        if k in (mods_map["alt"], mods_map["option"]):
            out.add(mods_map["alt"])
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="感知采集 daemon")
    parser.add_argument("--endpoint", default=os.getenv("PERCEPTION_ENDPOINT", DEFAULT_ENDPOINT))
    parser.add_argument("--instance", default=os.getenv("PERCEPTION_INSTANCE", ""),
                        help="目标实例（空=默认实例）")
    parser.add_argument("--hotkey", default=os.getenv("PERCEPTION_HOTKEY", DEFAULT_HOTKEY))
    parser.add_argument("--mode", default=os.getenv("PERCEPTION_MODE", "hotkey"),
                        choices=["hotkey", "cli"])
    parser.add_argument("--max-seconds", type=int, default=DEFAULT_MAX_CAPTURE_SECONDS)
    parser.add_argument("--fps", type=float, default=DEFAULT_FRAME_FPS)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    run_daemon(
        endpoint=args.endpoint,
        instance_id=args.instance,
        hotkey=args.hotkey,
        mode=args.mode,
        max_capture_seconds=args.max_seconds,
        fps=args.fps,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
