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


def _extract_msg_text(body: Any, msg_type: str) -> str:
    """从消息 body 提取可读文本。支持 text/share/post。

    - text: body.text
    - share (文档卡片): body.share.{title,url} → "[文档] 标题 URL"
    - post (富文本): 遍历 zh_cn/en_us 的 content, 提取 text/a 标签, a 标签带 href
    其他类型: fallback 到 body.content 或 body.text
    """
    if not isinstance(body, dict):
        return str(body or "")
    if msg_type == "text":
        return body.get("text", "") or body.get("content", "")
    if msg_type == "share":
        share = body.get("share") or {}
        title = share.get("title", "")
        url = share.get("url", "")
        # type: doc/sheet/bitable/folder...
        kind = share.get("type", "")
        prefix = f"[文档分享:{kind}]" if kind else "[文档分享]"
        return f"{prefix} {title} {url}".strip()
    if msg_type == "post":
        # post 按 locale 分组 (zh_cn / en_us / ...), 取第一个 locale
        parts: list[str] = []
        for locale_key in ("zh_cn", "en_us", "en"):
            locale = body.get(locale_key)
            if locale:
                title = locale.get("title", "")
                if title:
                    parts.append(title)
                for paragraph in locale.get("content", []):
                    if not isinstance(paragraph, list):
                        continue
                    for tag in paragraph:
                        if not isinstance(tag, dict):
                            continue
                        t = tag.get("tag")
                        if t == "text":
                            parts.append(tag.get("text", ""))
                        elif t == "a":
                            parts.append(f"{tag.get('text', '')}({tag.get('href', '')})")
                        elif t == "at":
                            parts.append(f"@{tag.get('user_name', tag.get('user_id', ''))}")
                break  # 只取一个 locale
        return " ".join(p for p in parts if p)
    # 其他类型 (image/file/interactive...) fallback
    return body.get("text", "") or body.get("content", "") or ""


