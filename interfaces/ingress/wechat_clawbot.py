"""WeChat ClawBot adapter —— 微信个人号机器人（腾讯 OpenClaw / iLink 协议）。

ClawBot 是腾讯 2026 年通过 OpenClaw 平台开放的微信个人号官方 Bot API。
底层走 iLink 协议（HTTP 长轮询，类似 Telegram Bot API 的 getUpdates）。

平台知识收口在这里：
  - 认证：扫码 → bot_token（Bearer auth）
  - 收消息：POST /ilink/bot/getupdates（hold 35s），用 get_updates_buf 游标推进
  - 发消息：POST /ilink/bot/sendmessage（必须带 context_token 关联对话窗口）
  - ID 格式：from_user_id = "xxx@im.wechat"，to_user_id = "xxx@im.bot"

限制（ChannelCapabilities 已声明）：
  - 仅支持私聊，不支持群聊
  - 不能主动推送，必须用户先发消息触发（有 context_token 才能回复）
  - 不支持媒体（图片/语音/文件，当前 SDK 限制）
  - 不支持 @（私聊场景不需要）

参考实现：github.com/SiverKing/weixin-ClawBot-API（bot.py + weixin-bot-api.md）
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import random
import time
from typing import Any

import httpx

from interfaces.ingress.base import (
    ChannelCapabilities,
    MessageHandler,
    NormalizedMessage,
)

logger = logging.getLogger("digital_life.ingress.wechat_clawbot")


# ClawBot 能力：私聊 only，不能群聊，不能主动推送
CLAWBOT_CAPABILITIES = ChannelCapabilities(
    supports_group=False,
    supports_dm=True,
    supports_proactive=False,   # 必须有 context_token 才能回
    supports_media=False,
    supports_mention=False,
    max_text_length=2000,
)

_DEFAULT_DOMAIN = "https://ilinkai.weixin.qq.com"
_POLL_TIMEOUT = 40  # 长轮询 hold 时间（ClawBot 服务端最多 hold 35s，we 给 40s）


class WeChatClawBotAdapter:
    """微信 ClawBot 个人号机器人 adapter。"""

    platform = "wechat"
    capabilities = CLAWBOT_CAPABILITIES

    def __init__(
        self,
        bot_token: str,
        *,
        domain: str | None = None,
        bot_id: str = "",
    ):
        """初始化 ClawBot adapter。

        Args:
            bot_token: 扫码登录后获得的 Bearer token
            domain: ClawBot API 域名（默认 https://ilinkai.weixin.qq.com）
            bot_id: 机器人自身 ID（xxx@im.bot），getupdates 返回时从消息里推断
        """
        self._bot_token = bot_token.strip()
        self._domain = (domain or _DEFAULT_DOMAIN).rstrip("/")
        self._bot_id = bot_id
        self._poll_buf = ""  # getupdates 游标
        self._handlers: list[MessageHandler] = []
        self._poll_task: asyncio.Task | None = None
        self._running = False

    @property
    def app_identity(self) -> str:
        """机器人稳定身份标识。"""
        return self._bot_id or "wechat_clawbot"

    def on_message(self, handler: MessageHandler) -> None:
        """注册消息回调。"""
        self._handlers.append(handler)

    async def start(self) -> None:
        """启动长轮询循环。"""
        if not self._bot_token:
            logger.error("WeChatClawBotAdapter: no bot_token configured, skipping start")
            return
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("WeChatClawBotAdapter started (%s)", self._bot_id or "bot_id pending")

    async def stop(self) -> None:
        """停止长轮询。"""
        self._running = False
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await asyncio.wait_for(self._poll_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        logger.info("WeChatClawBotAdapter stopped")

    # ── 长轮询循环 ────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """持续长轮询 ClawBot getupdates。"""
        while self._running:
            try:
                messages = await self._get_updates()
                if messages:
                    logger.info("ClawBot received %d message(s)", len(messages))
                for raw_msg in messages:
                    # 提取 context_token（用 client_id 字段，ClawBot 2.4.4 实测）
                    context_token = str(raw_msg.get("context_token") or raw_msg.get("client_id") or "")
                    normalized = self._normalize(raw_msg)
                    if normalized:
                        if context_token:
                            normalized.context_token = context_token
                        # 更新 bot_id（从第一条消息推断）
                        if not self._bot_id and raw_msg.get("to_user_id"):
                            self._bot_id = raw_msg["to_user_id"]
                        logger.info(
                            "ClawBot message normalized: platform=%s sender=%s content=%s",
                            normalized.platform,
                            normalized.sender_id[:20],
                            normalized.content[:50],
                        )
                        for handler in self._handlers:
                            try:
                                await handler(normalized)
                            except Exception as exc:
                                logger.error("ClawBot handler error: %s", exc)
                    else:
                        logger.debug("ClawBot message skipped (no text content)")
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("ClawBot poll error: %s", exc)
                await asyncio.sleep(5)

    async def _get_updates(self) -> list[dict[str, Any]]:
        """POST /ilink/bot/getupdates 长轮询。

        返回格式（ClawBot 2.4.4 实测）：
        {"msgs": [{from_user_id, to_user_id, message_id, context_token,
                    item_list: [{type:1, content:"文本"}], ...}],
         "get_updates_buf": "游标"}
        """
        url = f"{self._domain}/ilink/bot/getupdates"
        headers = self._build_headers()
        payload: dict[str, Any] = {}
        if self._poll_buf:
            payload["get_updates_buf"] = self._poll_buf

        async with httpx.AsyncClient(timeout=_POLL_TIMEOUT) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()

        # 检查错误
        if data.get("errcode"):
            ec = data.get("errcode")
            if ec == -14:  # session timeout
                logger.warning("ClawBot session timeout — token may have expired")
            return []

        # 推进游标
        if data.get("get_updates_buf"):
            self._poll_buf = data["get_updates_buf"]

        # 消息列表在 "msgs" 字段里
        msgs = data.get("msgs") or data.get("item_list") or data.get("updates") or []
        if not isinstance(msgs, list):
            msgs = []
        return msgs

    # ── 发送 ──────────────────────────────────────────────────────────

    async def send(self, chat_id: str, content: str, *, context_token: str = "", reply_to: str = "") -> bool:
        """POST /ilink/bot/sendmessage。

        ClawBot 要求必须带 context_token（从收到的消息里取）关联对话窗口。
        如果没有 context_token，返回 False（capabilities.supports_proactive=False 的原因）。

        Args:
            chat_id: 对方 user_id（xxx@im.wechat），兼容旧签名
            content: 文本内容
            context_token: 从收到的消息里取的对话上下文 token（ClawBot 必须要有）
            reply_to: 不用（ClawBot 没有 reply 语义）
        """
        if not context_token:
            logger.warning("ClawBot send: missing context_token — ClawBot requires it to associate the reply")
            return False

        # 超长文本分段发送（替代旧的静默截断 content[:max]）。
        # 详见 interfaces/ingress/text_segmenter.py。
        from interfaces.ingress.text_segmenter import split_text_for_send

        segments = split_text_for_send(content, self.capabilities.max_text_length)
        url = f"{self._domain}/ilink/bot/sendmessage"
        headers = self._build_headers()
        sent_count = 0
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                for idx, seg in enumerate(segments):
                    payload = {
                        "context_token": context_token,
                        "to_user_id": chat_id,
                        "item_list": [{"type": 1, "content": seg}],
                    }
                    try:
                        resp = await client.post(url, headers=headers, json=payload)
                        resp.raise_for_status()
                        result = resp.json()
                        if result.get("ret") == 0 or result.get("errcode") == 0:
                            sent_count += 1
                            continue
                        logger.warning(
                            "ClawBot send segment %d/%d response: %s",
                            idx + 1, len(segments), result,
                        )
                    except Exception as exc:
                        logger.warning(
                            "ClawBot send segment %d/%d failed: %s",
                            idx + 1, len(segments), exc,
                        )
                    # ClawBot 限速比飞书紧，段间稍等避免被风控；末段无需 sleep
                    if idx < len(segments) - 1:
                        await asyncio.sleep(0.3)
            if sent_count == len(segments):
                return True
            # 部分送达：保留已成功段（不回滚），但按 IngressAdapter 契约返回 False
            logger.warning(
                "ClawBot send partial: %d/%d segments delivered", sent_count, len(segments),
            )
            return False
        except Exception as exc:
            logger.error("ClawBot send failed: %s", exc)
            return False

    # ── 消息标准化 ────────────────────────────────────────────────────

    def _normalize(self, raw: dict[str, Any]) -> NormalizedMessage | None:
        """把 ClawBot 的 JSON 消息转成 NormalizedMessage。"""
        if not raw:
            return None
        # DEBUG：把 raw 打出来方便对齐字段
        logger.info("ClawBot _normalize raw keys: %s", list(raw.keys()))
        # 提取文本内容（ClawBot 2.4.4 格式：type=1 在 text_item.text 里）
        content = ""
        items = raw.get("item_list") or []
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get("type") == 1:
                    # 优先 text_item.text（真实格式）
                    ti = item.get("text_item") or {}
                    content = str(ti.get("text") or "").strip()
                    if content:
                        break
                    # fallback content（老格式兼容）
                    c = str(item.get("content") or "").strip()
                    if c:
                        content = c
                        break
        if not content:
            logger.info("ClawBot _normalize: no text (item_list=%s)", items[:1] if items else "[]")
            return None

        from_user = str(raw.get("from_user_id") or "")
        to_user = str(raw.get("to_user_id") or "")
        # 推断 bot_id
        if to_user and not self._bot_id:
            self._bot_id = to_user

        return NormalizedMessage(
            platform="wechat",
            chat_id=from_user,           # 私聊：chat_id = 对方 user_id
            message_id=str(raw.get("message_id") or ""),
            sender_id=from_user,
            content=content,
            sender_name=str(raw.get("from_nickname") or ""),
            chat_name="",                # 私聊没有群名
            is_group=False,              # ClawBot 仅私聊
            mentions_bot=False,          # 私聊不需要 @
            sender_is_bot=False,         # ClawBot 不接收 bot → bot 消息
            context_token=str(raw.get("context_token") or ""),  # send 时回传
            raw=raw,
        )

    # ── 认证 header ───────────────────────────────────────────────────

    def _build_headers(self) -> dict[str, str]:
        """每次请求构建的认证 header。

        ClawBot 要求（从 npm 包 api.js 源码确认）：
        - Authorization: Bearer {bot_token}
        - AuthorizationType: ilink_bot_token  ← 这个我们之前漏了！
        - Content-Type: application/json
        - X-WECHAT-UIN: 随机 uint32 → decimal → base64
        - iLink-App-Id: bot（固定值，来自 package.json ilink_appid）
        - iLink-App-ClientVersion: 132100（uint32 编码 2.4.4）
        """
        headers = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "Authorization": f"Bearer {self._bot_token}",
            "X-WECHAT-UIN": base64.b64encode(str(random.randint(0, 0xFFFFFFFF)).encode()).decode(),
            "iLink-App-Id": "bot",
            "iLink-App-ClientVersion": "132100",
        }
        return headers


# ── 扫码登录（一次性 bootstrap）─────────────────────────────────────────

async def login_clawbot_qrcode(
    domain: str | None = None,
    *,
    timeout: int = 120,
) -> tuple[str, str]:
    """引导用户扫码登录，返回 (bot_token, bot_id)。

    流程：
    1. POST /ilink/bot/get_bot_qrcode → 拿二维码 URL
    2. 循环 POST /ilink/bot/get_qrcode_status 直到 status=confirmed
    3. 拿到 bot_token + bot_id 返回

    这是一个 CLI / 控制台工具函数，不在 adapter runtime 中执行。
    """
    _domain = (domain or _DEFAULT_DOMAIN).rstrip("/")
    async with httpx.AsyncClient(timeout=_POLL_TIMEOUT) as client:
        # 1. 拿二维码
        resp = await client.post(f"{_domain}/ilink/bot/get_bot_qrcode")
        resp.raise_for_status()
        qr_data = resp.json()
        qr_url = qr_data.get("qrcode_url") or qr_data.get("url") or ""
        session_key = qr_data.get("session_key") or qr_data.get("ticket") or ""

        if qr_url:
            logger.info("请扫码登录微信 ClawBot: %s", qr_url)

        # 2. 等扫码确认
        deadline = time.time() + timeout
        while time.time() < deadline:
            await asyncio.sleep(3)
            resp = await client.post(
                f"{_domain}/ilink/bot/get_qrcode_status",
                json={"session_key": session_key},
            )
            status_data = resp.json()
            status = status_data.get("status") or status_data.get("ret")
            if status in ("confirmed", "ok", 0, "0"):
                bot_token = status_data.get("bot_token") or ""
                bot_id = status_data.get("bot_id") or ""
                if bot_token:
                    logger.info("ClawBot 登录成功！bot_id=%s", bot_id)
                    return bot_token, bot_id
        raise TimeoutError("ClawBot 扫码登录超时")


__all__ = [
    "WeChatClawBotAdapter",
    "CLAWBOT_CAPABILITIES",
    "login_clawbot_qrcode",
]
