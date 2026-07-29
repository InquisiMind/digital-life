"""微信全量消息接管模块 — 基于 itchat (Web 协议)。

以用户身份登录微信, 监听全量消息 (群+私聊), 转发给 digital-life。
和 feishu_takeover 对称 — 一个接管飞书, 一个接管微信。

使用方式:
  1. 前端点"接管我的微信" → 调 /api/system/instances/{iid}/wechat-takeover/start
  2. 后端启动 itchat 线程, 生成 QR 码图片
  3. 前端显示 QR → 用户扫码
  4. itchat 登录成功 → daemon 持续监听消息
  5. 每条消息 → HTTP POST /internal/wechat-ingest → social_feed
"""
from __future__ import annotations

import base64
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ============ 全局状态 (和 ClawBot 扫码类似的模式) ============

_takeover_state: dict[str, dict[str, Any]] = {}
"""{instance_id: {status, qr_base64, daemon_thread, error, login_time}}"""

_DAEMON_DIGITAL_LIFE_URL = os.environ.get("WECHAT_INGEST_URL", "http://localhost:8642")
_MAX_TEXT_LEN = 2000


def _forward_message(msg: dict, instance_id: str) -> None:
    """把 itchat 消息转发给 digital-life /internal/wechat-ingest。"""
    try:
        chat_id = str(msg.get("chat_id") or "")
        text = str(msg.get("text") or "")[:_MAX_TEXT_LEN]
        if not chat_id or not text.strip():
            return
        payload = {
            "chat_id": chat_id,
            "chat_name": str(msg.get("chat_name") or ""),
            "sender_id": str(msg.get("sender_id") or ""),
            "sender_name": str(msg.get("sender_name") or ""),
            "text": text,
            "is_group": bool(msg.get("is_group")),
            "msg_ref": str(msg.get("msg_ref") or f"wx_{time.time()}"),
            "timestamp": float(msg.get("timestamp") or 0),
            "media_type": str(msg.get("media_type") or "text"),
        }
        resp = httpx.post(
            f"{_DAEMON_DIGITAL_LIFE_URL}/internal/wechat-ingest",
            json=payload,
            timeout=10,
        )
        if resp.status_code == 200:
            r = resp.json()
            if r.get("new"):
                logger.info("wechat_takeover: forwarded chat=%s from=%s text=%r",
                            payload["chat_name"][:16] or chat_id[:12],
                            payload["sender_name"][:10],
                            text[:40])
    except Exception as e:
        logger.debug("wechat_takeover forward error: %s", e)


def _run_itchat_daemon(instance_id: str) -> None:
    """itchat daemon 线程主体。"""
    try:
        import itchat

        # QR 码回调 — itchat 生成 QR 时调这个
        def _qr_callback(uuid: str, status: str, qrcode: str):
            """itchat 4.x 的 QR 回调。3.x 用 picDir 参数。"""
            state = _takeover_state.get(instance_id, {})
            if status == "0":
                # QR 码就绪
                try:
                    # itchat 3.x: qrcode 是图片文件路径
                    if qrcode and os.path.exists(qrcode):
                        with open(qrcode, "rb") as f:
                            qr_b64 = base64.b64encode(f.read()).decode()
                        state["qr_base64"] = f"data:image/png;base64,{qr_b64}"
                        state["status"] = "qr_ready"
                        logger.info("wechat_takeover: QR ready for %s", instance_id[:8])
                except Exception:
                    pass

        # 消息处理回调
        @itchat.msg_register(itchat.content.TEXT)
        def on_text(msg):
            _process_itchat_msg(msg, instance_id)

        @itchat.msg_register([itchat.content.PICTURE, itchat.content.VOICE, itchat.content.VIDEO])
        def on_media(msg):
            _process_itchat_msg(msg, instance_id, media_type="media")

        @itchat.msg_register(itchat.content.CARD)
        def on_card(msg):
            _process_itchat_msg(msg, instance_id, media_type="card")

        @itchat.msg_register(itchat.content.SHARING)
        def on_sharing(msg):
            _process_itchat_msg(msg, instance_id, media_type="sharing")

        # V6: 用 qrCallback 拿到 QR PNG bytes → 转 base64 → 填 state
        # 不用 picDir (会弹系统图片查看器), 不用 enableCmdQR (终端 ASCII)
        def _qr_cb(uuid=None, status=None, qrcode=None):
            if qrcode and len(qrcode) > 100:
                try:
                    qr_b64 = base64.b64encode(qrcode).decode()
                    st_qr = _takeover_state.get(instance_id, {})
                    st_qr["qr_base64"] = f"data:image/png;base64,{qr_b64}"
                    st_qr["status"] = "qr_ready"
                    logger.info("wechat_takeover: QR ready (qrCallback, %d bytes)", len(qrcode))
                except Exception:
                    pass

        # auto_login 阻塞 (等扫码) — 用 qrCallback 不弹系统图片
        itchat.auto_login(
            hotReload=True,
            qrCallback=_qr_cb,
        )

        # 登录成功 (auto_login 返回了)
        st = _takeover_state.get(instance_id, {})
        st["status"] = "logged_in"
        st["login_time"] = time.time()
        logger.info("wechat_takeover: %s 登录成功, 开始监听消息", instance_id[:8])

        # 获取自己的用户信息
        try:
            myself = itchat.search_friends()
            if myself:
                st["my_username"] = myself.get("UserName", "")
                st["my_nickname"] = myself.get("NickName", "")
        except Exception:
            pass

        # 阻塞运行 (持续监听)
        itchat.run(debug=False)

    except Exception as e:
        logger.error("wechat_takeover daemon error: %s", e, exc_info=True)
        st = _takeover_state.get(instance_id)
        if st:
            st["status"] = "error"
            st["error"] = str(e)


