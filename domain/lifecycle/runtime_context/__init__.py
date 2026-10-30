"""L4 lifecycle context: 当前进程正在执行哪个 affair。

运行时在加载事务时调用 set_current_affair(aid)，
lifecycle 工具通过 get_current_affair() 拿到 aid 并把 WaitIntent 绑定到它。

用 ContextVar 而非全局变量，使得同进程内多运行时实例不互串。
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Optional

_current: ContextVar[Optional[str]] = ContextVar("digital_life_affair_id", default=None)


def set_current_affair(affair_id: Optional[str]) -> None:
    _current.set(affair_id)


def get_current_affair() -> Optional[str]:
    return _current.get()


_wake_reason: ContextVar[str] = ContextVar("digital_life_wake_reason", default="")


def set_current_wake_reason(reason: str) -> None:
    _wake_reason.set(reason)


def get_current_wake_reason() -> str:
    return _wake_reason.get()


# 当前正在交互的 conversation_id（平台内唯一 ID，如飞书 oc_xxx）
# sense_conversation 用它做智能默认过滤
_conversation_id: ContextVar[str] = ContextVar("digital_life_conversation_id", default="")


def set_current_conversation_id(conv_id: str) -> None:
    _conversation_id.set(conv_id)


def get_current_conversation_id() -> str:
    return _conversation_id.get()


# 当前正在处理的事件来源 chat_id（消息事件携带，prompt hint + reply channel 默认值）
# set 由 scheduler wake 入口、mid-session inject 路径负责
# express_to_human 在模型未显式指定 chat_id 时用它做 fallback
# 与 conversation_id 的区别：conversation_id 更宽泛（含主动发起的会话），
# event_chat_id 严格表示"当前刺激来源 chat"
_event_chat_id: ContextVar[str] = ContextVar("digital_life_event_chat_id", default="")


def set_current_event_chat_id(chat_id: str) -> object:
    """设置当前事件来源 chat_id。返回 token 供 reset 使用。"""
    return _event_chat_id.set(chat_id or "")


def get_current_event_chat_id() -> str:
    return _event_chat_id.get()


def reset_current_event_chat_id(token) -> None:
    """与 set_current_event_chat_id 配对使用。"""
    try:
        _event_chat_id.reset(token)
    except Exception:
        pass


# ── 多通道上下文：当前事件来自哪个平台 + 通道相关的 platform token ──────────
# 平台前缀统一用 feishu（旧数据可能存 lark，读侧需兜底认两者）。
# express_to_human 用来决定发到哪个通道(action_tools.py channel 前缀)
_event_platform: ContextVar[str] = ContextVar("digital_life_event_platform", default="")


def set_current_event_platform(platform: str) -> object:
    """设置当前事件来源平台（feishu/wechat/...）。返回 token。

    归一化：feishu 与历史遗留的 lark 都归一为 feishu（内部统一前缀）。
    wechat 等其他值原样保留。
    """
    _pf = "feishu" if (platform or "").lower() in ("feishu", "lark") else (platform or "")
    return _event_platform.set(_pf)


def get_current_event_platform() -> str:
    """返回当前事件平台前缀（feishu / wechat / ''=未设置）。

    消费方读侧匹配 channel 字符串时应同时认 'feishu' 与存量历史值 'lark'。
    """
    return _event_platform.get()


def reset_current_event_platform(token) -> None:
    try:
        _event_platform.reset(token)
    except Exception:
        pass


# ── 语音提前播报：已提前 TTS 的 text 前缀长度 ───────────────────────────────
# 流式解析（infrastructure/ai/tool_stream.py）在模型还在生成 express_to_human
# 参数时就按句提前 TTS；工具正式分派时 agent 把已播长度放进本 ContextVar，
# _send_voice_local 读取后只播剩余尾巴（日志仍记全文）。
_voice_prespeak_offset: ContextVar[int] = ContextVar("digital_life_voice_prespeak_offset", default=0)


def set_voice_prespeak_offset(chars: int) -> object:
    """设置 express_to_human 本次调用已被提前播报的前缀字符数。"""
    return _voice_prespeak_offset.set(max(0, int(chars)))


def get_voice_prespeak_offset() -> int:
    return _voice_prespeak_offset.get()


def reset_voice_prespeak_offset() -> None:
    _voice_prespeak_offset.set(0)


# ClawBot context_token：微信 ClawBot 发回复必须传的对话令牌
# express_to_human → _send_wechat_clawbot 读取它
_context_token: ContextVar[str] = ContextVar("digital_life_context_token", default="")


def set_current_context_token(token: str) -> object:
    """设置当前会话的 ClawBot context_token。返回 token。"""
    return _context_token.set(token or "")


def get_current_context_token() -> str:
    """返回当前会话的 context_token（ClawBot 用，飞书为空）。"""
    return _context_token.get()


def reset_current_context_token(token) -> None:
    try:
        _context_token.reset(token)
    except Exception:
        pass


# 当前 wake 对应的"入站原消息 msg_id"(三态收条用)
# handler 入站 set；express_to_human 发送成功后读它 -> clear_on_reply 撤掉 ⚙️
# wake 结束(没回任何消息)时也该清掉残留 ⚙️ —— 由 wake_inner finally 调 reset
_reply_msg_id: ContextVar[str] = ContextVar("digital_life_reply_msg_id", default="")


def set_current_reply_msg_id(msg_id: str) -> object:
    """记录当前 wake 对应的入站消息 id（三态收条:👀/⚙️ 的 key）。返回 token。"""
    return _reply_msg_id.set(msg_id or "")


def get_current_reply_msg_id() -> str:
    """返回当前 wake 的入站原消息 id(用于收条表情管理)。空表示无关联消息。"""
    return _reply_msg_id.get()


def reset_current_reply_msg_id(token) -> None:
    try:
        _reply_msg_id.reset(token)
    except Exception:
        pass


# 当前在跑的 wake 的 audit id（runtime_log.wake 表主键）。
# scheduler 在 WakeContext.start 之后 set，wake 结束后 reset。
# mid-session 注入（_inject_to_running_session / _mirror_inject_to_audit_turn）读它，
# 把注入的 user turn 精确挂到当前 wake——而不是靠 ended_at IS NULL 猜（旧实现会挂到
# 失败 wake 的 NULL 行上，导致前端按 wake_id 分组时把消息错归到上一组）。
_current_wake_id: ContextVar[str] = ContextVar("digital_life_current_wake_id", default="")


def set_current_wake_id(wake_id: str) -> object:
    """记录当前在跑的 wake 的 audit id。返回 token 供 reset 使用。"""
    return _current_wake_id.set(wake_id or "")


def get_current_wake_id() -> str:
    """返回当前在跑的 wake id；空表示当前没在 wake 作用域内。"""
    return _current_wake_id.get()


def reset_current_wake_id(token) -> None:
    try:
        _current_wake_id.reset(token)
    except Exception:
        pass
