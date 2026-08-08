#!/usr/bin/env python3
"""感知录制脚本——被 perception_trigger.py 后台启动。

录制截图+音频，收到 SIGTERM 后停止并处理：
  1. 停止截图+录音
  2. ASR 转写音频
  3. GLM-4.6V 视觉理解
  4. POST 到 perception endpoint
"""
from __future__ import annotations

import signal
import sys
import time
import threading
import wave
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import httpx
from infrastructure.perception.config import load_config, media_dir
from infrastructure.perception.frames import encode_frame_images
from infrastructure.perception.asr import transcribe_file
from infrastructure.perception.vision import call_vision
from infrastructure.perception.context import build_slim_context, wake_meta_snapshot

# 状态
recording = True
frames: list[str] = []
audio_path: str | None = None
instance_id = ""
cfg = None
out_dir = None


def handle_sigterm(signum, frame):
    global recording
    recording = False


signal.signal(signal.SIGTERM, handle_sigterm)


def record_screen():
    """截图循环。"""
    global frames
    import mss
    import mss.tools
    import Quartz

    interval = 1.0 / cfg.frame_fps
    idx = 0

    while recording:
        # 找前台窗口
        bounds = None
        try:
            ws = Quartz.NSWorkspace.sharedWorkspace()
            front = ws.frontmostApplication()
            if front:
                pid = front.processIdentifier()
                wl = Quartz.CGWindowListCopyWindowInfo(
                    Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
                    Quartz.kCGNullWindowID)
                for w in wl:
                    if w.get("kCGWindowOwnerPID") == pid and w.get("kCGWindowBounds"):
                        b = w["kCGWindowBounds"]
                        if int(b["Width"]) > 200 and int(b["Height"]) > 200:
                            bounds = {"left": int(b["X"]), "top": int(b["Y"]),
                                      "width": int(b["Width"]), "height": int(b["Height"])}
                            break
        except Exception:
            pass

        try:
            with mss.mss() as sct:
                monitor = bounds or (sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0])
                shot = sct.grab(monitor)
                p = out_dir / f"capture_{int(time.time())}_{idx:03d}.png"
                mss.tools.to_png(shot.rgb, shot.size, output=str(p))
                frames.append(str(p))
                idx += 1
        except Exception as exc:
            print(f"screenshot error: {exc}", file=sys.stderr)

        time.sleep(interval)


def record_audio():
    """录音——用 sounddevice。"""
    global audio_path
    sr = 16000
    ts = int(time.time())
    path = out_dir / f"capture_{ts}.wav"

    try:
        import sounddevice as sd
        import numpy as np

        data = sd.rec(int(cfg.max_capture_seconds * sr), samplerate=sr, channels=1, dtype="int16")
        # 等待停止信号
        while recording:
            time.sleep(0.1)

        sd.stop()
        # 截断到实际录制长度
        actual = int((time.time() - start_time) * sr)
        data = data[:actual]

        # 静音检测
        mx = int(np.abs(data).max())
        if mx < 100:
            print("audio is silent, skipping", file=sys.stderr)
            return

        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(data.tobytes())
        audio_path = str(path)
        print(f"audio recorded: {len(data)/sr:.1f}s maxval={mx}", file=sys.stderr)
    except Exception as exc:
        print(f"audio error: {exc}", file=sys.stderr)


def process_and_report():
    """停止后处理：ASR + 视觉理解 + 上报。"""
    import subprocess

    # ASR
    transcript = ""
    if audio_path:
        from infrastructure.perception.asr import split_audio_segments, probe_audio_duration
        duration = probe_audio_duration(audio_path)
        segs = split_audio_segments(duration, segment_seconds=30.0)
        # 用 ffmpeg 切分
        audio_segs = []
        if segs:
            import shutil
            if shutil.which("ffmpeg"):
                src = Path(audio_path)
                seg_dir = src.parent / (src.stem + "_segs")
                seg_dir.mkdir(parents=True, exist_ok=True)
                import subprocess
                subprocess.check_call(
                    ["ffmpeg", "-y", "-i", audio_path, "-f", "segment",
                     "-segment_time", "30", "-c", "copy", str(seg_dir / "seg_%03d.wav")],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
                audio_segs = [str(s) for s in sorted(seg_dir.glob("seg_*.wav"))]
        out = transcribe_file(audio_path, config=cfg, segment_paths=audio_segs or None)
        transcript = out.get("text", "")

    # 视觉
    image_uris = encode_frame_images(frames[:1], max_width=cfg.frame_max_width)  # 只发1帧
    history = build_slim_context(instance_id, recent_turns=cfg.context_recent_turns)

    vis = call_vision(
        image_data_uris=image_uris,
        transcript=transcript,
        history_messages=history,
        config=cfg,
        instance_id=instance_id,
    )

    summary = ""
    if vis.get("ok"):
        parsed = vis.get("parsed") or {}
        summary = parsed.get("summary", "") or vis.get("raw", "")
    else:
        summary = f"感知处理失败：{vis.get('error', '')}"

    # 上报
    endpoint = "http://localhost:8642/internal/perception/trigger"
    body = {
        "instance_id": instance_id,
        "source": "hotkey_both" if (frames and transcript) else ("hotkey_screen" if frames else "hotkey_audio"),
        "result": {
            "summary": summary,
            "transcript": transcript,
            "ok": vis.get("ok", False),
        },
        "reply_channel": "voice",
        "media_path": audio_path or (frames[0] if frames else ""),
    }
    try:
        with httpx.Client(timeout=120) as client:
            r = client.post(endpoint, json=body)
            print(f"reported: {r.status_code} {r.json()}", file=sys.stderr)
    except Exception as exc:
        print(f"report failed: {exc}", file=sys.stderr)


def main():
    global instance_id, cfg, out_dir, start_time

    # 解析参数
    for i, arg in enumerate(sys.argv):
        if arg == "--instance" and i + 1 < len(sys.argv):
            instance_id = sys.argv[i + 1]

    if not instance_id:
        instance_id = "zero"

    cfg = load_config(instance_id)
    out_dir = media_dir(instance_id)
    start_time = time.time()

    print(f"perception_capture started: instance={instance_id}", file=sys.stderr)

    # 启动截图+录音线程
    t_screen = threading.Thread(target=record_screen, daemon=True)
    t_audio = threading.Thread(target=record_audio, daemon=True)
    t_screen.start()
    t_audio.start()

    # 等待停止信号
    while recording:
        time.sleep(0.5)

    print("stopping...", file=sys.stderr)

    # 等线程结束
    t_screen.join(timeout=3)
    t_audio.join(timeout=5)

    # 处理+上报
    process_and_report()

    print("done", file=sys.stderr)


if __name__ == "__main__":
    main()
