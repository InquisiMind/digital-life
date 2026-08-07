"""视频抽帧 + 图片 base64 编码（spec FR-004）。

设计：
  - 录屏画面变化慢，按 ``frame_fps`` 抽帧（默认 2fps）足够还原操作流。
  - 空间降采样到 ``frame_max_width``（默认 720p 宽），进一步压体积。
  - 最终输出为 ``data:image/jpeg;base64,...`` 的 data URI 列表，可直接塞进
    GLM-4.6V 的 ``image_url`` 字段（spec：抽帧 + base64，不走 video_url）。

外部依赖（按需 import，缺失时降级）：
  - ``Pillow`` (PIL)：图片解码/缩放/JPEG 重编码。无 PIL 时直接用原始字节。
  - 视频抽帧：优先用 Pillow 的 ``ImageSequence``（GIF）或按时间戳截取；
    真实视频文件（mp4/mov）抽帧需要 ffmpeg，daemon 层负责调 ffmpeg 先抽成图片序列，
    本模块只处理"已有的图片序列"。

保持纯函数：输入路径/字节，输出 data URI，无 IO 副作用（除读文件）。
"""
from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

# 单张图片 base64 化后的软上限（bytes）。超了则降低 JPEG 质量。
_MAX_DATAURI_BYTES = 500_000  # ~500KB/帧，4 张 ≈ 2MB payload（快速上传 + 不超时）
_JPEG_QUALITY_STEPS = (60, 40, 30)  # 更激进压缩（屏幕文字 60 质量够读）


def _encode_image_bytes(img_bytes: bytes, mime: str = "image/jpeg") -> str:
    """把图片字节编码为 data URI。"""
    b64 = base64.b64encode(img_bytes).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _resize_encode(
    img_bytes: bytes,
    *,
    max_width: int,
    source_suffix: str = "",
) -> tuple[str, str]:
    """缩放 + JPEG 重编码，返回 (data_uri, mime)。

    无 Pillow 时降级为原始字节（不缩放）。
    """
    try:
        from io import BytesIO

        from PIL import Image
    except ImportError:
        mime = "image/png" if source_suffix.lower() in {".png"} else "image/jpeg"
        return _encode_image_bytes(img_bytes, mime), mime

    try:
        im = Image.open(BytesIO(img_bytes))
        im = im.convert("RGB")
        if im.width > max_width:
            new_h = round(im.height * max_width / im.width)
            im = im.resize((max_width, new_h), Image.LANCZOS)

        # 逐级降质量直到体积达标
        buf = BytesIO()
        for q in _JPEG_QUALITY_STEPS:
            buf = BytesIO()
            im.save(buf, format="JPEG", quality=q, optimize=True)
            if len(buf.getvalue()) <= _MAX_DATAURI_BYTES:
                break
        data = buf.getvalue()
        return _encode_image_bytes(data, "image/jpeg"), "image/jpeg"
    except Exception as exc:
        logger.debug("PIL resize failed, fallback raw: %s", exc)
        return _encode_image_bytes(img_bytes, "image/jpeg"), "image/jpeg"


def encode_image_file(path: str | Path, *, max_width: int = 1280) -> str:
    """读单张图片文件 → data URI（缩放降质）。

    供"模型主动观察"（sense_screen 截一张图）和 daemon 抽帧后单图使用。
    """
    p = Path(path)
    img_bytes = p.read_bytes()
    data_uri, _ = _resize_encode(img_bytes, max_width=max_width, source_suffix=p.suffix)
    return data_uri


def select_frame_timestamps(
    duration_seconds: float,
    *,
    fps: float,
    max_frames: int,
) -> list[float]:
    """按 fps 在 [0, duration] 内均匀选时间戳，截断到 ``max_frames``。

    纯函数：daemon 先用 ffmpeg 按这些时间戳抽帧成图片，再调
    :func:`encode_frame_images`。抽出时间戳这一步独立可测。

    >>> select_frame_timestamps(10.0, fps=2.0, max_frames=12)
    [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0]
    """
    if duration_seconds <= 0 or fps <= 0 or max_frames <= 0:
        return []
    step = 1.0 / fps
    # 从 step/2 开始而非 0，避免每段开头都抽到"刚切换的瞬态"
    timestamps: list[float] = []
    t = step / 2.0
    while t < duration_seconds and len(timestamps) < max_frames:
        timestamps.append(round(t, 3))
        t += step
    return timestamps


def encode_frame_images(
    image_paths: Iterable[str | Path],
    *,
    max_width: int = 1280,
) -> list[str]:
    """把一批图片文件编码为 data URI 列表（缩放降质，保持顺序）。

    输入是 daemon 已抽好的图片序列（任何格式），输出直接可塞进视觉模型 messages。
    """
    out: list[str] = []
    for p in image_paths:
        try:
            out.append(encode_image_file(p, max_width=max_width))
        except Exception as exc:
            logger.warning("encode frame %s failed: %s", p, exc)
    return out


def estimate_capture_seconds(num_frames: int, fps: float) -> float:
    """反推录制时长（供日志/校验用）。纯函数。"""
    if num_frames <= 0 or fps <= 0:
        return 0.0
    return num_frames / fps