def _get_app_creds() -> tuple[str, str]:
    """从环境变量 → 实例 secrets.env → 实例 app.yaml 读飞书 app_id + app_secret。

    ContextVar instance_id 必须已设置。读顺序:
      1. 进程 env (master 设置的全局默认)
      2. apps/{iid}/config/secrets.env (secret 类字段)
      3. apps/{iid}/config/app.yaml (app_id 等非敏感字段)
    """
    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")

    if not app_id or not app_secret:
        # fallback 1: 从实例 secrets.env 读
        try:
            from pathlib import Path
            from infrastructure.config import get_instance_dir
            iid = ""
            try:
                from infrastructure.config import get_app_instance_id
                iid = get_app_instance_id() or ""
            except Exception:
                pass
            if iid:
                secrets = get_instance_dir(iid) / "config" / "secrets.env"
                if secrets.exists():
                    for line in secrets.read_text(encoding="utf-8").splitlines():
                        line = line.strip()
                        if line.startswith("FEISHU_APP_ID=") and not app_id:
                            app_id = line.split("=", 1)[1].strip()
                        elif line.startswith("FEISHU_APP_SECRET=") and not app_secret:
                            app_secret = line.split("=", 1)[1].strip()
        except Exception:
            pass

    if not app_id or not app_secret:
        # fallback 2: 从实例 app.yaml 读 app_id (app_secret 通常在 secrets.env)
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
        # 已知的群列表缓存 {chat_id: {name, type}}
        self._chats: dict[str, dict] = {}
        # 已见过的 message_id 集合(内存去重, DB 也有 UNIQUE)
        self._seen_ids: set[str] = set()
        # sender name 缓存: open_id → name 避免每条消息都查 contact API
        self._name_cache: dict[str, str] = {}
        # 自己的 open_id (daemon 启动后 _load_self_info 填充, 用于 at_me 检测)
        self._self_open_id: str = ""
        # 飞书 domain
        app_id, _ = _get_app_creds()
        self._app_id = app_id
        self._base = FEISHU_BASE

    def _get_tenant_token(self) -> str:
        """拿 tenant_access_token (app_access_token) — 用 app_id/secret 换。

        OIDC refresh_access_token / access_token 接口要求 Authorization header
        带 tenant_access_token, 而不是 v1 接口的 body 传 app_id/app_secret。
        """
        app_id, app_secret = _get_app_creds()
        if not app_id or not app_secret:
            return ""
        try:
            resp = httpx.post(
                f"{self._base}/auth/v3/tenant_access_token/internal",
                headers={"Content-Type": "application/json"},
                json={"app_id": app_id, "app_secret": app_secret},
                timeout=10,
            )
            return resp.json().get("tenant_access_token", "")
        except Exception:
            return ""

    def _refresh_user_token(self) -> bool:
        """用 refresh_token 刷新 user_access_token。

        Feishu OIDC 接口: /authen/v1/oidc/refresh_access_token
        - Authorization header: Bearer <tenant_access_token>
        - body: {grant_type: refresh_token, refresh_token: <token>}
        - response.data: {access_token, refresh_token, token_type, expires_in, refresh_expires_in}
        """
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
            tenant_tok = self._get_tenant_token()
            if not tenant_tok:
                logger.warning("social_takeover: failed to get tenant_access_token")
                return False
            resp = httpx.post(
                f"{self._base}/authen/v1/oidc/refresh_access_token",
                headers={
                    "Authorization": f"Bearer {tenant_tok}",
                    "Content-Type": "application/json",
                },
                json={
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
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
            if not self._user_token:
                logger.warning("social_takeover: refresh returned empty access_token")
                return False
            if new_refresh:
                self._refresh_token = new_refresh
            expires_in = token_data.get("expires_in", 7200)
            self._token_expires = time.time() + expires_in - 60
            _save_tokens(self.instance_id, self._user_token, self._refresh_token)
            logger.info("social_takeover: token refreshed (OIDC), expires in %ds", expires_in)
            self._load_self_info()
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

    def _resolve_user_name(self, open_id: str) -> str:
        """通过飞书 contact API 解析 open_id → name。带 _name_cache 避免重复请求。

        用 tenant_access_token（应用身份），不是 user_access_token。
        需要应用后台"可用范围"覆盖目标用户（否则 code=41050 no user authority）。

        失败 → 返回空，不影响萹库主体流程。
        """
        if not open_id or open_id in self._name_cache:
            return self._name_cache.get(open_id, "")
        try:
            tenant_tok = self._get_tenant_token()
            if not tenant_tok:
                return ""
            resp = httpx.get(
                f"{self._base}/contact/v3/users/{open_id}",
                headers={"Authorization": f"Bearer {tenant_tok}"},
                params={"user_id_type": "open_id"},
                timeout=8,
            )
            data = resp.json()
            if data.get("code") == 0:
                name = (data.get("data") or {}).get("user", {}).get("name", "")
                if name:
                    self._name_cache[open_id] = name
                    return name
        except Exception:
            pass
        return ""

    def _load_self_info(self) -> None:
        """拿自己的 user info (OIDC user_info), 存 open_id + name 到 _name_cache。
        同时设 self._self_open_id 供 at_me 检测用。
        """
        if not self._user_token:
            return
        try:
            data = self._api_get("/authen/v1/user_info")
            if data:
                name = data.get("name", "")
                oid = data.get("open_id", "")
                if name and oid:
                    self._name_cache[oid] = name
                    self._self_open_id = oid
                    logger.info("social_takeover: self_info loaded name=%s open_id=%s", name, oid[:16])
        except Exception:
            pass

    def _list_chats(self) -> dict[str, dict]:
        """列出 zhp 参与的会话。返回 {chat_id: {name, type}}。

        Feishu `/im/v1/chats` API 不支持 chat_type 过滤参数(已实测被忽略),
        且只返回 group/topic 类型会话 —— P2P 私聊不在该列表里(P2P 是按需隐式建立的,
        通过给对方 open_id 发消息或读 `/im/v1/messages` 用 user_id_type 拉私聊历史)。

        MVP 策略: 这里仅拉 group/topic 会话; P2P 私聊消息走另一条路
        (后续单独加 `_fetch_p2p_with_recent_contacts` 或在 messages API 用 user_id_type
        拉跟特定联系人的 chat thread)。当前先把 group/topic 一类群萹库, P2P 留下一阶段。

        实测每条 item 含:
          chat_id, chat_mode(group|topic), name, owner_id, external, description...
        """
        chats: dict[str, dict] = {}
        page_token = None
        for _ in range(10):  # 最多翻 10 页
            params: dict[str, Any] = {
                "page_size": 100,
                "user_id_type": "open_id",
            }
            if page_token:
                params["page_token"] = page_token
            data = self._api_get("/im/v1/chats", params)
            if not data:
                break
            for item in data.get("items", []):
                cid = item.get("chat_id", "")
                if not cid:
                    continue
                name = item.get("name", "") or ""
                mode = (item.get("chat_mode", "") or "group").lower()
                # chat_mode=topic 当作 group 类(话题组也是群一种); 真正 P2P 不在这返
                type_str = "group" if mode in ("group", "topic") else mode
                if cid not in chats:
                    chats[cid] = {"name": name, "type": type_str}
            page_token = data.get("page_token")
            if not data.get("has_more"):
                break
        return _filter_chats_by_config(chats, self.instance_id)
    def _fetch_messages(self, chat_id: str, chat_name: str) -> list[dict]:
        """拉某个群的最近消息。返回标准化的消息列表。

        增强字段:
          - at_me: bool — 该消息是否@了当前机器人 user(本 OAuth 的 open_id)
          - at_all: bool — 是否@所有人
          - sender_is_app: bool — sender 是 app(机器人) 还是真人
        """
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
            msg_type = item.get("msg_type", "text")
            # 解析 body
            body_raw = item.get("body", {}).get("content", "{}")
            try:
                import json
                body = json.loads(body_raw) if isinstance(body_raw, str) else body_raw
                text = _extract_msg_text(body, msg_type)
            except Exception:
                text = str(body_raw)[:200]
            # 解析 sender
            sender = item.get("sender", {})
            sender_id = sender.get("id", "")
            sender_type = sender.get("sender_type", "")  # 'user' or 'app'
            sender_is_app = sender_type == "app"
            # sender name: 优先 cache; app 类型 →空
            sender_name = self._resolve_user_name(sender_id) if not sender_is_app else ""
            # 解析 mentions (@信息) — 同时把 mention name 缓存到 _name_cache
            mentions_raw = item.get("mentions") or []
            at_all = False
            at_me = False
            for mt in mentions_raw:
                if not isinstance(mt, dict):
                    continue
                mt_id = mt.get("id", "")
                mt_key = mt.get("key", "")
                mt_name = mt.get("name", "")
                if mt.get("is_at_all") or mt_id == "all" or mt_key == "at_all":
                    at_all = True
                elif self._self_open_id and (mt_id == self._self_open_id or mt_key == self._self_open_id):
                    at_me = True
                # 顺手缓存 mention name → open_id mapping (飞书 mention 自带 name)
                if mt_name and mt_id:
                    self._name_cache[mt_id] = mt_name

            # 用 mentions 的 {key: name} 替换 text 里的 @_user_N 占位符
            if mentions_raw and text:
                for mt in mentions_raw:
                    if isinstance(mt, dict):
                        mt_key = mt.get('key', '')
                        mt_name = mt.get('name', '')
                        if mt_key and mt_name:
                            text = text.replace(mt_key, f'@{mt_name}')

            # 放开 msg_type: text/share/post 都收, _extract_msg_text 已统一转成文本
            if text:
                messages.append({
                    "message_id": msg_id,
                    "chat_id": chat_id,
                    "chat_name": chat_name,
                    "sender_id": sender_id,
                    "sender_is_app": sender_is_app,
                    "sender_name": sender_name,
                    "text": text.strip()[:500],
                    "message_ts": float(item.get("create_time", "0") or "0"),
                    "at_me": at_me,
                    "at_all": at_all,
                    "mentions": mentions_raw,
                    "msg_type": msg_type,
                })
        return messages

    def _emit_command(self, msg: dict) -> None:
        """含命令的消息 → emit social_command 事件(触发 wake)。"""
        try:
            from infrastructure.config import set_current_instance_id
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
        # 1. 刷新会话列表(群 + P2P); 每 5 分钟刷一次
        if not self._chats or time.time() % 300 < POLL_INTERVAL:
            self._chats = self._list_chats()
            if not self._chats:
                return
            n_group = sum(1 for v in self._chats.values() if v.get("type") == "group")
            n_p2p = sum(1 for v in self._chats.values() if v.get("type") == "p2p")
            logger.info("social_takeover: chats refreshed instance=%s groups=%d p2p=%d",
                        self.instance_id[:8], n_group, n_p2p)
        # 2. 逐会话拉消息
        from domain.social.store import insert_message, has_command
        for chat_id, chat_meta in self._chats.items():
            chat_name = chat_meta.get("name", "") if isinstance(chat_meta, dict) else str(chat_meta)
            msgs = self._fetch_messages(chat_id, chat_name)
            for msg in msgs:
                self._seen_ids.add(msg["message_id"])
                is_new = insert_message(
                    source="feishu",
                    chat_id=msg["chat_id"],
                    chat_name=msg["chat_name"],
                    message_id=msg["message_id"],
                    sender_name=msg.get("sender_name", ""),
                    sender_id=msg["sender_id"],
                    text=msg["text"],
                    message_ts=msg["message_ts"],
                    instance_id=self.instance_id,
                    at_all=msg.get("at_all", False),
                    at_me=msg.get("at_me", False),
                    sender_is_app=msg.get("sender_is_app", False),
                )
                # 即时触发: zhp 本人说的话含关键词 → emit social_command 立刻唤醒
                # 其他人发的消息仅萹库, 等模型自然醒来时被动看到
                if is_new:
                    from domain.social.store import is_zhp_command
                    if is_zhp_command(msg["text"], msg["sender_id"], self._self_open_id):
                        self._emit_command(msg)
            # 控制频率, 避免飞书 rate limit
            time.sleep(0.5)
        # 3. 清理 _seen_ids (保留最近 5000 条)
        if len(self._seen_ids) > 10000:
            self._seen_ids = set(list(self._seen_ids)[-5000:])
        # 注: 已废除 _maybe_emit_review 主动触发 social_review 事件路径。
        # 社交接管改为「项目化 + 待办面板」模式: daemon 只萹库, 模型正常 wake
        # 时通过 todos 面板的「个人助理」项目日常待办, 决定是否进 social_feed 看
        # 最新消息。这种统一机制更符合"一切都是项目+待办"的数字生命哲学。

        # 4. 拉 P2P 私聊消息
        # 飞书 /im/v1/chats 不返回 P2P 会话——用 contacts 表里的真实人 open_id 逐个拉。
        # 只拉最近有互动的联系人(从 messages.db 取最近 7 天有私聊记录的 chat_id)。
        self._fetch_p2p_messages()

    def _resolve_p2p_chat_id(self, open_id: str) -> str:
        """V6: open_id → P2P chat_id (关键突破).

        飞书 /im/v1/messages 不接受 open_id 作为 container_id.
        但 /im/v1/chat_p2p/batch_query 可以把 open_id → P2P chat_id.
        参考 OpenClaw 飞书插件 resolveP2PChatId.

        返回 chat_id (oc_xxx) 或空字符串 (没有私聊记录).
        """
        try:
            resp = httpx.post(
                f"{self._base}/im/v1/chat_p2p/batch_query",
                headers={
                    "Authorization": f"Bearer {self._user_token}",
                    "Content-Type": "application/json",
                },
                params={"user_id_type": "open_id"},
                json={"chatter_ids": [open_id]},
                timeout=15,
            )
            data = resp.json()
            if data.get("code") == 0:
                chats = data.get("data", {}).get("p2p_chats", [])
                if chats:
                    return chats[0].get("chat_id", "")
            return ""
        except Exception:
            return ""

    def _fetch_p2p_messages(self) -> None:
        """拉 P2P 私聊消息。

        飞书 /im/v1/chats API 不返回私聊会话,我们必须自己确定跟谁有私聊。
        策略 (V6 扩展):
          1. 从 messages.db 取 ou_ 开头的 chat_id (bot 收到过的私聊)
          2. 从 contacts 表取活跃联系人的 open_id (zhp 的联系人)
          3. 从 social_feed 已有群消息的 sender_id 中提取 ou_ (在群里活跃的人可能有私聊)
          合并去重 → 逐个用 /im/v1/messages?container_id=ou_xxx 拉私聊消息。
          容忍失败 (没有私聊的会返回空或报错, 跳过即可)。
        """
        try:
            import sqlite3 as _sqlite3
            from pathlib import Path as _Path
            from domain.social.store import insert_message, is_zhp_command
            p2p_ids: set[str] = set()

            # 1. messages.db 里已知的 P2P chat_id
            inst_path = _Path("apps") / self.instance_id / "data" / "messages.db"
            inst_path = _Path("apps") / self.instance_id / "data" / "messages.db"
            if inst_path.exists():
                try:
                    mdb = _sqlite3.connect(str(inst_path), timeout=3.0)
                    mdb.row_factory = _sqlite3.Row
                    rows = mdb.execute(
                        "SELECT DISTINCT chat_id FROM messages "
                        "WHERE chat_id LIKE 'ou_%' "
                        "ORDER BY ROWID DESC LIMIT 30"
                    ).fetchall()
                    mdb.close()
                    p2p_ids.update(r["chat_id"] for r in rows)
                except Exception:
                    pass

            # 2. contacts 表的 open_id
            try:
                from infrastructure.config import get_runtime_state_db_path
                sdb = _sqlite3.connect(str(get_runtime_state_db_path()), timeout=3.0)
                sdb.row_factory = _sqlite3.Row
                crows = sdb.execute(
                    "SELECT open_id FROM contacts "
                    "WHERE open_id LIKE 'ou_%' AND open_id IS NOT NULL "
                    "ORDER BY ROWID DESC LIMIT 30"
                ).fetchall()
                sdb.close()
                p2p_ids.update(r["open_id"] for r in crows)
            except Exception:
                pass

            # 3. social_feed 群消息的 sender_id
            try:
                from infrastructure.config import get_runtime_state_db_path
                sdb = _sqlite3.connect(str(get_runtime_state_db_path()), timeout=3.0)
                sdb.row_factory = _sqlite3.Row
                srows = sdb.execute(
                    "SELECT DISTINCT sender_id FROM social_feed "
                    "WHERE source='feishu' AND sender_id LIKE 'ou_%' "
                    "ORDER BY ROWID DESC LIMIT 30"
                ).fetchall()
                sdb.close()
                p2p_ids.update(r["sender_id"] for r in srows)
            except Exception:
                pass

            if not p2p_ids:
                logger.debug("social_takeover: no P2P candidates found")
                return

            # 排除自己的 open_id (不需要拉自己的消息)
            if self._self_open_id:
                p2p_ids.discard(self._self_open_id)

            logger.info("social_takeover: fetching P2P from %d candidates", len(p2p_ids))
            for ou_id in p2p_ids:
                # V6 关键突破: 用 chat_p2p/batch_query 把 open_id → P2P chat_id
                # 再用 chat_id 调 /im/v1/messages 拉消息
                # (参考 OpenClaw 飞书插件 resolveP2PChatId)
                p2p_chat_id = self._resolve_p2p_chat_id(ou_id)
                if not p2p_chat_id:
                    continue  # 没有私聊记录, 跳过
                msgs = self._fetch_messages(p2p_chat_id, "")
                for msg in msgs:
                    self._seen_ids.add(msg["message_id"])
                    # P2P chat_name 用 sender_name
                    chat_name = msg.get("sender_name", "") or "私聊"
                    is_new = insert_message(
                        source="feishu",
                        chat_id=msg["chat_id"],
                        chat_name=chat_name,
                        message_id=msg["message_id"],
                        sender_name=msg.get("sender_name", ""),
                        sender_id=msg["sender_id"],
                        text=msg["text"],
                        message_ts=msg["message_ts"],
                        instance_id=self.instance_id,
                        at_all=False,
                        at_me=False,
                        sender_is_app=msg.get("sender_is_app", False),
                    )
                    if is_new and is_zhp_command(msg["text"], msg["sender_id"], self._self_open_id):
                        self._emit_command(msg)
                time.sleep(0.3)  # 限速
            logger.info("social_takeover: P2P fetch done from %d candidates", len(p2p_ids))
        except Exception as exc:
            logger.warning("social_takeover: p2p fetch failed: %s", exc)

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


def _filter_chats_by_config(chats: dict[str, dict], instance_id: str) -> dict[str, dict]:
    """按实例 app.yaml 的 social.takeover 白名单/黑名单过滤会话。

    配置结构（apps/<id>/config/app.yaml）::

        social:
          takeover:
            mode: allowlist        # allowlist | blocklist | all（默认 all 全拉）
            allowlist:             # mode=allowlist 时只拉这些（chat_id 或群名均可）
              - oc_xxx
              - 数字生命讨论群
            blocklist:             # mode=blocklist 时排除这些
              - oc_yyy

    匹配规则：条目与 chat_id 完全相等，或与群名完全相等（大小写不敏感）。
    mode=all 或未配置 → 不过滤（保持旧行为，兼容存量部署）。
    """
    try:
        import yaml

        from infrastructure.config import get_project_root

        cfg_path = get_project_root() / "apps" / instance_id / "config" / "app.yaml"
        if not cfg_path.exists():
            return chats
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        tk = ((raw.get("social") or {}).get("takeover")) or {}
        if not isinstance(tk, dict):
            return chats
        mode = str(tk.get("mode") or "all").strip().lower()

        def _norm_list(key: str) -> set[str]:
            items = tk.get(key) or []
            if isinstance(items, str):
                items = [items]
            return {str(x).strip().lower() for x in items if str(x).strip()}

        if mode == "allowlist":
            allowed = _norm_list("allowlist")
            if not allowed:
                logger.warning("social.takeover mode=allowlist 但 allowlist 为空 → 拉不到任何群，忽略过滤")
                return chats
            kept = {
                cid: meta for cid, meta in chats.items()
                if cid.lower() in allowed
                or str(meta.get("name") or "").strip().lower() in allowed
            }
            logger.info("social_takeover: allowlist filter %d → %d chats", len(chats), len(kept))
            return kept
        if mode == "blocklist":
            blocked = _norm_list("blocklist")
            kept = {
                cid: meta for cid, meta in chats.items()
                if cid.lower() not in blocked
                and str(meta.get("name") or "").strip().lower() not in blocked
            }
            logger.info("social_takeover: blocklist filter %d → %d chats", len(chats), len(kept))
            return kept
        return chats
    except Exception as exc:
        logger.warning("social_takeover: 白名单配置读取失败，不过滤: %s", exc)
        return chats

