"""Message ingress abstraction — platform-neutral message model + adapter interface."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable


@dataclass
class ChannelCapabilities:
    """声明这个通道支持什么——上层据此决策，不在业务代码里写 if platform == 'xxx'。

    每个平台能力差异很大（如 ClawBot 只能私聊、不能主动推送）。adapter 构造时
    填好这组 flag，上层（express_to_human / handler / social_context）查它就行。
    """

    supports_group: bool = True       # 能收/发群消息
    supports_dm: bool = True          # 能收/发私聊
    supports_proactive: bool = True   # 能主动发起消息（非被动回复）
    supports_media: bool = False      # 能发图片/语音/文件
    supports_mention: bool = True     # 支持 @ 功能
    max_text_length: int = 4000       # 单条消息文本上限


@dataclass
class NormalizedMessage:
    """Platform-neutral message — shields downstream code from platform details."""

    platform: str
    chat_id: str
    message_id: str
    sender_id: str
    content: str
    reply_to: str = ""
    sender_name: str = ""
    chat_name: str = ""
    is_group: bool = False
    mentions_bot: bool = False
    # 被 @ 的标识列表（app_id / open_id / "name:<显示名>"，给路由用）
    mentioned_bot_app_ids: list = field(default_factory=list)
    # mention 显示名列表，例：["alpha", "张三"]。prompt 注入用，不修改 text。
    mention_names: list = field(default_factory=list)
    # mention key→name 映射，例：["@_user_1→alpha"]。让模型理解 text 里脱敏占位符
    mention_map: list = field(default_factory=list)
    # 发送者是不是机器人(app)。飞书 event.sender.sender_type=='app' 时为 True。
    # 用于把兄弟/其它机器人联系人标记为 kind='bot'，并在发送侧判断"@ 到了机器人"。
    sender_is_bot: bool = False
    # 平台可能需要的上下文 token（如 ClawBot 的 context_token），send 时回传。
    # 对飞书/钉钉等留空；上层不直接读这个字段，由 adapter.send 自行使用。
    context_token: str = ""
    # 群消息 batch 合并历史:adapter 30s 窗口 + per-instance offset 吐 batch 时,
    # 把窗口里多条群消息以 [{sender, text}, ...] 形式挂这里。单条消息此字段为空 list。
    # 见 interfaces/ingress/group_buffer.py。handler 透传到事件 payload 供模型看到 batch 上下文。
    merged_texts: list = field(default_factory=list)
    # 多模态附件（source 无关）。adapter 入站时把图片/文件下载落盘 + register_attachment，
    # 这里挂轻量摘要——content 字段只是文本占位（"[图片 feishu:img_xxx]"），真实附件靠这里。
    # 元素形如 {"attachment_id": "feishu:img_v3_xxx", "source": "feishu", "source_key": "img_v3_xxx",
    #          "mime": "image/png", "kind": "image"}
    attachments: list = field(default_factory=list)
    raw: Any = None


MessageHandler = Callable[[NormalizedMessage], Awaitable[None]]


@runtime_checkable
class IngressAdapter(Protocol):
    """Message platform adapter interface.

    Each platform (Feishu, Telegram, WeChat, ...) implements one adapter.
    平台知识（凭证、ID 解析、@ 语义等）的**唯一家园**——上游不应直接读
    platform-specific 字段，一律通过本协议暴露的属性拿。
    """

    platform: str
    # 该 adapter 在本平台的稳定身份标识。每个平台语义不同：
    #   飞书 = app_id (cli_xxx)；钉钉 = agent_id+corp_id 拼；企微 = corp_id+agent_id
    # 上游（如 unified_contact 体系区分"哪个 app 视角"）一律走这个，
    # 不直接读 app.yaml 的具体平台字段——这是平台适配的边界。
    app_identity: str
    # 通道能力声明：群聊/私聊/主动推送/媒体/@ 等
    capabilities: ChannelCapabilities

    async def start(self) -> None:
        """Start the adapter (WebSocket connect, webhook listen, etc.)."""
        ...

    async def stop(self) -> None:
        """Stop the adapter gracefully."""
        ...

    async def send(self, chat_id: str, content: str, reply_to: str = "") -> bool:
        """Send a message. Returns True on success."""
        ...

    def on_message(self, handler: MessageHandler) -> None:
        """Register a callback for incoming messages."""
        ...
