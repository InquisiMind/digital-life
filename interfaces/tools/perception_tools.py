"""感知观察工具（spec US2 / FR-014/FR-015）。

主意识可主动调用的观察工具，内部复用 :mod:`infrastructure.perception` 流水线。
与快捷键触发的区别：这些是**模型主动发起、同步执行、结果作为工具返回值**
回到思考流——不产生新事件（spec FR-015）。

三个工具：
  - sense_screen  — 截一张当前屏幕图 → 视觉模型描述
  - sense_audio   — 录一段麦克风音频 → ASR 转写
  - sense_media   — 回看已落盘的原始媒体（快捷键触发时 media_path 指向的文件）

外部依赖（按需 import，缺失时返回友好错误）：
  - ``mss``：屏幕截图（sense_screen）
  - ``sounddevice`` + ``soundfile`` / ``wave``：录音（sense_audio）
"""
from __future__ import annotations

import json
import logging
import time
import wave
from pathlib import Path
from typing import Any, Dict

from infrastructure.config import get_app_instance_id
from interfaces.tools import registry
from infrastructure.perception import run_pipeline
from infrastructure.perception.config import load_config, media_dir
from infrastructure.perception.frames import encode_image_file
from infrastructure.perception.vision import call_vision

logger = logging.getLogger(__name__)


# ── 采集原语（可选依赖，缺失降级）─────────────────────────────────────────────


def _capture_screen_once(dest_path: Path) -> bool:
    """截一张屏幕图到 dest_path，成功返回 True。需要 mss。"""
    try:
        import mss  # type: ignore
        import mss.tools  # type: ignore
    except ImportError:
        return False
    try:
        with mss.mss() as sct:
            monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            shot = sct.grab(monitor)
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            mss.tools.to_png(shot.rgb, shot.size, output=str(dest_path))
        return True
    except Exception as exc:
        logger.warning("capture screen failed: %s", exc)
        return False


