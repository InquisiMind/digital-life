"""HTTP 路由: 接收微信消息推送 (itchat daemon → digital-life)。

itchat daemon 在另一台机器跑微信 Web 协议, 收到消息后 HTTP POST 到本 endpoint。
digital-life 统一入库 social_feed, source="wechat"。
"""
from __future__ import annotations

import logging
import time
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)


async def _handle_wechat_ingest(request: web.Request) -> web.Response:
    """POST /internal/wechat-ingest

    Body (JSON):
        chat_id: 微信聊天 ID (wxid 或 roomid)
        chat_name: 聊天名 (群名 / 联系人昵称)
        sender_id: 发送者 wxid
        sender_name: 发送者昵称
        text: 消息文本
        is_group: 是否群消息
        msg_ref: 消息唯一 ID (去重 key)
        timestamp: 消息时间戳 (秒, 微信用秒级)
        media_type: 消息类型 (text/image/file/...) 默认 text

    不需要鉴权 (仅内网 / 同机调用)。
    """
    try:
        payload = await request.json()
    except Exception as exc:
        logger.warning("wechat-ingest: bad json: %s", exc)
        return web.json_response({"ok": False, "reason": f"bad json: {exc}"}, status=400)

    chat_id = str(payload.get("chat_id") or "")
    if not chat_id:
        return web.json_response({"ok": False, "reason": "chat_id required"}, status=400)

    text = str(payload.get("text") or "")
    if not text.strip():
        # 非文本消息 (图片/文件/语音) — 暂时存一条占位
        media_type = str(payload.get("media_type") or "image")
        text = f"[{media_type}]"

    chat_name = str(payload.get("chat_name") or "")
    sender_name = str(payload.get("sender_name") or "")
    sender_id = str(payload.get("sender_id") or "")
    is_group = bool(payload.get("is_group"))
    msg_ref = str(payload.get("msg_ref") or f"wx_{int(time.time()*1000)}")

    # 微信时间戳可能是秒级, 统一转毫秒级
    ts_raw = float(payload.get("timestamp") or 0)
    if ts_raw > 0 and ts_raw < 1e12:
        ts_raw *= 1000  # 秒 → 毫秒

    try:
        from domain.social.store import insert_message
        is_new = insert_message(
            source="wechat",
            chat_id=chat_id,
            chat_name=chat_name,
            message_id=msg_ref,
            sender_name=sender_name,
            sender_id=sender_id,
            text=text,
            message_ts=ts_raw,
            instance_id="",  # 不绑定特定实例
            at_all=False,
            sender_is_app=False,
            at_me=False,
        )
        if is_new:
            from_short = (sender_name or sender_id or "?")[:10]
            chat_short = (chat_name or chat_id)[:16]
            logger.info("wechat-ingest: chat=%s from=%s text_head=%r",
                        chat_short, from_short, text[:40])
        return web.json_response({"ok": True, "new": is_new})
    except Exception as exc:
        logger.exception("wechat-ingest error: %s", exc)
        return web.json_response({"ok": False, "reason": str(exc)}, status=500)


def add_wechat_ingest_routes(app: web.Application) -> None:
    """注册微信消息接收 endpoint。"""
    app.router.add_post("/internal/wechat-ingest", _handle_wechat_ingest)
    logger.info("WeChat ingest endpoint registered: POST /internal/wechat-ingest")
