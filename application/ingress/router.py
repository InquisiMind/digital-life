"""Ingress message router — chat_id / bot_app_id → instance_id."""

from __future__ import annotations

import logging
import os

from interfaces.ingress.base import NormalizedMessage

logger = logging.getLogger(__name__)


def _instance_app_id(instance_id: str) -> str:
    """Read channels.feishu.app_id from apps/{id}/config/app.yaml."""
    from infrastructure.config import get_project_root
    import yaml
    config_file = get_project_root() / "apps" / instance_id / "config" / "app.yaml"
    if not config_file.exists():
        return ""
    try:
        cfg = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
        channels = cfg.get("channels") or {}
        feishu = channels.get("feishu") or {} if isinstance(channels, dict) else {}
        app_id = (feishu.get("app_id") or "").strip() if isinstance(feishu, dict) else ""
        return app_id
    except Exception:
        return ""


def resolve_instance(msg: NormalizedMessage) -> str:
    """Map an incoming normalized message to ONE digital life instance.

    Scans apps/{id}/config/app.yaml for Feishu mappings.
    Falls back to DIGITAL_LIFE_INSTANCE_ID env var or "zero".

    路由优先级：
      1. 群聊 + @ 了某个 bot → 路由给那个 bot 对应的实例
      2. chat_id 精确匹配
      3. bot_app_id 兜底
    """
    from infrastructure.config import discover_instances, get_project_root
    import yaml

    if msg.platform == "feishu":
        bot_app_id = ""
        if msg.raw:
            try:
                header = getattr(msg.raw, "header", None)
                if header:
                    bot_app_id = getattr(header, "app_id", "") or ""
            except Exception:
                pass

        # 1) 群聊 + @ 路由：优先级最高
        if msg.is_group and getattr(msg, "mentioned_bot_app_ids", None):
            mentioned = list(msg.mentioned_bot_app_ids)
            for instance_id in discover_instances():
                iid = _instance_app_id(instance_id)
                if iid and iid in mentioned:
                    logger.info("Group mention routed to %s (mentioned=%s)", instance_id[:8], mentioned)
                    return instance_id

        if msg.chat_id or bot_app_id:
            # 2) chat_id 精确匹配
            for instance_id in discover_instances():
                config_file = get_project_root() / "apps" / instance_id / "config" / "app.yaml"
                if not config_file.exists():
                    continue
                try:
                    cfg = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
                    channels = cfg.get("channels") or {}
                    feishu = channels.get("feishu") or {} if isinstance(channels, dict) else {}
                    chat_ids = (feishu.get("chat_ids") or []) if isinstance(feishu, dict) else []
                    if msg.chat_id and msg.chat_id in chat_ids:
                        return instance_id
                except Exception:
                    pass

            # 3) bot_app_id 兜底
            for instance_id in discover_instances():
                config_file = get_project_root() / "apps" / instance_id / "config" / "app.yaml"
                if not config_file.exists():
                    continue
                try:
                    cfg = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
                    channels = cfg.get("channels") or {}
                    feishu = channels.get("feishu") or {} if isinstance(channels, dict) else {}
                    cfg_app_id = (feishu.get("app_id") or "").strip() if isinstance(feishu, dict) else ""
                    if bot_app_id and cfg_app_id == bot_app_id:
                        return instance_id
                except Exception:
                    pass

    elif msg.platform == "wechat":
        # WeChat (ClawBot) 路由：当前进程就是这个实例的 adapter，
        # 没有 bot_app_id 兜底（ClawBot 长轮询只有自己一个 bot）。
        # 私聊场景默认路由到当前实例（就是 env DIGITAL_LIFE_INSTANCE_ID）。
        return os.environ.get("DIGITAL_LIFE_INSTANCE_ID") or "zero"

    # 兜底：其它平台（dingtalk / telegram 等）也走 fallback
    return os.environ.get("DIGITAL_LIFE_INSTANCE_ID") or os.environ.get("L4_AGENT_ID") or "zero"