def _process_itchat_msg(msg, instance_id: str, media_type: str = "text") -> None:
    """处理一条 itchat 消息, 提取字段转发。"""
    try:
        from_user = msg.get("FromUserName", "")
        actual_user = msg.get("ActualUserName", "") or from_user
        is_group = "@chatroom" in from_user

        if is_group:
            chat_id = from_user
            try:
                chat_name = msg.get("User", {}).get("NickName", "") or msg.get("User", {}).get("RemarkName", "")
            except Exception:
                chat_name = ""
            sender_id = actual_user
            try:
                sender_name = msg.get("ActualNickName", "") or actual_user
            except Exception:
                sender_name = actual_user
        else:
            chat_id = from_user
            try:
                chat_name = msg.get("User", {}).get("RemarkName", "") or msg.get("User", {}).get("NickName", "")
            except Exception:
                chat_name = ""
            sender_id = from_user
            sender_name = chat_name

        text = ""
        if media_type == "text":
            text = str(msg.get("Text", "") or "")
        elif media_type == "media":
            mt = int(msg.get("MsgType", 0) or 0)
            if mt == 3:
                text = "[图片]"
            elif mt == 34:
                text = "[语音]"
            elif mt == 43:
                text = "[视频]"
            else:
                text = f"[媒体]"
        elif media_type == "card":
            text = "[名片]"
        elif media_type == "sharing":
            text = str(msg.get("Text", "") or "[链接]")

        # 过滤系统消息
        msg_type = int(msg.get("MsgType", 0) or 0)
        if msg_type in (51, 10000, 10002):
            return

        create_time = float(msg.get("CreateTime", 0) or 0)
        msg_id = str(msg.get("MsgId", "")) or f"wx_{create_time}"

        _forward_message({
            "chat_id": chat_id,
            "chat_name": chat_name,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "text": text,
            "is_group": is_group,
            "msg_ref": f"wx_{msg_id}",
            "timestamp": create_time,
            "media_type": media_type,
        }, instance_id)

    except Exception as e:
        logger.debug("wechat_takeover msg process error: %s", e)


# ============ 公开 API (给 system_routes.py 调用) ============

def start_takeover(instance_id: str) -> dict[str, Any]:
    """启动微信接管 daemon。返回初始状态 (QR 码稍后异步生成)。"""
    state = _takeover_state.get(instance_id)
    if state and state.get("status") in ("logged_in", "logging_in", "qr_ready"):
        return {
            "ok": True,
            "status": state["status"],
            "message": "daemon already running" if state["status"] == "logged_in" else "QR already generated",
        }

    _takeover_state[instance_id] = {
        "status": "starting",
        "qr_base64": "",
        "error": "",
        "login_time": 0,
    }

    # 新线程跑 itchat (阻塞)
    thread = threading.Thread(
        target=_run_itchat_daemon,
        args=(instance_id,),
        daemon=True,
        name=f"wechat_takeover_{instance_id[:8]}",
    )
    thread.start()
    _takeover_state[instance_id]["daemon_thread"] = thread

    return {"ok": True, "status": "starting", "message": "daemon starting, QR will be ready in ~5s"}


def get_takeover_status(instance_id: str) -> dict[str, Any]:
    """获取接管状态 + QR 码。"""
    state = _takeover_state.get(instance_id)
    if not state:
        return {"ok": False, "status": "not_started", "message": "点'接管我的微信'开始"}
    return {
        "ok": True,
        "status": state.get("status", "unknown"),
        "qr_base64": state.get("qr_base64", ""),
        "login_time": state.get("login_time", 0),
        "my_nickname": state.get("my_nickname", ""),
        "error": state.get("error", ""),
    }


def stop_takeover(instance_id: str) -> dict[str, Any]:
    """停止微信接管 (logout)。"""
    state = _takeout_state = _takeover_state.get(instance_id)
    if not state:
        return {"ok": False, "reason": "not running"}

    try:
        import itchat
        itchat.logout()
        logger.info("wechat_takeover: %s logged out", instance_id[:8])
    except Exception:
        pass

    state["status"] = "stopped"
    return {"ok": True, "message": "logged out"}
