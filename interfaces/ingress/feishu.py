"""Feishu (Lark) message platform adapter.

Consolidates what was previously scattered across:
- infrastructure/gateway/server.py (WS + HTTP client)
- infrastructure/feishu/life_consumer.py (message processing)
- backend/ingress_interactions/feishu/ (normalization)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from typing import Any
from uuid import uuid4

import lark_oapi as lark
from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
from lark_oapi.ws import Client as WSClient

from interfaces.ingress.base import IngressAdapter, MessageHandler, NormalizedMessage

logger = logging.getLogger(__name__)

# 飞书域名：优先从实例 app.yaml 的 channels.feishu.feishu_domain 读（init_instance
# 和 ConfigCenter 都写这），再 fallback env FEISHU_DOMAIN，最终默认国内地址。
# ⚠️ 通道字段统一收敛到 channels.<type>，无 messenger 旧段兜底；老实例需先跑
#    scripts/migrate_messenger_to_channels.py 迁移。
def _read_feishu_domain() -> str:
    try:
        from infrastructure.config import get_app_instance_id, get_project_root
        iid = get_app_instance_id() or ""
        if iid:
            import yaml as _yaml
            cfg_path = get_project_root() / "apps" / iid / "config" / "app.yaml"
            if cfg_path.exists():
                cfg = _yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
                # channels.feishu.feishu_domain（标准路径）
                d = (((cfg.get("channels") or {}).get("feishu") or {}).get("feishu_domain") or "").strip()
                if d:
                    return d
    except Exception:
        pass
    return os.getenv("FEISHU_DOMAIN", "https://open.feishu.cn")

FEISHU_DOMAIN = _read_feishu_domain()
if FEISHU_DOMAIN and not FEISHU_DOMAIN.startswith(("https://", "http://")):
    FEISHU_DOMAIN = "https://" + FEISHU_DOMAIN


# Patch lark_oapi.ws.client so that each adapter thread gets its own event
# loop.  The library uses a module-level ``loop`` variable that is shared
# across all WSClient instances; with multiple bot credentials we need
# per-thread isolation.
import lark_oapi.ws.client as _ws_client

_per_thread_loops: dict[int, asyncio.AbstractEventLoop] = {}

class _ThreadLocalLoopProxy:
    """Delegates all asyncio-loop calls to the current thread's loop."""
    def _loop(self):
        return _per_thread_loops[threading.get_ident()]

    def run_until_complete(self, *args, **kwargs):
        return self._loop().run_until_complete(*args, **kwargs)

    def create_task(self, *args, **kwargs):
        return self._loop().create_task(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._loop(), name)

_ws_client.loop = _ThreadLocalLoopProxy()


