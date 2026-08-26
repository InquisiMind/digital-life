"""Social context — 给模型的社交关系总览。

包含：
- 我认识的 contacts（人 / bot / system）— 来自 domain.contacts
- 我参与的群 — 来自 app.yaml channels.feishu.chat_ids + project.yaml.group_chat_id + chat_stream 历史里见过的 chat_id

每次 wake 注入到 prompt 里（_sys_tool=social_context），让模型决定
「这条话应该发给谁，发哪个 chat」。

注：项目岗位不再渲染——_role_positioning 段（scheduler.py）已经把
   "我担任什么项目的什么岗位 + 协作者是谁" 完整内容插到 system prompt
   里，这里再渲染会重复。
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def render_social_context(instance_id: str) -> str:
    """渲染给 LLM 看的社交关系文本。空时返回空字符串。

    多通道设计：每个联系人显示**所有平台**的可达 ID + 发送提示。
    模型据此决定「用什么 channel 发给谁」。
    """
    lines: list[str] = ["## ── 我的社交关系 ──"]

    # ─── 联系人（contacts） ───
    try:
        from domain.contacts import list_contacts
        cs = list_contacts() or []
        humans = [c for c in cs if c.get("kind") == "human"]
        bots = [c for c in cs if c.get("kind") == "bot"]

        def _channel_ids(c: dict) -> list[str]:
            """提取联系人**所有平台**的可达 ID（带通道前缀）。

            返回例：["feishu:ou_eb5083eb...", "wechat:zhp@im.wechat..."]
            ⚠️ 完整 ID 必须保留——模型会原样填回 express_to_human(channel=...)。
               任何截断都会让模型拿到无法发送的废字符串。
            """
            out = []
            for p in (c.get("platform_ids") or []):
                pf = (p.get("platform") or "").strip()
                pid = (p.get("platform_id") or "").strip()
                if not pid:
                    continue
                if pf == "feishu":
                    out.append(f"feishu:{pid}")
                elif pf == "wechat":
                    out.append(f"wechat:{pid}")
                else:
                    out.append(f"{pf}:{pid}")
            return out

        if humans:
            lines.append("\n联系人（人类），回复时按平台填 channel：")
            lines.append("  格式：feishu:dm:<ou_xxx>（飞书私聊）/ feishu:group:<oc_xxx>（飞书群）/ wechat:dm:<xxx@im.wechat>（微信）")
            for c in humans:
                ids = _channel_ids(c)
                name = (c.get("name") or "").strip()
                label = name if name else "(未命名)"
                id_part = f" [{', '.join(ids)}]" if ids else ""
                note = (c.get("notes") or "").strip()
                lines.append(f"  · {label}{id_part}" + (f" / 备注: {note[:80]}" if note else ""))
        if bots:
            lines.append("\n联系人（机器人，群内可用 @<name> 召唤）：")
            for c in bots:
                ids = _channel_ids(c)
                name = (c.get("name") or "").strip()
                label = name if name else "(未命名 bot)"
                id_part = f" [{', '.join(ids)}]" if ids else ""
                note = (c.get("notes") or "").strip()
                lines.append(f"  · {label}{id_part}" + (f" / 备注: {note[:80]}" if note else ""))
    except Exception as exc:
        logger.debug("social_context contacts failed: %s", exc)

    # ─── 窗口档案（chats 表：OC → name/type，自动建档单一真相）───
    try:
        from domain.contacts import list_chats
        chats = list_chats(limit=30) or []
        if chats:
            lines.append("\n窗口（OC = 会话 ID；群/私聊是类型），回复时 chat_id 填这些：")
            for ch in chats:
                cid = ch.get("chat_id") or ""
                if not cid:
                    continue
                t = ch.get("type") or ""
                t_label = {"group": "群", "dm": "私聊"}.get(t, "会话")
                name = (ch.get("name") or "").strip() or "(未命名)"
                lines.append(f"  · {name}（{cid}，{t_label}）")
    except Exception as exc:
        logger.debug("social_context chats failed: %s", exc)

    # ─── ID 用法说明（OC/OU 语义统一教学）───
    lines.append(
        "\nID 用法：OC = 窗口 ID（群/私聊都是它，回复填 chat_id 用 OC）；"
        "OU = 用户 ID（@ 人 / 识别谁在说话）；跨实例提人用名字、提窗口可用 OC。"
    )

    if len(lines) <= 1:
        return ""

    lines.append("\n## ── /社交关系 ──")
    return "\n".join(lines)
