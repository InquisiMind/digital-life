"""语音会话段路由 —— 转写文本 → 实例 → emit_event。

持续语音会话里，VAD 切出一段话、ASR 转写完，这一段要送给谁？
本模块回答这个问题：

  1. ``build_instance_keyword_map()`` —— 启动会话时调一次，遍历所有活跃实例，
     读各自 app.yaml 的 ``group_chat.attention_keywords``，构建
     ``{instance_id: [关键词...]}`` 映射。
  2. ``match_instance(transcript, keyword_map)`` —— 每段调一次：转写文本里
     是否包含某实例的关键词（子串、大小写不敏感）？命中则返回该实例 id。
  3. ``emit_segment_to_instance(...)`` —— 命中后把段作为 perception_signal 事件
     发给目标实例（设两个 ContextVar → emit_event）。

为什么是纯函数 + 显式注入：
  match_instance 不读 app.yaml、不碰 ContextVar，只做"文本里有没有这些词"
  的判定，输入输出确定，单测零 mock。副作用（读配置、emit）拆到另外两个函数，
  测试时分别 mock。

ASR 变体怎么处理：
  ASR 可能把 "zero" 听成 "zeros"/"吉洛"/"塞罗" 等。这些变体**不写死在代码里**——
  用户在 app.yaml 的 attention_keywords 里加，build_instance_keyword_map 自然读到。
  例：``attention_keywords: [zero, Zero, 吉洛, 塞罗]``
"""
from __future__ import annotations

import logging

from infrastructure.perception.config import PerceptionConfig

logger = logging.getLogger(__name__)


def build_instance_keyword_map() -> dict[str, list[str]]:
    """遍历活跃实例，构建 ``{instance_id: [keywords]}``。

    每个实例的关键词来自其 app.yaml 的 ``group_chat.attention_keywords``。
    没配关键词或读取失败的实例不进 map（无法被语音命中）。

    会话启动时调一次（不是每段），避免反复扫 apps/ 目录。
    """
    try:
        from infrastructure.config import discover_active_instances, get_instance_app_config_path
    except Exception:
        return {}

    import yaml

    out: dict[str, list[str]] = {}
    for iid in discover_active_instances():
        try:
            path = get_instance_app_config_path(iid)
            cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            kws = (cfg.get("group_chat") or {}).get("attention_keywords") or []
            if isinstance(kws, (list, tuple)) and kws:
                out[iid] = [str(k) for k in kws if k]
        except Exception as exc:
            logger.debug("read keywords for %s failed: %s", iid[:8], exc)
    logger.info("voice keyword map: %d instances → %s",
                len(out), {k[:8]: v for k, v in out.items()})
    return out


def match_instance(transcript: str, keyword_map: dict[str, list[str]]) -> str | None:
    """转写文本里是否包含某实例的关键词？返回命中的 instance_id，无命中 None。

    子串匹配、大小写不敏感。第一个命中的实例胜出（map 迭代顺序 = 构建顺序）。

    >>> match_instance("zero 帮我查个东西", {"id1": ["zero", "Zero"]})
    'id1'
    >>> match_instance("今天天气不错", {"id1": ["zero"]}) is None
    True
    >>> match_instance("", {"id1": ["zero"]}) is None
    True
    """
    text = (transcript or "").strip()
    if not text:
        return None
    text_lower = text.lower()
    for iid, keywords in keyword_map.items():
        for kw in keywords:
            if kw and str(kw).lower() in text_lower:
                return iid
    return None


def emit_segment_to_instance(
    instance_id: str,
    transcript: str,
    audio_path: str,
    config: PerceptionConfig,
) -> int | None:
    """把一段语音转写作为 perception_signal 事件发给目标实例。

    必须先把两个 ContextVar 都设到目标实例（emit_event 的 wake 链路依赖它们）：
      - ``set_current_instance_id``（DB 路径解析）
      - ``set_instance_context``（事件 channel 隔离）

    payload 复用 daemon._asr_and_report 的结构，标记 ``reply_channel="voice"``
    让实例的回复走 TTS。

    返回 event_id；失败返回 None。
    """
    try:
        from domain.lifecycle.events import emit_event, set_instance_context
        from infrastructure.config import set_current_instance_id

        # 关键：ContextVar 必须设到目标实例，否则 wake 链路找不到 channel
        set_current_instance_id(instance_id)
        set_instance_context(instance_id)

        event_id = emit_event("perception_signal", {
            "source": "voice_session",
            "summary": transcript or "（语音转写为空）",
            "transcript": transcript,
            "media_path": audio_path,
            "ok": bool(transcript),
            "reply_channel": "voice",
        })
        logger.info("voice segment routed to %s: event_id=%d transcript=%s",
                    instance_id[:8], event_id, (transcript or "")[:40])
        return event_id
    except Exception as exc:
        logger.error("emit_segment_to_instance failed (target=%s): %s",
                     instance_id[:8], exc)
        return None
