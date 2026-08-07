"""感知流水线编排（spec FR-004/FR-005/FR-006/FR-007）。

把"媒体 → 预处理 → 视觉理解"串成一条管线，供：
  - daemon（人类快捷键触发，spec US1）
  - 模型主动观察工具（sense_screen 等，spec US2）
  共用。

降级策略（spec FR-006）：视觉失败但 ASR 成功 → 只用转写；
ASR 失败但视觉成功 → 只用画面；都失败 → 返回 ok=False。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from infrastructure.perception.asr import transcribe_file
from infrastructure.perception.config import PerceptionConfig, load_config
from infrastructure.perception.context import build_slim_context, wake_meta_snapshot
from infrastructure.perception.frames import encode_frame_images
from infrastructure.perception.vision import call_vision

logger = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    """流水线产出。``to_payload()`` 转成 perception_signal 事件的 payload。"""

    ok: bool = False
    source: str = ""
    summary: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    transcript: str = ""
    media_path: str = ""
    error: str = ""
    # 诊断信息（不进事件 payload，仅日志/调试）
    frames_used: int = 0
    asr_ok: bool | None = None
    vision_ok: bool | None = None

    def to_payload(self) -> dict[str, Any]:
        """转成 perception_signal 事件 payload（spec FR-013）。"""
        return {
            "source": self.source,
            "summary": self.summary or ("感知处理失败：" + self.error) if self.error else self.summary,
            "details": self.details,
            "transcript": self.transcript,
            "media_path": self.media_path,
            "ok": self.ok,
        }


def run_pipeline(
    *,
    instance_id: str,
    source: str,
    frame_image_paths: list[str | Path] | None = None,
    audio_path: str | Path | None = None,
    audio_segment_paths: list[str] | None = None,
    config: PerceptionConfig | None = None,
    media_path_for_record: str = "",
    session_id: str | None = None,
    chat_id: str | None = None,
    question_prompt: str | None = None,
) -> PipelineResult:
    """端到端跑感知流水线。

    Args:
        instance_id: 目标实例（决定读谁的 audit + 用谁的凭据）。
        source: 感知来源标记（如 ``hotkey_screen``、``sense_screen``、``hotkey_both``）。
        frame_image_paths: daemon 已抽好的图片帧文件列表。
            为空则跳过视觉（纯音频路径，spec FR-006 降级）。
        audio_path: 原始音频文件（若 audio_segment_paths 给定则仅用于记录）。
        audio_segment_paths: daemon 切好的分段 wav 列表。
        config: 配置；None 按 instance_id 加载。
        media_path_for_record: 原始媒体落盘路径（写进事件 payload，供回看）。
        session_id / chat_id: 限定精简上下文范围（可选）。
        question_prompt: 自定义视觉指令。

    Returns:
        :class:`PipelineResult`。
    """
    cfg = config or load_config(instance_id)
    result = PipelineResult(source=source, media_path=str(media_path_for_record))

    # 1. 图片帧 → data URIs（spec FR-004）
    image_uris: list[str] = []
    if frame_image_paths:
        image_uris = encode_frame_images(
            frame_image_paths,
            max_width=cfg.frame_max_width,
        )[: cfg.max_frames]
        result.frames_used = len(image_uris)
        if not image_uris:
            result.error = "图片帧编码失败"

    # 2. ASR（spec FR-005）
    if audio_segment_paths or audio_path:
        asr_target = audio_path if audio_segment_paths is None and audio_path else None
        asr_out = transcribe_file(
            asr_target or "",
            config=cfg,
            segment_paths=audio_segment_paths,
        )
        result.asr_ok = asr_out.get("ok", False)
        result.transcript = asr_out.get("text", "")
        if not asr_out.get("ok"):
            logger.info("pipeline asr degraded: %s", asr_out.get("error"))

    # 3. 视觉理解（spec FR-007）—— 需要图片或转写至少一路可用
    if image_uris or result.transcript:
        history = build_slim_context(
            instance_id,
            session_id=session_id,
            chat_id=chat_id,
            recent_turns=cfg.context_recent_turns,
        )
        wake_meta = wake_meta_snapshot(instance_id)
        task_hint = cfg.vision_task_hint
        if wake_meta.get("reason"):
            task_hint = (task_hint + "\n").lstrip() + f"（主意识当前任务：{wake_meta['reason']}）"

        vcfg = cfg
        # 把 task_hint 注入 config 副本（dataclass frozen，用 replace）
        if task_hint and task_hint != cfg.vision_task_hint:
            from dataclasses import replace

            vcfg = replace(cfg, vision_task_hint=task_hint)

        vis = call_vision(
            image_data_uris=image_uris,
            transcript=result.transcript,
            history_messages=history,
            config=vcfg,
            instance_id=instance_id,
            question_prompt=question_prompt,
        )
        result.vision_ok = vis.get("ok", False)

        parsed = vis.get("parsed") or {}
        if parsed:
            result.summary = parsed.get("summary", "") or vis.get("raw", "")
            details = parsed.get("details") or {}
            if isinstance(details, dict):
                result.details = details
            else:
                result.details = {"raw_details": details}
            if parsed.get("related_to_background"):
                result.details["related_to_background"] = parsed["related_to_background"]
            result.details["should_notify"] = parsed.get("should_notify", True)
        else:
            # JSON 解析失败，用 raw 当 summary
            result.summary = (vis.get("raw") or "")[:200]
            result.details = {"parse_failed": True, "raw": vis.get("raw", "")}

        if not vis.get("ok"):
            result.error = result.error or vis.get("error", "视觉调用失败")
    else:
        result.error = result.error or "无图片帧且无音频可用"

    # 4. 汇总 ok（spec FR-006 降级：至少一路成功即 ok）
    any_input = bool(image_uris) or bool(audio_segment_paths) or bool(audio_path)
    some_output = result.vision_ok is True or bool(result.transcript)
    result.ok = any_input and some_output and bool(result.summary)

    logger.info(
        "perception pipeline done: instance=%s source=%s ok=%s frames=%d asr=%s vision=%s",
        (instance_id or "")[:8], source, result.ok, result.frames_used,
        result.asr_ok, result.vision_ok,
    )
    return result
