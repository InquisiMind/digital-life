"""HTTP 路由：接收感知 daemon（或模型主动观察外部触发）的感知请求。

仅在 master 进程的 HTTP server 上注册。daemon（独立子进程）通过 HTTP POST 调用。

两种调用模式：
  (a) 媒体路径模式（默认）：body 带 frame_paths / audio_path，endpoint 调 pipeline
      跑完视觉/ASR 理解再 emit 事件。
  (b) 结果直传模式：body 带 result（已是结构化理解），endpoint 只负责 emit 事件。
      适合 daemon 端已预处理或重试场景。

实例归属（spec FR-012）：
  body.instance_id 为空 → resolve_instance_id("") 落到默认实例（registry 第一个）。
  这就是"默认给 zero/默认实例"的落地方式——不硬编码实例名。

路由范式抄 employee_console_routes._instance_context_middleware 的三件套
（set_instance_context + set_current_instance_id + os.environ），finally reset。
emit_event 内部自动 _wake_or_inject，wake 后台线程自己重设 ContextVar，reset 不影响它。
"""
from __future__ import annotations

import logging
import os
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)


async def _handle_perception_trigger(request: web.Request) -> web.Response:
    """POST /internal/perception/trigger

    Body(JSON):
        instance_id?: str     目标实例（空→默认实例）
        source: str           感知来源（hotkey_screen/hotkey_audio/hotkey_both/sense_screen/...）
        frame_paths?: list[str]   daemon 已抽好的图片帧文件绝对路径
        audio_path?: str          原始音频文件路径
        audio_segment_paths?: list[str]  已切好的分段 wav 路径
        media_path?: str          原始媒体落盘路径（写进事件 payload 供回看）
        result?: object           已结构化的理解结果（直传模式，绕过 pipeline）
        session_id?: str          限定视觉上下文范围
        chat_id?: str             限定视觉上下文范围
    """
    try:
        body = await request.json()
    except Exception as exc:
        return web.json_response({"ok": False, "reason": f"bad json: {exc}"}, status=400)

    if not isinstance(body, dict):
        return web.json_response({"ok": False, "reason": "body must be object"}, status=400)

    raw_iid = body.get("instance_id") or ""
    source = (body.get("source") or "").strip()
    if not source:
        return web.json_response({"ok": False, "reason": "missing source"}, status=400)

    # 解析实例（空 → 默认实例）
    from infrastructure.config import resolve_instance_id, is_instance_active

    resolved = resolve_instance_id(raw_iid)
    if not is_instance_active(resolved):
        logger.warning(
            "perception trigger: instance %r not active (raw=%r) — emit anyway, cron will兜底",
            resolved, raw_iid,
        )

    # 设三件套（参照 employee_console._instance_context_middleware）
    from domain.lifecycle.events import set_instance_context, reset_instance_context
    from infrastructure.config import set_current_instance_id, reset_current_instance_id

    ev_tok = set_instance_context(resolved)
    cfg_tok = set_current_instance_id(resolved)
    prev_env = os.environ.get("DIGITAL_LIFE_INSTANCE_ID")
    os.environ["DIGITAL_LIFE_INSTANCE_ID"] = resolved
    try:
        return await _run_perception(body, resolved)
    finally:
        if prev_env is None:
            os.environ.pop("DIGITAL_LIFE_INSTANCE_ID", None)
        else:
            os.environ["DIGITAL_LIFE_INSTANCE_ID"] = prev_env
        reset_current_instance_id(cfg_tok)
        reset_instance_context(ev_tok)


async def _run_perception(body: dict[str, Any], instance_id: str) -> web.Response:
    """在已设好实例上下文的前提下执行感知流水线 + emit 事件。

    独立出来便于单测（可直接调用，不走 aiohttp）。
    """
    from infrastructure.perception import run_pipeline, PipelineResult

    source = body.get("source", "")

    # 直传模式
    direct_result = body.get("result")
    if isinstance(direct_result, dict) and direct_result.get("summary"):
        pr = PipelineResult(
            ok=bool(direct_result.get("ok", True)),
            source=source,
            summary=str(direct_result.get("summary", "")),
            details=direct_result.get("details") or {},
            transcript=str(direct_result.get("transcript", "")),
            media_path=str(direct_result.get("media_path") or body.get("media_path") or ""),
        )
    else:
        # 媒体路径模式 → 跑 pipeline（用 to_thread 避免阻塞 master event loop）
        import asyncio

        pr = await asyncio.to_thread(
            run_pipeline,
            instance_id=instance_id,
            source=source,
            frame_image_paths=body.get("frame_paths") or [],
            audio_path=body.get("audio_path"),
            audio_segment_paths=body.get("audio_segment_paths"),
            media_path_for_record=body.get("media_path") or "",
            session_id=body.get("session_id"),
            chat_id=body.get("chat_id"),
            reply_channel=body.get("reply_channel") or "",
        )

    payload = pr.to_payload()

    # emit 事件（emit_event 内部自动 _wake_or_inject）
    from domain.lifecycle.events import emit_event

    try:
        event_id = emit_event("perception_signal", payload)
        logger.info(
            "perception emitted: instance=%s source=%s ok=%s event_id=%d summary_head=%r",
            instance_id[:8], source, pr.ok, event_id, pr.summary[:60],
        )
        return web.json_response({
            "ok": True,
            "event_id": event_id,
            "perception_ok": pr.ok,
            "summary": pr.summary,
            "frames_used": pr.frames_used,
            "asr_ok": pr.asr_ok,
            "vision_ok": pr.vision_ok,
        })
    except Exception as exc:
        logger.exception("perception emit failed: %s", exc)
        return web.json_response(
            {"ok": False, "reason": f"emit failed: {exc}", "perception_ok": pr.ok, "summary": pr.summary},
            status=500,
        )


def add_perception_routes(app: web.Application) -> None:
    """注册感知 endpoint 路由到 aiohttp app。"""
    app.router.add_post("/internal/perception/trigger", _handle_perception_trigger)
    logger.info("Perception endpoint registered: POST /internal/perception/trigger")