class FeishuAdapter(IngressAdapter):
    """Feishu / Lark message platform adapter."""

    platform = "feishu"
    # 飞书全能力：群聊/私聊/主动推送/媒体/@ 全支持
    from interfaces.ingress.base import ChannelCapabilities as _Caps
    capabilities = _Caps(
        supports_group=True,
        supports_dm=True,
        supports_proactive=True,
        supports_media=True,
        supports_mention=True,
        max_text_length=4000,
    )

    @property
    def app_identity(self) -> str:
        # 飞书 adapter 的稳定身份 = app_id (cli_xxx)。给 unified_contact 体系用：
        # 区分同一个 ou_/cli_ 是哪个 app 视角看到的。
        return self._app_id

    def __init__(
        self,
        app_id: str = "",
        app_secret: str = "",
        *,
        domain: str = "",
    ) -> None:
        self._app_id = app_id or os.getenv("FEISHU_APP_ID", "")
        self._app_secret = app_secret or os.getenv("FEISHU_APP_SECRET", "")
        if not self._app_id:
            raise ValueError("FeishuAdapter requires app_id (or FEISHU_APP_ID env var)")
        self._domain = domain or FEISHU_DOMAIN
        self._ws: WSClient | None = None
        self._http: lark.Client | None = None
        self._handlers: list[MessageHandler] = []
        self._bot_name: str = ""
        # 群消息 buffer（30s 合并窗口 + per-instance offset）。
        # lazy 创建:第一次 on_message 注册 handler 时初始化;非群消息不走 buffer。
        self._group_buffer = None
        # tenant_access_token 缓存（reaction API 用）；5 分钟内可复用。
        self._token_cache: tuple[str, float] | None = None
        self._token_ttl = 1500.0  # 25 分钟（飞书默认 2 小时,留余量）

    # -- IngressAdapter impl -------------------------------------------------

    async def start(self) -> None:
        self._http = self._build_http_client()
        self._ws = self._build_ws_client()

        def _run_ws() -> None:
            _per_thread_loops[threading.get_ident()] = asyncio.new_event_loop()
            asyncio.set_event_loop(_per_thread_loops[threading.get_ident()])
            self._ws.start()

        ws_thread = threading.Thread(
            name=f"feishu-ws-{self._app_id[:8]}",
            target=_run_ws,
            daemon=True,
        )
        ws_thread.start()
        self._bot_name = self._fetch_bot_name()
        logger.info("FeishuAdapter started (app_id=%s bot_name=%s)", self._app_id[:12], self._bot_name)

        # 群名 cache：chat_id → (name, fetch_ts)；5min TTL
        self._chat_name_cache: dict[str, tuple[str, float]] = {}
        self._chat_name_cache_ttl = 300.0

    async def stop(self) -> None:
        logger.info("FeishuAdapter stopping...")
        # 清掉 group_buffer 里残留的最后一批,避免消息丢失
        # (stop 现在是 sync:daemon thread + asyncio.run flush, 不 await)
        if self._group_buffer is not None:
            try:
                self._group_buffer.stop()
            except Exception as exc:
                logger.warning("group_buffer stop failed: %s", exc)

    def _fetch_bot_name(self) -> str:
        try:
            import httpx
            with httpx.Client(timeout=5) as c:
                tr = c.post(
                    "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                    json={"app_id": self._app_id, "app_secret": self._app_secret},
                )
                token = tr.json().get("tenant_access_token", "")
            if not token:
                return ""
            with httpx.Client(timeout=5) as c:
                r = c.get(
                    "https://open.feishu.cn/open-apis/bot/v3/info",
                    headers={"Authorization": f"Bearer {token}"},
                )
                bot = (r.json().get("bot") or {})
                return (bot.get("app_name") or "").strip()
        except Exception:
            return ""

    def _fetch_chat_name(self, chat_id: str) -> str:
        """同步拉群名，5min cache。失败返回空字符串。"""
        if not chat_id:
            return ""
        import time
        now = time.time()
        cached = self._chat_name_cache.get(chat_id)
        if cached and (now - cached[1] < self._chat_name_cache_ttl):
            return cached[0]
        name = ""
        try:
            import httpx
            # token 与 send 路径一致 —— 同步阻塞拉（5s 超时）
            with httpx.Client(timeout=5.0) as c:
                tr = c.post(
                    "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                    json={"app_id": self._app_id, "app_secret": self._app_secret},
                )
                token = tr.json().get("tenant_access_token", "")
                if token:
                    cr = c.get(
                        f"https://open.feishu.cn/open-apis/im/v1/chats/{chat_id}",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    data = cr.json() or {}
                    if data.get("code") == 0:
                        info = data.get("data") or {}
                        name = (info.get("name") or "").strip()
        except Exception as exc:
            logger.debug("fetch chat_name failed: %s", exc)
            name = ""
        self._chat_name_cache[chat_id] = (name, now)
        # Phase 4:不再登记到 chat_groups 缓存表——广播 endpoint 直接带 display_name,
        # 接收方写 messages.db 时存 sender_name,不需要任何共享群名表(去中心化消息
        # 总线设计决策 3)。handler 调 _fetch_chat_name 时也只想拿到本地缓存给
        # wake_prompt 用,不跨进程共享。
        return name
        return name

    async def send(self, chat_id: str, content: str, reply_to: str = "") -> bool:
        if not self._http:
            return False
        # 超长文本分段发送（飞书 text 消息上限由 capabilities.max_text_length 声明）。
        # 详见 interfaces/ingress/text_segmenter.py。
        from interfaces.ingress.text_segmenter import split_text_for_send

        segments = split_text_for_send(content, self.capabilities.max_text_length)
        sent_count = 0
        try:
            for idx, seg in enumerate(segments):
                # reply_to 仅作用于首段（飞书 reply 语义绑单条消息）
                rt = reply_to if idx == 0 else ""
                ok = await self._post_one(chat_id, seg, rt)
                if ok:
                    sent_count += 1
                else:
                    logger.warning("Feishu send segment %d/%d failed", idx + 1, len(segments))
                # 段间适度等待，避免触发飞书发消息 QPS 限制
                if idx < len(segments) - 1:
                    import asyncio

                    await asyncio.sleep(0.2)
            if sent_count == len(segments):
                return True
            logger.warning(
                "Feishu send partial: %d/%d segments delivered", sent_count, len(segments),
            )
            return False
        except Exception as exc:
            logger.warning("Feishu send error: %s", exc)
            return False

    async def _post_one(self, chat_id: str, content: str, reply_to: str) -> bool:
        """发送单段文本。返回是否成功。"""
        if not self._http:
            return False
        try:
            if reply_to:
                req = (
                    lark.im.v1.message.ReplyMessageRequest.builder()
                    .message_id(reply_to)
                    .data(
                        lark.im.v1.message.ReplyMessageRequestBody(
                            msg_type="text",
                            content=content,
                        )
                    )
                )
            else:
                import json as _json

                body = {"text": content}
                req = (
                    lark.im.v1.message.CreateMessageRequest.builder()
                    .receive_id_type("chat_id")
                    .receive_id(chat_id)
                    .msg_type("text")
                    .content(_json.dumps(body, ensure_ascii=False))
                )

            def _do_send():
                try:
                    resp = self._http.im.v1.message.create(req)
                    return resp.code == 0
                except Exception as exc:
                    logger.warning("Feishu send failed: %s", exc)
                    return False

            return await asyncio.to_thread(_do_send)
        except Exception as exc:
            logger.warning("Feishu send one error: %s", exc)
            return False

    # ── Reaction API(三态收条:👀 收到 → ⚙️ 思考中 → 发送后撤回) ──
    # 设计动机:用户反馈循环 —— 真人发消息后能即时感知"被收到 / 在处理",
    # 而不是等几十秒后才看到 bot 出声。类似 Hermes / Slack 收条机制。

    def _get_token(self) -> str:
        """获取 tenant_access_token(带 25 分钟缓存)。"""
        import time as _time
        now = _time.time()
        if self._token_cache and (now - self._token_cache[1]) < self._token_ttl:
            return self._token_cache[0]
        try:
            import httpx
            with httpx.Client(timeout=5.0) as c:
                tr = c.post(
                    f"{self._domain}/open-apis/auth/v3/tenant_access_token/internal",
                    json={"app_id": self._app_id, "app_secret": self._app_secret},
                )
                token = tr.json().get("tenant_access_token", "")
                if token:
                    self._token_cache = (token, now)
                    return token
        except Exception as exc:
            logger.debug("_get_token failed: %s", exc)
        return ""

    async def add_reaction(self, msg_id: str, emoji: str) -> str:
        """给消息加 emoji 表情回应。返 reaction_id(用于后续 remove),失败返 ""。

        emoji 用飞书 emoji_type 字符串,如 "EYES"(👀) / "SETTINGS"(⚙️) / "THUMBSUP"(👍)。
        完整 list: https://open.feishu.cn/document/ukTMukTMukTM/uAjNyYjL4YTMxkDOyYjN
        """
        if not msg_id:
            return ""
        try:
            import httpx
            import json as _json
            token = self._get_token()
            if not token:
                return ""

            def _do():
                with httpx.Client(timeout=5.0) as c:
                    r = c.post(
                        f"{self._domain}/open-apis/im/v1/messages/{msg_id}/reactions",
                        headers={"Authorization": f"Bearer {token}",
                                 "Content-Type": "application/json"},
                        data=_json.dumps({"reaction_type": {"emoji_type": emoji}}),
                    )
                    data = r.json() or {}
                    if data.get("code") == 0:
                        return (data.get("data") or {}).get("reaction_id", "")
                    logger.debug("add_reaction failed: %s", data)
                    return ""
            return await asyncio.to_thread(_do)
        except Exception as exc:
            logger.debug("add_reaction error: %s", exc)
            return ""

    def _download_feishu_resource(
        self, message_id: str, file_key: str, resource_type: str,
    ) -> tuple[bytes, str]:
        """下载飞书消息资源（图片 / 文件 / 音频），同步返回 (bytes, mime)。

        Endpoint: GET /open-apis/im/v1/messages/{message_id}/resources/{file_key}?type=image|file
        这是 CowAgent 等开源飞书机器人通用方案——支持图片 + 文件 + 音频 + 视频。

        Args:
            message_id: 飞书消息 ID（om_xxxxxxx），从 event.message.message_id 拿
            file_key: 资源 ID（image 类型为 image_key，file/audio 为 file_key）
            resource_type: "image" 或 "file"（飞书 docs 原话）

        Returns:
            (bytes_content, mime_type) —— mime 从 response Content-Type header 读，
            未给时按 resource_type 猜（image→image/png, file→application/octet-stream）。

        Raises:
            Exception on network/HTTP error. 调用方 catch 并 fallback 处理。
        """
        import httpx
        token = self._get_token()
        if not token:
            raise RuntimeError("no tenant_access_token")
        url = (
            f"{self._domain}/open-apis/im/v1/messages/{message_id}"
            f"/resources/{file_key}?type={resource_type}"
        )
        with httpx.Client(timeout=30.0) as c:  # 图片下载给 30s，远比 LLM 调用短
            r = c.get(url, headers={"Authorization": f"Bearer {token}"})
            r.raise_for_status()
            mime = (r.headers.get("Content-Type") or "").split(";")[0].strip()
            if not mime:
                mime = "image/png" if resource_type == "image" else "application/octet-stream"
            return (r.content, mime)

    async def remove_reaction(self, msg_id: str, reaction_id: str) -> bool:
        """删除一个 reaction(基于 reaction_id)。"""
        if not msg_id or not reaction_id:
            return False
        try:
            import httpx
            token = self._get_token()
            if not token:
                return False

            def _do():
                with httpx.Client(timeout=5.0) as c:
                    r = c.delete(
                        f"{self._domain}/open-apis/im/v1/messages/{msg_id}/reactions/{reaction_id}",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    data = r.json() or {}
                    return data.get("code") == 0
            return await asyncio.to_thread(_do)
        except Exception as exc:
            logger.debug("remove_reaction error: %s", exc)
            return False

    def on_message(self, handler: MessageHandler) -> None:
        self._handlers.append(handler)

    def _ensure_group_buffer(self) -> None:
        """懒创建群消息 buffer。flush 在 daemon thread 跑独立 loop。"""
        if self._group_buffer is not None:
            return
        from interfaces.ingress.group_buffer import GroupMessageBuffer

        async def _dispatch_all(msg):
            for h in self._handlers:
                try:
                    await h(msg)
                except Exception as exc:
                    logger.exception("group_buffer dispatch handler error: %s", exc)

        def _is_priority(msg) -> bool:
            """快通道判定:@  bot 或 正文含 attention_keywords。

            只对群聊生效(私聊不进 buffer)。判定逻辑跟 handler.py
            _classify_group_urgency 一致,但 buffer 这一层提前算。
            """
            if getattr(msg, "mentions_bot", False):
                return True
            text = (getattr(msg, "content", "") or "").lower()
            if not text:
                return False
            try:
                from application.ingress.handler import _load_group_chat_config
                cfg = _load_group_chat_config() or {}
                for kw in (cfg.get("attention_keywords") or []):
                    if kw and str(kw).lower() in text:
                        return True
            except Exception:
                pass
            return False

        self._group_buffer = GroupMessageBuffer(_dispatch_all, is_priority=_is_priority)

    # -- Internals -----------------------------------------------------------

    def _build_http_client(self) -> lark.Client:
        return (
            lark.Client.builder()
            .app_id(self._app_id)
            .app_secret(self._app_secret)
            .domain(self._domain)
            .build()
        )

    def _build_ws_client(self) -> WSClient:
        def _on_message(
            event: lark.api.im.v1.model.p2_im_message_receive_v1.P2ImMessageReceiveV1,
        ) -> None:
            try:
                msg = getattr(event.event, "message", None)
                mentions = getattr(msg, "mentions", None) or []
                # 调试日志：dump mentions 的 raw 数据（field/值）
                if mentions:
                    debug_lines = []
                    for m in mentions:
                        try:
                            debug_lines.append(
                                "id={!r} id_type={!r} name={!r} key={!r}".format(
                                    getattr(m, "id", None),
                                    getattr(m, "id_type", None),
                                    getattr(m, "name", None),
                                    getattr(m, "key", None),
                                )
                            )
                        except Exception:
                            debug_lines.append(f"<unreadable mention: {m!r}>")
                    logger.info("Mention raw dump: %s", " | ".join(debug_lines))
                logger.info(
                    "Feishu WS: chat=%s msg_id=%s mentions=%d bot=%s",
                    getattr(msg, "chat_id", "") or "",
                    getattr(msg, "message_id", "") or "",
                    len(mentions),
                    self._app_id[:12],
                )
                normalized = self._normalize(event)
                # 群消息走 30s 合并窗口 + per-instance offset（防多 bot 同步竞争 / 群消息交错）。
                # 私聊立即下发（用户等不起）。
                # 设计:
                #   - 群消息入 self._group_buffer (30s flush + offset);flush 时聚成单条 NormalizedMessage
                #     (只带 merged_texts 列表),一次调 handler —— 取代事件层 group_message 的 30s debounce
                #   - 这是消息系统问题,事件层只调度不再合并
                if getattr(normalized, "is_group", False):
                    self._ensure_group_buffer()
                    self._group_buffer.add(normalized)
                else:
                    for handler in self._handlers:
                        try:
                            loop = asyncio.get_running_loop()
                            loop.call_soon(
                                lambda h=handler, n=normalized: asyncio.create_task(h(n))
                            )
                        except RuntimeError:
                            asyncio.run(handler(normalized))
            except Exception as exc:
                logger.exception("Feishu _on_message error: %s", exc)

        handler_builder = (
            EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(_on_message)
            .build()
        )

        return WSClient(
            app_id=self._app_id,
            app_secret=self._app_secret,
            log_level=lark.LogLevel.DEBUG if logger.isEnabledFor(logging.DEBUG) else lark.LogLevel.INFO,
            event_handler=handler_builder,
            domain=self._domain,
            auto_reconnect=True,
        )

    def _normalize(self, event: Any) -> NormalizedMessage:
        """Convert a Lark P2ImMessageReceiveV1 event to NormalizedMessage.

        **多模态支持**：image/post(file) file/audio 各按 msg_type 分支处理——
        下载资源字节到本地 attachments/，register 进 attachment registry，
        把 attachment_id 摘要挂到 NormalizedMessage.attachments。
        text content 字段给出可读占位（" [图片 xxx]" / "[文件 xxx]"）让模型看到。
        """
        msg = event.event.message
        sender = event.event.sender

        chat_id = getattr(msg, "chat_id", "") or ""
        chat_type = getattr(msg, "chat_type", "p2p") or "p2p"
        message_id = getattr(msg, "message_id", "") or f"in_{uuid4().hex}"
        msg_type = (getattr(msg, "message_type", "text") or "text").strip().lower()
        raw_content = getattr(msg, "content", "") or ""

        # ── 按 msg_type 解析 text + attachments ────────────────────────────────
        # image: content={"image_key":"img_v3_xxx"}
        # file:  content={"file_key":"file_v3_xxx","file_name":"..."}
        # audio: content={"file_key":"file_v3_xxx","duration_ms":...}
        # post: 富文本，content={"title":"...","content":[[{tag:"text",text},{tag:"img",image_key}]]}
        # text: content={"text":"hello"}（不加 @/at 未提及）
        text_parts: list[str] = []
        attachments: list[dict] = []  # 元素轻量摘要 {attachment_id, source, source_key, mime, kind}

        def _ingest_resource(source_key: str, resource_type: str, kind: str,
                             fallback_mime: str = "") -> str | None:
            """把一个飞书资源（image_key/file_key）拉下来 + register attachment。

            返回 attachment_id 或 None（失败时不抛，返回 None 让调用方决定占位文案）。
            """
            if not source_key or not message_id or message_id.startswith("in_"):
                return None
            try:
                data, mime = self._download_feishu_resource(
                    message_id, source_key, resource_type,
                )
            except Exception as exc:
                logger.warning(
                    "FeishuAdapter: download %s=%s failed (msg=%s): %s",
                    resource_type, source_key, message_id, exc,
                )
                return None
            if not data:
                return None
            try:
                from infrastructure.config import get_app_instance_id
                from infrastructure.persistence.instance.attachments import (
                    save_bytes_as_attachment,
                )
                iid = get_app_instance_id() or ""
                if not iid:
                    return None
                att = save_bytes_as_attachment(
                    instance_id=iid, source="feishu", source_key=source_key,
                    data=data, mime=mime or fallback_mime or "application/octet-stream",
                )
                attachments.append({
                    "attachment_id": att.attachment_id,
                    "source": "feishu",
                    "source_key": source_key,
                    "mime": att.mime,
                    "kind": att.kind,
                })
                return att.attachment_id
            except Exception as exc:
                logger.warning("FeishuAdapter: register attachment failed: %s", exc)
                return None

        if msg_type == "text":
            text_parts.append(raw_content)
            if raw_content.startswith("{"):
                try:
                    text_parts = [json.loads(raw_content).get("text", raw_content)]
                except (json.JSONDecodeError, TypeError):
                    pass
        elif msg_type == "image":
            image_key = ""
            if raw_content.startswith("{"):
                try:
                    image_key = (json.loads(raw_content).get("image_key") or "").strip()
                except (json.JSONDecodeError, TypeError):
                    pass
            att_id = _ingest_resource(image_key, "image", "image", fallback_mime="image/png")
            text_parts.append(f"[图片 {att_id or image_key}]" if att_id else f"[图片 {image_key}]")
        elif msg_type == "post":
            # post 富文本：title + content list of list（每元素 {tag, text, image_key, ...}）
            try:
                post_data = json.loads(raw_content) if raw_content.startswith("{") else {}
            except (json.JSONDecodeError, TypeError):
                post_data = {}
            # zh / en_us 任一即可
            locale_obj = post_data.get("zh") or post_data.get("en_us") or post_data.get("content") or {}
            title = (locale_obj.get("title") or "").strip() if isinstance(locale_obj, dict) else ""
            if title:
                text_parts.append(title)
            content_lines = locale_obj.get("content") or [] if isinstance(locale_obj, dict) else []
            for line in content_lines:
                if not isinstance(line, list):
                    continue
                line_parts: list[str] = []
                for el in line:
                    if not isinstance(el, dict):
                        continue
                    tag = el.get("tag") or el.get("type") or ""
                    if tag == "text":
                        t = (el.get("text") or "").strip()
                        if t:
                            line_parts.append(t)
                    elif tag in ("img", "image"):
                        img_key = (el.get("image_key") or "").strip()
                        att_id = _ingest_resource(img_key, "image", "image", fallback_mime="image/png")
                        placeholder = f"[图片 {att_id or img_key}]" if att_id else f"[图片 {img_key}]"
                        # 图片占位单独一行——便于模型识别"这里需要调 sense_image"
                        line_parts.append(placeholder)
                    elif tag in ("at",):
                        # post 里的 @：保留占位（mention 解析靠 event.mentions 字段，下面统一处理）
                        pass
                    # 其它 tag（a / media 等）暂不处理
                if line_parts:
                    text_parts.append("".join(line_parts))
            # 如果 post 完全没解出内容，fallback 到 raw_content 显示给模型看
            if not text_parts:
                text_parts.append(raw_content if raw_content else "[空富文本]")
        elif msg_type == "file":
            file_key = ""
            file_name = ""
            try:
                fv = json.loads(raw_content) if raw_content.startswith("{") else {}
                file_key = (fv.get("file_key") or "").strip()
                file_name = (fv.get("file_name") or "").strip()
            except (json.JSONDecodeError, TypeError):
                pass
            att_id = _ingest_resource(file_key, "file", "file")
            text_parts.append(f"[文件 {file_name or att_id or file_key}]")
        elif msg_type == "audio":
            file_key = ""
            duration_ms = 0
            try:
                av = json.loads(raw_content) if raw_content.startswith("{") else {}
                file_key = (av.get("file_key") or "").strip()
                duration_ms = int(av.get("duration_ms") or 0)
            except (json.JSONDecodeError, TypeError):
                pass
            att_id = _ingest_resource(file_key, "file", "audio", fallback_mime="audio/opus")
            sec = round(duration_ms / 1000, 1) if duration_ms else 0
            text_parts.append(f"[语音 {sec}s]" if sec else "[语音]")
        else:
            # 其它/未知（如 sticker, share_chat, interactive 等）：fallback 占位
            text_parts.append(f"[不支持的消息类型 {msg_type}]")

        # text 兜底：从未解出内容时退回 raw_content
        text = "".join(text_parts).strip() if text_parts else (raw_content or "")
        # 空白文本兜底——避免空消息被 handler 静默丢弃（如纯表情图，没 attachment 时）
        if not text and attachments:
            text = "[附件消息]"

        sender_open_id = ""
        sender_obj = getattr(sender, "sender_id", None)
        if sender_obj:
            sender_open_id = getattr(sender_obj, "open_id", "") or ""
            if not sender_open_id:
                sender_open_id = getattr(sender_obj, "user_id", "") or ""
            if not sender_open_id:
                sender_open_id = getattr(sender_obj, "app_id", "") or ""

        # 发送者是不是机器人：飞书 SDK 的 sender.sender_type 为 "app" 表示 app/机器人发的
        # （"user" 为真人）。用于把 sibling/其它机器人标记为 kind='bot' 联系人，
        # 以及发送侧判断"@ 到了机器人→飞书会送达它→本侧不再广播"。
        _sender_type = (getattr(sender, "sender_type", "") or "").strip().lower()
        sender_is_bot = _sender_type == "app"

        # 解析 @ 列表（仅群聊）
        # Lark Mention 字段：key(可能是占位符 '@_user_1')、id(UserId|str)、id_type、name
        # text 中的 '@_user_N' 是飞书脱敏占位符，无法可靠恢复，
        # 所以保留原文 + 在 mention_mentions 字段提供 "key → name" 映射让下游 prompt 解释
        mentioned_ids: list[str] = []
        mentioned_names: list[str] = []
        mention_map: list[str] = []       # ["@_user_1 → alpha", "@_user_2 → 张三"]
        is_group = chat_type == "group"
        if is_group:
            mentions = getattr(msg, "mentions", None) or []
            for m in mentions:
                try:
                    mname = (getattr(m, "name", "") or "").strip()
                    mkey = (getattr(m, "key", "") or "").strip()
                    mid_obj = getattr(m, "id", None)
                    mid_str = ""
                    if isinstance(mid_obj, str):
                        mid_str = mid_obj.strip()
                    elif mid_obj is not None:
                        for attr in ("open_id", "union_id", "user_id", "app_id"):
                            v = getattr(mid_obj, attr, None)
                            if v:
                                mid_str = str(v).strip()
                                break
                    if mname:
                        mentioned_names.append(mname)
                    if mid_str:
                        mentioned_ids.append(mid_str)
                    elif mname:
                        mentioned_ids.append(f"name:{mname}")
                    if mkey and mname:
                        mention_map.append(f"{mkey}→{mname}")
                except Exception:
                    pass

        # 把 text 中的 @_user_N 占位符替换为实际名字，让模型直接看可读文本
        # mention.name 是飞书给的可读名（用户名/bot 应用名），不可伪造
        if is_group and mention_map:
            for entry in mention_map:
                if "→" not in entry:
                    continue
                key, name = entry.split("→", 1)
                key = key.strip()
                name = name.strip()
                if key and name and key in text:
                    text = text.replace(key, f"@{name}")

        # 判断当前 adapter 的 bot 是否被 @：
        #   - mention.id 直接是 app_id（少见）
        #   - mention.name == bot 的应用名（legacy_name / display_name）
        #   - mention.name == bot 应用后台的中文名（如 "数字生命 alpha"）
        bot_names_match = False
        if self._bot_name:
            bn_lower = self._bot_name.lower().strip()
            for mname in mentioned_names:
                mn = (mname or "").lower().strip()
                if mn and (mn == bn_lower or mn in bn_lower or bn_lower in mn):
                    bot_names_match = True
                    break
        if not bot_names_match:
            try:
                from infrastructure.config import _load_registry
                registry = _load_registry() or {}
                from pathlib import Path as _P
                import yaml as _yaml
                from infrastructure.config import get_project_root
                for iid, meta in registry.items():
                    cfg_path = get_project_root() / "apps" / iid / "config" / "app.yaml"
                    if not cfg_path.exists():
                        continue
                    try:
                        cfg = _yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
                    except Exception:
                        continue
                    ch = (cfg.get("channels") or {}).get("feishu") or {}
                    if (ch.get("app_id") or "").strip() != self._app_id:
                        continue
                    legacy = (meta.get("legacy_name") or "").lower()
                    display = (meta.get("display_name") or "").lower()
                    for mname in mentioned_names:
                        mn = (mname or "").lower().strip()
                        if not mn:
                            continue
                        if mn == legacy or mn == display or mn in legacy or mn in display:
                            bot_names_match = True
                            break
                    break
            except Exception:
                pass
        mentions_bot_self = (self._app_id in mentioned_ids) or bot_names_match

        # 查 contacts 表补 sender_name（platform+platform_id → name 映射）
        # 未命中时显示友好短码（ou_xxx 前 12 位），避免 prompt 显示完整 open_id 太丑
        sender_name_resolved = ""
        if sender_open_id:
            try:
                from domain.contacts import lookup_name
                sender_name_resolved = lookup_name("feishu", sender_open_id)
            except Exception:
                sender_name_resolved = ""
            if not sender_name_resolved:
                # 短码 fallback：ou_eb5083eb... → "用户 eb5083eb"
                short = sender_open_id[3:11] if len(sender_open_id) > 11 else sender_open_id
                sender_name_resolved = f"用户{short}" if short else sender_open_id

        # chat_name 飞书不在事件 payload 里，调 im/v1/chats/{chat_id} 拉取
        # 带 5min cache 避免 API 调用过密；失败时为空，模板渲染显示 chat_id 不丑
        chat_name_resolved = self._fetch_chat_name(chat_id) if is_group and chat_id else ""

        return NormalizedMessage(
            platform="feishu",
            chat_id=chat_id,
            message_id=message_id,
            sender_id=sender_open_id,
            content=text,
            sender_name=sender_name_resolved,
            chat_name=chat_name_resolved,
            is_group=is_group,
            mentions_bot=mentions_bot_self,
            mentioned_bot_app_ids=mentioned_ids,
            mention_names=mentioned_names,
            mention_map=mention_map,
            sender_is_bot=sender_is_bot,
            attachments=attachments,
            raw=event,
        )
