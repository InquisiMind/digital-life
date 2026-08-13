"""语音会话段路由 —— 转写文本 → 实例 → emit_event。

配置入口唯一化：每个实例的 app.yaml ``perception.wake_words`` 是唯一真相来源。
不再维护全局 voice_keywords.txt 的关键词→实例映射。

  1. ``build_instance_keyword_map()`` —— 读各实例 ``perception.wake_words``
     （fallback 到 ``group_chat.attention_keywords``），构建
     ``{instance_id: [wake_words]}`` 映射。
  2. ``build_keyword_to_instance_map()`` —— 反向映射 ``{keyword: instance_id}``，
     KWS 命中时直接查（不需要 ASR + match_instance 再绕一圈）。
  3. ``match_instance(transcript, keyword_map)`` —— ASR 转写后用子串匹配。
  4. ``emit_segment_to_instance(...)`` —— emit perception_signal 到目标实例。

配置示例（实例 app.yaml）：
  perception:
    wake_words:
      - zero        # 英文原名
      - 塞罗         # ASR 中文变体
      - 吉洛         # ASR 变体
"""
from __future__ import annotations

import logging

from infrastructure.perception.config import PerceptionConfig

logger = logging.getLogger(__name__)


def build_instance_keyword_map() -> dict[str, list[str]]:
    """遍历活跃实例，构建 ``{instance_id: [wake_words]}``。

    关键词来源（优先级）：
      1. app.yaml ``perception.wake_words``（语音专用，推荐）
      2. app.yaml ``group_chat.attention_keywords``（fallback，和群聊共用）
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
            # 优先读 perception.wake_words
            perc = cfg.get("perception") or {}
            kws = perc.get("wake_words") or []
            # fallback: group_chat.attention_keywords
            if not kws:
                kws = (cfg.get("group_chat") or {}).get("attention_keywords") or []
            if isinstance(kws, (list, tuple)) and kws:
                out[iid] = [str(k) for k in kws if k]
        except Exception as exc:
            logger.debug("read wake_words for %s failed: %s", iid[:8], exc)
    logger.info("voice keyword map: %d instances → %s",
                len(out), {k[:8]: v for k, v in out.items()})
    return out


def build_keyword_to_instance_map(keyword_map: dict[str, list[str]]) -> dict[str, str]:
    """反向映射 ``{keyword: instance_id}``。

    KWS 命中时直接用 keyword 查实例，不需要 ASR + match_instance。
    大小写不敏感（key 统一存小写）。
    """
    out: dict[str, str] = {}
    for iid, keywords in keyword_map.items():
        for kw in keywords:
            if kw:
                out[str(kw).lower()] = iid
    return out


def match_instance(transcript: str, keyword_map: dict[str, list[str]]) -> str | None:
    """转写文本里是否包含某实例的关键词？返回命中的 instance_id，无命中 None。

    子串匹配、大小写不敏感。

    >>> match_instance("zero 帮我查个东西", {"id1": ["zero", "Zero"]})
    'id1'
    >>> match_instance("塞罗 帮忙", {"id1": ["zero", "塞罗"]})
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


def lookup_instance_by_keyword(keyword: str, keyword_to_instance: dict[str, str]) -> str | None:
    """KWS 命中后直接用 keyword 查实例。大小写不敏感。

    >>> lookup_instance_by_keyword("塞罗", {"塞罗": "id1", "zero": "id1"})
    'id1'
    >>> lookup_instance_by_keyword("不存在", {"zero": "id1"})
    None
    """
    if not keyword:
        return None
    return keyword_to_instance.get(keyword.strip().lower())


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

    payload 标记 ``reply_channel="voice"`` 让实例的回复走 TTS。
    """
    try:
        from domain.lifecycle.events import emit_event, set_instance_context
        from infrastructure.config import set_current_instance_id

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