def _record_audio(dest_path: Path, *, seconds: float, sample_rate: int = 16000) -> bool:
    """录一段音频到 dest_path（wav），成功返回 True。需要 sounddevice。"""
    try:
        import sounddevice as sd  # type: ignore
    except ImportError:
        return False
    try:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        frames = sd.rec(int(seconds * sample_rate), samplerate=sample_rate, channels=1, dtype="int16")
        sd.wait()
        with wave.open(str(dest_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(frames.tobytes())
        return True
    except Exception as exc:
        logger.warning("record audio failed: %s", exc)
        return False


# ── sense_screen ─────────────────────────────────────────────────────────────


def _handle_sense_screen(args: Dict[str, Any], **kwargs) -> str:
    """sense_screen —— 截当前屏幕一张图，让视觉模型描述。"""
    iid = get_app_instance_id() or ""
    if not iid:
        return registry.tool_error("无法确定当前实例 ID（ContextVar 未设）")

    cfg = load_config(iid)
    question = (args.get("question") or "").strip() or "简要描述当前屏幕上有什么值得注意的内容。"

    # 截图落盘到 media_dir
    ts = int(time.time())
    shot_path = media_dir(iid) / f"screen_{ts}.png"
    if not _capture_screen_once(shot_path):
        return registry.tool_error(
            "屏幕截图失败：缺少 mss 依赖，或未授予屏幕录制权限。"
            "请 `pip install mss` 并在系统设置中授权。"
        )

    # 编码 + 调视觉模型（带精简上下文）
    try:
        from infrastructure.perception.context import build_slim_context

        data_uri = encode_image_file(shot_path, max_width=cfg.frame_max_width)
        history = build_slim_context(iid, recent_turns=cfg.context_recent_turns)
        vis = call_vision(
            image_data_uris=[data_uri],
            transcript="",
            history_messages=history,
            config=cfg,
            instance_id=iid,
            question_prompt=question + "\n直接用中文描述，不要输出 JSON。",
        )
    except Exception as exc:
        return registry.tool_error(f"视觉调用失败: {exc}")

    if not vis.get("ok"):
        return registry.tool_error(f"视觉模型调用失败: {vis.get('error')}")
    return vis.get("raw") or "(视觉模型返回空)"


# ── sense_audio ──────────────────────────────────────────────────────────────


def _handle_sense_audio(args: Dict[str, Any], **kwargs) -> str:
    """sense_audio —— 录一段麦克风音频，ASR 转写。"""
    iid = get_app_instance_id() or ""
    if not iid:
        return registry.tool_error("无法确定当前实例 ID（ContextVar 未设）")

    cfg = load_config(iid)
    seconds = float(args.get("seconds") or 5)
    seconds = max(1.0, min(seconds, float(cfg.max_capture_seconds)))

    ts = int(time.time())
    audio_path = media_dir(iid) / f"audio_{ts}.wav"
    if not _record_audio(audio_path, seconds=seconds):
        return registry.tool_error(
            "录音失败：缺少 sounddevice 依赖，或未授予麦克风权限。"
            "请 `pip install sounddevice` 并在系统设置中授权。"
        )

    # ASR（单段，seconds ≤ 30 通常够；超长由 transcribe_file 分段）
    from infrastructure.perception.asr import transcribe_file, split_audio_segments

    # 若超过单次上限，切成分段（这里整文件即一段 wav，transcribe_file 内部按 segment_paths 处理）
    duration = min(seconds, float(cfg.max_capture_seconds))
    segs = split_audio_segments(duration)
    # 简化：单文件直接传，ASR 内部若超时由服务端报错（30s 内安全）
    out = transcribe_file(audio_path, config=cfg, segment_paths=None)
    if not out.get("ok"):
        return registry.tool_error(f"ASR 转写失败: {out.get('error')}")
    text = out.get("text", "")
    return json.dumps({
        "ok": True,
        "transcript": text,
        "duration_seconds": seconds,
        "audio_path": str(audio_path),
        "segments_planned": len(segs),
    }, ensure_ascii=False)


# ── sense_media ──────────────────────────────────────────────────────────────


def _handle_sense_media(args: Dict[str, Any], **kwargs) -> str:
    """sense_media —— 回看已落盘的原始媒体文件（快捷键触发时留下的）。"""
    iid = get_app_instance_id() or ""
    media_path = (args.get("media_path") or "").strip()
    if not media_path:
        return registry.tool_error("必须传 media_path（感知事件 payload 里带的路径）")

    p = Path(media_path).expanduser()
    if not p.exists():
        return registry.tool_error(f"媒体文件不存在: {p}")

    question = (args.get("question") or "").strip() or "详细描述这个媒体的内容。"

    # 图片：直接走视觉
    if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
        cfg = load_config(iid)
        try:
            data_uri = encode_image_file(p, max_width=cfg.frame_max_width)
            vis = call_vision(
                image_data_uris=[data_uri],
                config=cfg,
                instance_id=iid,
                question_prompt=question + "\n直接用中文描述，不要输出 JSON。",
            )
            if vis.get("ok"):
                return vis.get("raw") or "(空)"
            return registry.tool_error(f"视觉模型失败: {vis.get('error')}")
        except Exception as exc:
            return registry.tool_error(f"处理失败: {exc}")

    # 音频：走 ASR
    if p.suffix.lower() in {".wav", ".mp3", ".m4a"}:
        cfg = load_config(iid)
        from infrastructure.perception.asr import transcribe_file

        out = transcribe_file(p, config=cfg, segment_paths=None)
        if out.get("ok"):
            return json.dumps({"ok": True, "transcript": out.get("text", "")}, ensure_ascii=False)
        return registry.tool_error(f"ASR 失败: {out.get('error')}")

    # 视频文件：提示用抽帧（本期 daemon 负责；这里直接回看不支持）
    if p.suffix.lower() in {".mp4", ".mov", ".m4v"}:
        return registry.tool_error(
            "视频回看需要先抽帧。本期请用快捷键重新触发，或手动用 ffmpeg 抽帧后用 sense_media 看图片。"
        )

    return registry.tool_error(f"不支持的媒体类型: {p.suffix}")


# ── 注册 ─────────────────────────────────────────────────────────────────────


registry.register(
    name="sense_screen",
    toolset="actions",
    schema={
        "name": "sense_screen",
        "description": (
            "截一张当前屏幕的画面，让视觉模型描述上面有什么。\n"
            "\n"
            "什么时候调：\n"
            "  - 你想知道用户当前在做什么、屏幕上显示了什么\n"
            "  - 用户说「看看我屏幕」「帮我看看这个」时\n"
            "\n"
            "注意：需要运行环境装有 mss 且授予屏幕录制权限；本机不可用时返回错误。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "向视觉模型提的问题。空时默认「描述当前屏幕值得注意的内容」。",
                    "default": "",
                },
            },
            "required": [],
        },
    },
    handler=_handle_sense_screen,
    check_fn=lambda: True,
    emoji="🖥️",
)


registry.register(
    name="sense_audio",
    toolset="actions",
    schema={
        "name": "sense_audio",
        "description": (
            "录一段麦克风音频并转写成文字。\n"
            "\n"
            "什么时候调：\n"
            "  - 你想听一下周围的声音 / 用户在说什么\n"
            "  - 用户说「听听」「周围有什么声音」时\n"
            "\n"
            "注意：需要 sounddevice 且授予麦克风权限。seconds 建议 3~15 秒。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "seconds": {
                    "type": "number",
                    "description": "录音时长（秒），1~30。默认 5。",
                    "default": 5,
                },
            },
            "required": [],
        },
    },
    handler=_handle_sense_audio,
    check_fn=lambda: True,
    emoji="🎙️",
)


registry.register(
    name="sense_media",
    toolset="actions",
    schema={
        "name": "sense_media",
        "description": (
            "回看一个已落盘的原始媒体文件（图片/音频），返回视觉模型描述或 ASR 转写。\n"
            "\n"
            "什么时候调：\n"
            "  - 收到 perception_signal 事件，payload 里有 media_path，想看原始画面/听原始音频\n"
            "  - 想确认感知系统理解得对不对\n"
            "\n"
            "支持：图片（png/jpg/webp）、音频（wav/mp3/m4a）。视频需先抽帧。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "media_path": {
                    "type": "string",
                    "description": "媒体文件绝对路径（来自 perception_signal 事件的 media_path）",
                },
                "question": {
                    "type": "string",
                    "description": "对图片提的问题；音频忽略此参数。",
                    "default": "",
                },
            },
            "required": ["media_path"],
        },
    },
    handler=_handle_sense_media,
    check_fn=lambda: True,
    emoji="🎬",
)
