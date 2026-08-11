"""HTTP 路由：语音感知控制端点。

仅在 master 进程的 HTTP server 上注册。实例（通过 voice_focus 工具）或
外部按键触发器通过 HTTP POST 调用，控制 AudioSenseService 的状态。

路由：
  POST /internal/voice/control {action, instance_id?}
    action: "focus" | "dialog" | "dormant" | "status"
    返回：{"ok": bool, "state": str, "action": str}
"""
from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)

# 全局 AudioSenseService 实例（由 server.py master 启动路径注入）
_voice_sense_service: Any = None


def set_voice_sense_service(svc: Any) -> None:
    """由 server.py master 启动路径调用，注入 AudioSenseService 实例。"""
    global _voice_sense_service
    _voice_sense_service = svc


async def _handle_voice_control(request: web.Request) -> web.Response:
    """POST /internal/voice/control — 控制 AudioSenseService 状态。"""
    if _voice_sense_service is None:
        return web.json_response(
            {"ok": False, "error": "voice sense service not available"},
            status=503,
        )
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

    action = str(body.get("action", "")).strip().lower()
    instance_id = str(body.get("instance_id", "")).strip()

    if not action:
        return web.json_response({"ok": False, "error": "missing 'action'"}, status=400)

    result = _voice_sense_service.control(action, instance_id)
    return web.json_response(result)


def add_voice_sense_routes(app: web.Application) -> None:
    """注册语音感知控制路由到 master HTTP server。"""
    app.router.add_post("/internal/voice/control", _handle_voice_control)
    logger.info("Voice sense control endpoint registered: POST /internal/voice/control")
