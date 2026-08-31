"""ASR 语音转写（glm-asr-2512，spec FR-005）。

官方约束（调研确认）：
  - endpoint: ``POST {base_url}/audio/transcriptions``
  - 单次 ≤ 30s、≤ 25MB；格式 wav/mp3
  - 支持 ``prompt``（上文，长音频分段时保持连贯）、``hotwords``（领域热词）

本模块负责：
  - 把一段音频文件按 30s 分段
  - 逐段调 ASR，把上一段结果作为下一段的 ``prompt``
  - 拼接成完整转写文本

分段调度是纯函数（:func:`split_audio_segments`），可独立单测；
真实切分音频文件需要 ffmpeg，daemon 层负责切，这里只规划时间戳。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

from infrastructure.perception.config import ASR_SEGMENT_SECONDS, PerceptionConfig

logger = logging.getLogger(__name__)


def split_audio_segments(duration_seconds: float, *, segment_seconds: float = ASR_SEGMENT_SECONDS) -> list[tuple[float, float]]:
    """把音频时长切成 [(start, end), ...] 分段，每段 ≤ segment_seconds。

    纯函数，可独立单测。最后一段可能短于 segment_seconds。

    >>> split_audio_segments(75.0)
    [(0.0, 30.0), (30.0, 60.0), (60.0, 75.0)]
    """
    if duration_seconds <= 0 or segment_seconds <= 0:
        return []
    segs: list[tuple[float, float]] = []
    t = 0.0
    while t < duration_seconds:
        end = min(t + segment_seconds, duration_seconds)
        segs.append((round(t, 3), round(end, 3)))
        t = end
    return segs


def probe_audio_duration(path: str | Path) -> float:
    """探测音频时长（秒）。需要 ffmpeg；失败返回 0（调用方兜底为"单段"）。"""
    import shutil
    import subprocess

    ff = shutil.which("ffprobe") or shutil.which("ffmpeg")
    if not ff:
        return 0.0
    try:
        cmd = [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ]
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=15).decode().strip()
        return float(out) if out else 0.0
    except Exception as exc:
        logger.debug("probe duration failed: %s", exc)
        return 0.0


def _transcribe_segment(
    audio_bytes: bytes,
    *,
    filename: str,
    config: PerceptionConfig,
    prompt: str = "",
) -> str:
    """调一次 ASR endpoint，返回该段文本。失败抛异常（调用方捕获）。

    根据 ``config.asr_provider`` 路由到不同 ASR 后端：
      - ``glm``（默认）：智谱 glm-asr-2512，HTTP API
      - ``iflytek``：科大讯飞语音听写流式版，Websocket API
    """
    provider = getattr(config, "asr_provider", "glm")

    if provider == "iflytek":
        return _transcribe_segment_iflytek(audio_bytes, config=config)

    return _transcribe_segment_glm(audio_bytes, filename=filename, config=config, prompt=prompt)


def _transcribe_segment_glm(
    audio_bytes: bytes,
    *,
    filename: str,
    config: PerceptionConfig,
    prompt: str = "",
) -> str:
    """智谱 GLM ASR 转写（HTTP API，OpenAI Whisper 兼容）。"""
    url = f"{config.base_url}/audio/transcriptions"
    data: dict[str, str] = {
        "model": config.asr_model,
        "stream": "false",
    }
    if prompt:
        data["prompt"] = prompt
    if config.asr_hotwords:
        # hotwords 是 JSON 数组字符串
        import json as _json

        data["hotwords"] = _json.dumps(list(config.asr_hotwords), ensure_ascii=False)

    files = {"file": (filename, audio_bytes, "audio/wav")}
    headers = {"Authorization": f"Bearer {config.api_key}"}
    with httpx.Client(timeout=120.0) as client:
        r = client.post(url, data=data, files=files, headers=headers)
        r.raise_for_status()
        data_resp = r.json()
    # 智谱 ASR 返回 {"text": "..."}（OpenAI Whisper 兼容）
    return (data_resp.get("text") or "").strip()


def _transcribe_segment_iflytek(
    audio_bytes: bytes,
    *,
    config: PerceptionConfig,
) -> str:
    """科大讯飞 IAT 流式 ASR 转写（Websocket API）。"""
    from infrastructure.perception.iflytek_asr import transcribe_iflytek

    if not config.iflytek_app_id or not config.iflytek_api_key or not config.iflytek_api_secret:
        raise RuntimeError("讯飞 ASR 凭据未配置（需要 app_id / api_key / api_secret）")

    return transcribe_iflytek(
        audio_bytes,
        app_id=config.iflytek_app_id,
        api_key=config.iflytek_api_key,
        api_secret=config.iflytek_api_secret,
        language=config.iflytek_language,
        accent=config.iflytek_accent,
        hotwords=config.asr_hotwords,
    )


def transcribe_segment(
    audio_bytes: bytes, *, filename: str, config: PerceptionConfig, prompt: str = "",
) -> str:
    """公开单段转写（live 增量转写路径用）。失败抛异常（调用方捕获）。"""
    return _transcribe_segment(audio_bytes, filename=filename, config=config, prompt=prompt)


def transcribe_file(
    audio_path: str | Path,
    *,
    config: PerceptionConfig,
    segment_paths: list[str] | None = None,
) -> dict[str, Any]:
    """转写音频文件，返回 ``{"text": 全文, "ok": bool, "error": str, "segments": n}``。

    Args:
        audio_path: 原始音频文件（若 segment_paths 给定则忽略其内容）。
        config: 感知配置。
        segment_paths: daemon 已切好的分段 wav 文件列表（每段 ≤ 30s）。
            为空时降级为"整文件单次转写"（超时长会失败，但保留兜底）。

    分段间用上一段结果作为下一段的 ``prompt``，保持连贯（spec FR-005）。
    """
    # 凭据检查：根据 provider 验证对应凭据
    _provider = getattr(config, "asr_provider", "glm")
    if _provider == "iflytek":
        if not config.iflytek_app_id:
            return {"ok": False, "error": "讯飞 app_id 未配置", "text": "", "segments": 0}
    elif not config.api_key:
        return {"ok": False, "error": "LLM_API_KEY 未配置", "text": "", "segments": 0}

    paths = segment_paths or [str(audio_path)]
    full_text_parts: list[str] = []
    prev = ""
    failures = 0
    for i, p in enumerate(paths):
        try:
            seg_bytes = Path(p).read_bytes()
            chunk = _transcribe_segment(
                seg_bytes,
                filename=f"seg-{i:03d}.wav",
                config=config,
                prompt=prev,
            )
            if chunk:
                full_text_parts.append(chunk)
                prev = chunk  # 作为下一段 prompt
        except Exception as exc:
            logger.warning("ASR segment %d failed: %s", i, exc)
            failures += 1

    text = "\n".join(t for t in full_text_parts if t).strip()
    if not text and failures == len(paths):
        return {"ok": False, "error": f"全部 {failures} 段转写失败", "text": "", "segments": len(paths)}
    return {
        "ok": bool(text),
        "error": "" if text else "转写结果为空",
        "text": text,
        "segments": len(paths),
        "segment_failures": failures,
    }
