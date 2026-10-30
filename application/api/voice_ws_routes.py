"""HTTP/WS 路由：网页全双工语音会话。

仅 master 进程。两个入口：

  GET  /voice                 → 语音控制台页面（interfaces/web/voice-console）
  GET  /internal/voice/ws     → WebSocket：浏览器推 16k/16bit/mono PCM 二进制帧，
                                收下行 JSON（partial/sentence/echo_dropped/状态）。

下行消息（JSON）：
  {"type": "hello", "instance_id": ...}            连接成功（RTASR 已就绪）
  {"type": "partial", "text": ...}                 实时字幕（讯飞中间结果）
  {"type": "sentence", "text": ...}                确认句（已进入聚合窗口）
  {"type": "echo_dropped", "text": ...}            回声句被闸门丢弃
  {"type": "event_sent", "event_id":..., "text":...} utterance 已成事件（text=这段话全文）
  {"type": "agent_step", "name", "phase", "args", "result"}  执行步骤（思考/工具起止）
  {"type": "thinking_delta", "text": ...}          LLM 思考流式增量（攒批下发）
  {"type": "thinking_end", "text": ...}            一轮思考结束（全文）
  {"type": "agent_reply", "text": ...}             express_to_human 回复全文
  {"type": "tts_segment", "text": ...}             一段 TTS 文字标签（紧随 WAV 二进制帧）
  {"type": "stop_tts"}                             通知停播（打断）
  {"type": "barge_in", "partial", ...}             检测到真人打断
  {"type": "other_speaker", "text", "speaker"}     声纹门禁拦截的他人说话
  {"type": "asr_error", "message": ...}            讯飞流错误
  {"type": "error", "message": ...}                会话错误（即将关闭）
  {"type": "stopped", ...}                         会话结束摘要

上行：二进制帧 = raw PCM；文本帧 {"type": "stop"} = 主动结束，
     {"type": "tts_playback", "state": "start"|"end"} = 浏览器播放起止回报。
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiohttp import WSMsgType, web

logger = logging.getLogger(__name__)

_PAGE_PATH = Path(__file__).resolve().parents[2] / "interfaces" / "web" / "voice-console" / "index.html"


def _default_web_voice_instance() -> str:
    """默认语音会话实例（config/voice_sense.yaml 的 web_voice.default_instance）。"""
    try:
        import yaml

        cfg_path = Path(__file__).resolve().parents[2] / "config" / "voice_sense.yaml"
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        return str((cfg.get("web_voice") or {}).get("default_instance") or "").strip()
    except Exception:
        return ""


async def _handle_voice_page(_: web.Request) -> web.StreamResponse:
    if not _PAGE_PATH.exists():
        return web.Response(status=404, text="voice console page missing")
    return web.FileResponse(_PAGE_PATH, headers={"Cache-Control": "no-store"})


async def _handle_voice_ws(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=25)
    await ws.prepare(request)

    instance_id = (request.query.get("instance") or "").strip() or _default_web_voice_instance()
    if not instance_id:
        await ws.send_json({"type": "error", "message": "missing ?instance=<id> and no default configured"})
        await ws.close()
        return ws

    from infrastructure.perception.web_voice import get_web_voice_hub

    hub = get_web_voice_hub()
    loop = asyncio.get_running_loop()
    _client_gone = asyncio.Event()

    def _push(msg: dict) -> None:
        """RTASR 接收线程 → event loop（线程安全下行 JSON）。"""

        def _send() -> None:
            if not ws.closed:
                asyncio.ensure_future(ws.send_json(msg))

        if not _client_gone.is_set():
            loop.call_soon_threadsafe(_send)

    def _push_binary(data: bytes) -> None:
        """TTS WAV → event loop（WS binary 帧，前端按帧类型区分音频/JSON）。"""

        def _send() -> None:
            if not ws.closed:
                asyncio.ensure_future(ws.send_bytes(data))

        if not _client_gone.is_set():
            loop.call_soon_threadsafe(_send)

    ok, message, token = hub.start_session(instance_id, _push)
    if not ok:
        await ws.send_json({"type": "error", "message": message})
        await ws.close()
        return ws
    hub._session and hub._session.set_binary_push(_push_binary)
    await ws.send_json({"type": "hello", "instance_id": instance_id})
    logger.info("voice ws connected: instance=%s token=%s", instance_id[:8], token)

    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                hub.feed_pcm(msg.data)
            elif msg.type == WSMsgType.TEXT:
                try:
                    control = msg.json()
                except Exception:
                    continue
                ctype = str(control.get("type") or "")
                if ctype == "stop":
                    break
                if ctype == "tts_playback":
                    session = hub._session
                    if session is not None:
                        session.note_browser_playback(bool(control.get("state") == "start"))
                elif ctype == "tts_interrupted_at_seq":
                    # 浏览器回报：stop_tts 到达时正在播的段序号（假打断恢复起点）
                    session = hub._session
                    if session is not None:
                        session.note_interrupted_at_seq(control.get("seq"))
            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    finally:
        _client_gone.set()
        # 带 token 对账：若本连接已被新连接逐旧，这里 no-op 不误杀接管者
        summary = hub.stop_session(token=token)
        logger.info("voice ws closed: %s", summary)
        try:
            await ws.close()
        except Exception:
            pass
    return ws


async def _handle_voice_settings(request: web.Request) -> web.Response:
    """GET/POST /internal/voice/settings —— 前端实时配置（音色/灵敏度/倾听模式/提示词）。"""
    from infrastructure.perception.web_voice import (
        DEFAULT_SETTINGS, TTS_RATE_CANDIDATES, TTS_VOICE_CANDIDATES, get_web_voice_hub,
    )

    hub = get_web_voice_hub()
    if request.method == "GET":
        instances = []
        try:
            from infrastructure.config import discover_active_instances, get_instance_display_name

            for iid in discover_active_instances():
                try:
                    instances.append({"id": iid, "name": get_instance_display_name(iid) or iid[:8]})
                except Exception:
                    instances.append({"id": iid, "name": iid[:8]})
        except Exception:
            instances = []
        return web.json_response({
            "settings": dict(hub.settings),
            "voices": TTS_VOICE_CANDIDATES,
            "rates": TTS_RATE_CANDIDATES,
            "sensitivities": [
                {"value": "low", "label": "低（办公吵/少误触发）"},
                {"value": "mid", "label": "中（默认）"},
                {"value": "high", "label": "高（安静房间/抢话即停）"},
                {"value": "custom", "label": "自定义（展开数值）"},
            ],
            "instances": instances,
            "default_instance": _default_web_voice_instance(),
        })
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
    applied = hub.apply_settings(body or {})
    return web.json_response({"ok": True, "settings": applied})


async def _handle_voice_enroll(request: web.Request) -> web.Response:
    """POST /internal/voice/enroll —— 上传注册音频（binary PCM 16k/16bit/mono）。

    body = 原始 PCM 字节（前端用现有麦克风流录 15 秒直接 POST）。
    """
    from infrastructure.perception import voiceprint as vp

    instance_id = (request.query.get("instance") or "").strip()
    if not instance_id:
        instance_id = _default_web_voice_instance()
    if not instance_id:
        return web.json_response({"ok": False, "error": "missing instance"}, status=400)
    body = await request.read()
    if len(body) < 16000 * 2 * 5:  # <5 秒
        return web.json_response({"ok": False, "error": "音频太短（至少 5 秒）"}, status=400)
    ok = vp.enroll(instance_id, body)
    if not ok:
        # 失败必须带原因（2026-10-30 事故：ok=false 无 error，前端只显示"音频不足"
        # 兜底文案，用户注册无感知失败）——区分模型不可用 vs 提取失败
        error = (
            "声纹模型不可用（models/speaker-embed 缺失或损坏）"
            if vp._extractor_failed or vp._get_extractor() is None
            else "声纹提取失败（15 秒内有效语音不足，请对着麦克风正常说话）"
        )
        return web.json_response({"ok": False, "error": error, "enrolled": False})
    return web.json_response({"ok": True, "enrolled": True})


async def _handle_voice_enroll_status(request: web.Request) -> web.Response:
    """GET /internal/voice/enroll?instance=… —— 查注册状态。DELETE 清除。"""
    from infrastructure.perception import voiceprint as vp

    instance_id = (request.query.get("instance") or "").strip() or _default_web_voice_instance()
    if not instance_id:
        return web.json_response({"ok": False, "error": "missing instance"}, status=400)
    if request.method == "DELETE":
        vp.clear(instance_id)
        return web.json_response({"ok": True, "enrolled": False})
    return web.json_response({"ok": True, "enrolled": vp.is_enrolled(instance_id)})


async def _handle_speakers(request: web.Request) -> web.Response:
    """GET /internal/voice/speakers —— 说话人列表。
    POST {speaker_id, role?, label?} —— 手动标注（"这是我"/"这是背景"）。
    DELETE —— 清空注册表（重学）。"""
    from infrastructure.perception.speaker_registry import get_speaker_registry

    instance_id = (request.query.get("instance") or "").strip() or _default_web_voice_instance()
    if not instance_id:
        return web.json_response({"ok": False, "error": "missing instance"}, status=400)
    registry = get_speaker_registry(instance_id)
    if request.method == "GET":
        return web.json_response({"ok": True, "speakers": registry.list_speakers()})
    if request.method == "DELETE":
        registry.clear()
        return web.json_response({"ok": True, "cleared": True})
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
    sid = str(body.get("speaker_id") or "").strip()
    if not sid:
        return web.json_response({"ok": False, "error": "missing speaker_id"}, status=400)
    if body.get("role"):
        registry.set_role(sid, str(body["role"]))
    if body.get("label"):
        registry.set_label(sid, str(body["label"]))
    return web.json_response({"ok": True, "speakers": registry.list_speakers()})


async def _handle_voice_events(request: web.Request) -> web.Response:
    """GET /internal/voice/events —— 会话判定事件日志（调试灵敏度用）。"""
    from infrastructure.perception.web_voice import get_web_voice_hub

    session = get_web_voice_hub()._session
    events = session.recent_events() if session is not None else []
    return web.json_response({"events": events})


async def _handle_voice_history(request: web.Request) -> web.Response:
    """GET /internal/voice/history —— 最近对话历史（hub 级环形缓冲，跨会话存活）。

    前端页面加载时恢复对话流：item = {kind: user|agent|interrupt, text, ts}。
    """
    from infrastructure.perception.web_voice import get_web_voice_hub

    return web.json_response({"history": get_web_voice_hub().history()})


def add_voice_ws_routes(app: web.Application) -> None:
    """注册网页语音会话路由到 master HTTP server。"""
    app.router.add_get("/voice", _handle_voice_page)
    app.router.add_get("/internal/voice/ws", _handle_voice_ws)
    app.router.add_get("/internal/voice/settings", _handle_voice_settings)
    app.router.add_post("/internal/voice/settings", _handle_voice_settings)
    app.router.add_get("/internal/voice/events", _handle_voice_events)
    app.router.add_get("/internal/voice/history", _handle_voice_history)
    app.router.add_post("/internal/voice/enroll", _handle_voice_enroll)
    app.router.add_get("/internal/voice/enroll", _handle_voice_enroll_status)
    app.router.add_delete("/internal/voice/enroll", _handle_voice_enroll_status)
    app.router.add_get("/internal/voice/speakers", _handle_speakers)
    app.router.add_post("/internal/voice/speakers", _handle_speakers)
    app.router.add_delete("/internal/voice/speakers", _handle_speakers)
    logger.info("Voice web console registered: GET /voice + WS /internal/voice/ws + settings API")
