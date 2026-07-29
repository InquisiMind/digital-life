#!/usr/bin/env python3
"""微信消息接管 daemon — 基于 itchat (Web 协议)。

部署方式:
  1. 在一台能跑微信 Web 协议的机器上 (Windows/Mac/Linux 均可)
  2. pip install itchat requests
  3. python3 wechat_daemon.py
  4. 扫码登录
  5. 自动监听所有消息 → HTTP POST 推送到 digital-life

只读模式: 不调 send_msg, 不自动回复。
如果需要回复, 由 digital-life 的 agent 通过飞书/其它通道通知用户。

配置: 改下面的 DIGITAL_LIFE_URL 和 INSTANCE_ID。
"""

import json
import time
import logging
import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("wechat_daemon")

# ============ 配置 ============
DIGITAL_LIFE_URL = "http://localhost:8642"  # digital-life 服务地址
INGEST_PATH = "/internal/wechat-ingest"
POLL_INTERVAL = 3  # itchat 消息轮询间隔 (秒)
MAX_TEXT_LEN = 2000  # 截断超长消息

# ============ 消息转发 ============

def forward_message(msg) -> None:
    """把 itchat Message 转发给 digital-life。"""
    try:
        # itchat 的 Message 对象属性
        msg_id = msg.get("MsgId") or str(msg.get("CreateTime", 0)) or f"wx_{time.time()}"
        text = msg.get("Text", "") or ""
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="replace")
        text = str(text)[:MAX_TEXT_LEN]

        from_user = msg.get("FromUserName", "")  # wxid_xxx 或 xxx@chatroom
        to_user = msg.get("ToUserName", "")
        actual_user = msg.get("ActualUserName", "") or from_user
        user_remark = msg.get("User", {}).get("RemarkName") or msg.get("User", {}).get("NickName", "")
        create_time = float(msg.get("CreateTime", 0) or 0)

        # 判断群聊 vs 私聊
        is_group = "@chatroom" in from_user
        if is_group:
            chat_id = from_user
            # 群名
            chat_name = ""
            try:
                user_obj = msg.get("User", {})
                chat_name = user_obj.get("NickName", "") or user_obj.get("RemarkName", "")
            except Exception:
                pass
            # 实际发送者 (群消息里 FromUserName 是 chatroom, ActualUserName 是个人)
            sender_id = actual_user
            sender_name = msg.get("ActualNickName", "") or actual_user
        else:
            chat_id = from_user
            chat_name = user_remark
            sender_id = from_user
            sender_name = user_remark

        # 只推文本/卡片类消息, 过滤系统消息
        msg_type = int(msg.get("MsgType", 0) or 0)
        if msg_type == 10000:  # 系统消息 (撤回/入群等)
            return
        if msg_type == 51:  # 同步消息 (自己发的)
            return

        # 非文本消息标记类型
        media_type = "text"
        if msg_type == 3:
            media_type = "image"
            text = "[image]"
        elif msg_type == 49:
            media_type = "link/card"
            text = text or "[链接/文件]"
        elif msg_type == 34:
            media_type = "voice"
            text = "[语音]"
        elif msg_type == 43:
            media_type = "video"
            text = "[视频]"

        payload = {
            "chat_id": chat_id,
            "chat_name": chat_name,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "text": text,
            "is_group": is_group,
            "msg_ref": f"wx_{msg_id}",
            "timestamp": create_time,
            "media_type": media_type,
        }

        resp = requests.post(
            f"{DIGITAL_LIFE_URL}{INGEST_PATH}",
            json=payload,
            timeout=10,
        )
        if resp.status_code == 200:
            r = resp.json()
            if r.get("new"):
                logger.info("✓ forwarded: chat=%s from=%s text=%r",
                            chat_name[:16] or chat_id[:12],
                            sender_name[:10],
                            text[:40])
        else:
            logger.warning("forward failed: HTTP %d %s", resp.status_code, resp.text[:100])

    except requests.exceptions.ConnectionError:
        logger.warning("digital-life 不可达, 消息丢弃 (chat=%s)", from_user[:12])
    except Exception as e:
        logger.error("forward error: %s", e, exc_info=True)


# ============ 主入口 ============

def main():
    import itchat

    logger.info("=== 微信接管 daemon 启动 ===")
    logger.info("目标: %s%s", DIGITAL_LIFE_URL, INGEST_PATH)

    # 登录 (hotReload=True 缓存登录状态, 下次不用重新扫码)
    itchat.auto_login(hotReload=True, enableCmdQR=2)

    logger.info("✅ 微信登录成功, 开始监听消息 (只读模式, 不回复)")

    # 注册消息处理
    @itchat.msg_register(itchat.content.TEXT)
    def on_text(msg):
        forward_message(msg)

    @itchat.msg_register([itchat.content.PICTURE, itchat.content.VOICE, itchat.content.VIDEO])
    def on_media(msg):
        forward_message(msg)

    @itchat.msg_register(itchat.content.CARD)
    def on_card(msg):
        forward_message(msg)

    @itchat.msg_register(itchat.content.SHARING)
    def on_sharing(msg):
        forward_message(msg)

    # 阻塞运行
    itchat.run(debug=False)


if __name__ == "__main__":
    main()
