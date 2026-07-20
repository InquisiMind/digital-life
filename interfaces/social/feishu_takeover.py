"""飞书社交接管模块 — 以 user_access_token 身份拉取 zhp 的消息。

独立 daemon thread, 每 POLL_INTERVAL 秒轮询:
  1. 拉所有群 (GET /im/v1/chats)
  2. 逐群拉最新消息 (GET /im/v1/messages?container_id=xxx)
  3. 去重后入库 (social_feed 表)
  4. 含命令 + zero bot 不在该群 → emit social_command 事件
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

POLL_INTERVAL = 1800  # 30 分钟拉一轮(只是入库, 不等于触发)
FEISHU_BASE = "https://open.feishu.cn/open-apis"


def _get_app_creds() -> tuple[str, str]:
    """从环境变量读飞书 app_id + app_secret。"""
    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    if not app_id or not app_secret:
        # fallback: 从实例 app.yaml 读
        try:
            import yaml
            from infrastructure.config import get_instance_app_config_path
            cfg_path = get_instance_app_config_path()
            if cfg_path.exists():
                raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
                feishu = (raw.get("channels") or {}).get("feishu") or {}
                app_id = app_id or feishu.get("app_id", "")
        except Exception:
            pass
    return app_id, app_secret


def _get_refresh_token(instance_id: str) -> str:
    """从 social.env 读 user refresh_token。"""
    from pathlib import Path
    social_env = Path("apps") / instance_id / "config" / "social.env"
    if not social_env.exists():
        return ""
    for line in social_env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("FEISHU_USER_REFRESH_TOKEN="):
            return line.split("=", 1)[1].strip()
    return ""


def _save_tokens(instance_id: str, access_token: str, refresh_token: str) -> None:
    """持久化 user tokens 到 social.env。"""
    from pathlib import Path
    social_env = Path("apps") / instance_id / "config" / "social.env"
    social_env.parent.mkdir(parents=True, exist_ok=True)
    social_env.write_text(
        f"FEISHU_USER_ACCESS_TOKEN={access_token}\n"
        f"FEISHU_USER_REFRESH_TOKEN={refresh_token}\n",
        encoding="utf-8",
    )


class FeishuSocialTakeover:
    """飞书社交接管 — 轮询 zhp 的消息并入库/触发事件。"""

    def __init__(self, instance_id: str):
        self.instance_id = instance_id
        self._user_token: str = ""
        self._refresh_token: str = ""
        self._token_expires: float = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # 已知的群列表缓存 {chat_id: chat_name}
        self._chats: dict[str, str] = {}
        # 已见过的 message_id 集合(内存去重, DB 也有 UNIQUE)
        self._seen_ids: set[str] = set()
        # 飞书 domain
        app_id, _ = _get_app_creds()
        self._app_id = app_id
        self._base = FEISHU_BASE

    def _refresh_user_token(self) -> bool:
        """用 refresh_token 刷新 user_access_token。"""
        app_id, app_secret = _get_app_creds()
        if not app_id or not app_secret:
            logger.debug("social_takeover: no app creds")
            return False
        if not self._refresh_token:
            self._refresh_token = _get_refresh_token(self.instance_id)
            if not self._refresh_token:
                logger.debug("social_takeover: no refresh_token — OAuth not done yet")
                return False
        try:
            resp = httpx.post(
                f"{self._base}/authen/v1/refresh_access_token",
                headers={"Content-Type": "application/json"},
                json={
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                    "app_id": app_id,
                    "app_secret": app_secret,
                },
                timeout=10,
            )
            data = resp.json()
            if data.get("code") != 0:
                logger.warning("social_takeover: refresh failed: %s", data.get("msg", ""))
                return False
            token_data = data.get("data") or {}
            self._user_token = token_data.get("access_token", "")
            new_refresh = token_data.get("refresh_token", "")
            if new_refresh:
                self._refresh_token = new_refresh
            expires_in = token_data.get("token", {}).get("expires_in", 7200)
            self._token_expires = time.time() + expires_in - 60
            _save_tokens(self.instance_id, self._user_token, self._refresh_token)
            logger.info("social_takeover: token refreshed, expires in %ds", expires_in)
            return True
        except Exception as exc:
            logger.warning("social_takeover: refresh exception: %s", exc)
            return False

    def _ensure_token(self) -> bool:
        if self._user_token and time.time() < self._token_expires:
            return True
        return self._refresh_user_token()

    def _api_get(self, path: str, params: dict | None = None) -> dict | None:
        """以 user 身份调飞书 API。"""
        if not self._ensure_token():
            return None
        try:
            resp = httpx.get(
                f"{self._base}{path}",
                headers={"Authorization": f"Bearer {self._user_token}"},
                params=params or {},
                timeout=15,
            )
            data = resp.json()
            if data.get("code") != 0:
                logger.debug("social_takeover API %s failed: code=%s msg=%s",
                             path, data.get("code"), data.get("msg", ""))
                return None
            return data.get("data") or {}
        except Exception as exc:
            logger.debug("social_takeover API %s exception: %s", path, exc)
            return None

    def _list_chats(self) -> dict[str, str]:
        """列出 zhp 参与的所有群。返回 {chat_id: chat_name}。"""
        chats: dict[str, str] = {}
        page_token = None
        for _ in range(10):  # 最多翻 10 页
            params: dict[str, Any] = {"page_size": 100, "user_id_type": "open_id"}
            if page_token:
                params["page_token"] = page_token
            data = self._api_get("/im/v1/chats", params)
            if not data:
                break
            for item in data.get("items", []):
                chat_id = item.get("chat_id", "")
                chat_name = item.get("name", "")
                if chat_id:
                    chats[chat_id] = chat_name
            page_token = data.get("page_token")
            if not data.get("has_more"):
                break
        return chats

    def _fetch_messages(self, chat_id: str, chat_name: str) -> list[dict]:
        """拉某个群的最近消息。返回标准化的消息列表。"""
        data = self._api_get(
            "/im/v1/messages",
            {
                "container_id_type": "chat",
                "container_id": chat_id,
                "page_size": 20,
                "sort_type": "ByCreateTimeDesc",
            },
        )
        if not data:
            return []
        messages: list[dict] = []
        for item in data.get("items", []):
            msg_id = item.get("message_id", "")
            if not msg_id or msg_id in self._seen_ids:
                continue
            # 解析 body (飞书消息 body 是 JSON string)
            body_raw = item.get("body", {}).get("content", "{}")
            try:
                import json
                body = json.loads(body_raw) if isinstance(body_raw, str) else body_raw
                text = body.get("text", "") or body.get("content", "")
            except Exception:
                text = str(body_raw)[:200]
            # 解析 sender
            sender = item.get("sender", {})
            sender_id_raw = sender.get("id", "")
            # sender.id 是完整 open_id, 截短
            sender_id = sender_id_raw
            # 飞书 sender_id 在 sender 对象里, name 不一定有
            sender_name = ""  # 需要额外调 API 才知道名字, 先空着
            msg_type = item.get("msg_type", "text")
            if msg_type == "text" and text:
                messages.append({
                    "message_id": msg_id,
                    "chat_id": chat_id,
                    "chat_name": chat_name,
                    "sender_id": sender_id,
                    "sender_name": sender_name,
                    "text": text.strip()[:500],
                    "message_ts": float(item.get("create_time", "0") or "0"),
                })
        return messages

    def _emit_command(self, msg: dict) -> None:
        """含命令的消息 → emit social_command 事件(触发 wake)。"""
        try:
            from domain.lifecycle.runtime_context import set_current_instance_id
            from domain.lifecycle.events import emit_event
            set_current_instance_id(self.instance_id)
            emit_event(
                kind="social_command",
                payload={
                    "text": msg["text"],
                    "chat_name": msg.get("chat_name", ""),
                    "chat_id": msg.get("chat_id", ""),
                    "sender_name": msg.get("sender_name", "zhp"),
                    "source": "feishu_user",
                },
                channel=f"instance:{self.instance_id}",
            )
            logger.info("social_takeover: emitted social_command from %s",
                         msg.get("chat_name", "?"))
        except Exception as exc:
            logger.warning("social_takeover: emit_command failed: %s", exc)

    def _tick(self) -> None:
        """一次轮询:拉群 → 拉消息 → 入库 → 检测命令。"""
        if not self._ensure_token():
            return
        # 1. 刷新群列表(每 5 分钟刷一次)
        if not self._chats or time.time() % 300 < POLL_INTERVAL:
            self._chats = self._list_chats()
            if not self._chats:
                return
        # 2. 逐群拉消息
        from domain.social.store import insert_message, has_command
        for chat_id, chat_name in self._chats.items():
            msgs = self._fetch_messages(chat_id, chat_name)
            for msg in msgs:
                self._seen_ids.add(msg["message_id"])
                is_new = insert_message(
                    source="feishu",
                    chat_id=msg["chat_id"],
                    chat_name=msg["chat_name"],
                    message_id=msg["message_id"],
                    sender_name=msg["sender_name"],
                    sender_id=msg["sender_id"],
                    text=msg["text"],
                    message_ts=msg["message_ts"],
                    instance_id=self.instance_id,
                )
                if is_new and has_command(msg["text"]):
                    self._emit_command(msg)
            # 控制频率, 避免飞书 rate limit
            time.sleep(0.5)
        # 3. 清理 _seen_ids (保留最近 5000 条)
        if len(self._seen_ids) > 10000:
            self._seen_ids = set(list(self._seen_ids)[-5000:])

    def _loop(self) -> None:
        """daemon loop。"""
        logger.info("social_takeover: loop started for instance %s", self.instance_id[:8])
        from infrastructure.config import set_current_instance_id
        set_current_instance_id(self.instance_id)
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:
                logger.warning("social_takeover tick error: %s", exc)
            self._stop.wait(POLL_INTERVAL)
        logger.info("social_takeover: loop stopped for instance %s", self.instance_id[:8])

    def start(self) -> None:
        """启动 daemon thread。"""
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name=f"social_takeover_{self.instance_id[:8]}",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)


def start_takeover_daemon(instance_id: str) -> FeishuSocialTakeover | None:
    """启动一个实例的社交接管 daemon。返回 takeover 对象(可用于 stop)。

    前提: apps/{iid}/config/social.env 里要有 FEISHU_USER_REFRESH_TOKEN
    (由 OAuth 授权后在 callback handler 里写入)。
    没有 token → 不启动(静默跳过)。
    """
    refresh = _get_refresh_token(instance_id)
    if not refresh:
        logger.debug("social_takeover: no refresh_token for %s, skipping", instance_id[:8])
        return None
    takeover = FeishuSocialTakeover(instance_id)
    takeover.start()
    return takeover
