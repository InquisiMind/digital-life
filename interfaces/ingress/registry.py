"""通道注册表 + 工厂函数 —— 从配置创建 adapter 的唯一入口。

架构原则：server.py / handler.py 不直接 import 具体Adapter，全部通过本模块
的 create_adapters_from_config 拿到 adapter 列表。

支持的通道类型（在 _ADAPTER_BUILDERS 里注册）：
  - "feishu" → FeishuAdapter（飞书 WebSocket 长连接）
  - "wechat_clawbot" → WeChatClawBotAdapter（微信 ClawBot 长轮询）

未来加新通道只需：
  1. 实现 IngressAdapter Protocol（新建一个 adapter 文件）
  2. 在 _ADAPTER_BUILDERS 加一个 factory 函数
  3. 其余层（handler/router/server）零改动
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("digital_life.ingress.registry")


def _build_feishu(cfg: dict[str, Any], secrets_env: dict[str, str]) -> Any:
    """飞书 adapter 工厂。凭证不全返回 None（跳过，不报错）。"""
    from interfaces.ingress.feishu import FeishuAdapter

    app_id = str(cfg.get("app_id") or "").strip()
    app_secret = (
        secrets_env.get("FEISHU_APP_SECRET")
        or os.getenv("FEISHU_APP_SECRET")
        or ""
    ).strip()
    domain = str(cfg.get("feishu_domain") or "").strip() or None
    if not app_id or not app_secret:
        logger.info("feishu channel skipped: missing app_id or app_secret")
        return None
    return FeishuAdapter(app_id=app_id, app_secret=app_secret, domain=domain)


def _build_wechat_clawbot(cfg: dict[str, Any], secrets_env: dict[str, str]) -> Any:
    """微信 ClawBot adapter 工厂。凭证不全返回 None（跳过，不报错）。"""
    from interfaces.ingress.wechat_clawbot import WeChatClawBotAdapter

    bot_token = (
        secrets_env.get("WECHAT_BOT_TOKEN")
        or os.getenv("WECHAT_BOT_TOKEN")
        or ""
    ).strip()
    domain = str(cfg.get("domain") or "").strip() or None
    bot_id = str(cfg.get("bot_id") or "").strip()
    if not bot_token:
        logger.info("wechat channel skipped: no WECHAT_BOT_TOKEN (待扫码登录)")
        return None
    return WeChatClawBotAdapter(bot_token=bot_token, domain=domain, bot_id=bot_id)


# 通道类型 → builder 映射
_ADAPTER_BUILDERS = {
    "feishu": _build_feishu,
    "wechat_clawbot": _build_wechat_clawbot,
    # 未来：
    # "dingtalk_stream": _build_dingtalk,
    # "telegram": _build_telegram,
    # "wecom": _build_wecom,
}


def parse_channels(app_yaml_cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """从实例的 app.yaml 解析 channels 配置。

    只读 ``channels`` 段。飞书/微信等通道字段统一存放在
    ``channels.<type>.<field>``（与 ConfigField.path、init_instance 模板、
    adapter 读取侧一致），不存在 ``messenger`` 旧段兜底。

    格式：
        channels:
          feishu:
            type: feishu
            app_id: cli_xxx
          wechat:
            type: wechat_clawbot
            domain: https://...

    旧实例的 ``messenger`` 段需先跑 ``scripts/migrate_messenger_to_channels.py``
    搬到 ``channels.feishu``，否则飞书通道不会生效。
    """
    if not app_yaml_cfg:
        return {}

    raw_channels = app_yaml_cfg.get("channels")
    if not isinstance(raw_channels, dict):
        return {}
    return {name: cfg for name, cfg in raw_channels.items() if isinstance(cfg, dict)}


def create_adapters_from_config(
    app_yaml_cfg: dict[str, Any],
    secrets_env: dict[str, str],
) -> list[Any]:
    """从配置创建所有通道的 adapter 列表。

    Args:
        app_yaml_cfg: apps/<id>/config/app.yaml 解析后的 dict
        secrets_env: apps/<id>/config/secrets.env 解析后的 {key: value}

    Returns:
        adapter 实例列表（已初始化但未 start）
    """
    channels = parse_channels(app_yaml_cfg)
    if not channels:
        logger.warning("No channels configured in app.yaml")
        return []

    adapters = []
    for ch_name, ch_cfg in channels.items():
        adapter_type = str(ch_cfg.get("type") or ch_name)
        builder = _ADAPTER_BUILDERS.get(adapter_type)
        if not builder:
            logger.warning(
                "Unknown channel type '%s' (name='%s'), skipping. "
                "Supported: %s",
                adapter_type, ch_name, list(_ADAPTER_BUILDERS.keys()),
            )
            continue
        try:
            adapter = builder(ch_cfg, secrets_env)
            if adapter is None:
                # builder 返回 None = 凭证不全，跳过（不报错）
                continue
            adapters.append(adapter)
            logger.info(
                "Channel '%s' (type=%s) adapter created: platform=%s identity=%s",
                ch_name, adapter_type,
                getattr(adapter, "platform", "?"),
                getattr(adapter, "app_identity", "?"),
            )
        except Exception as exc:
            logger.error(
                "Failed to create adapter for channel '%s' (type=%s): %s",
                ch_name, adapter_type, exc,
            )

    return adapters


def get_supported_types() -> list[str]:
    """返回当前注册的所有通道类型名。"""
    return list(_ADAPTER_BUILDERS.keys())


def channel_signatures(
    app_yaml_cfg: dict[str, Any],
    secrets_env: dict[str, str],
) -> dict[str, str]:
    """计算 ``platform -> signature``，用于热重载 diff。

    signature 由该通道参与构造 adapter 的全部字段拼接而成（app_id / domain /
    bot_id + 对应 secrets.env 凭据）。任何字段改动都会让 signature 变化，
    触发 ``_hot_reload_channels`` 停旧 adapter、起旧 adapter。

    与 ``_build_*`` 工厂读取的字段保持一致即可——凭据缺失的通道不会被 factory
    返回 adapter，这里同样跳过（不写入 signatures dict）。
    """
    channels = parse_channels(app_yaml_cfg)
    out: dict[str, str] = {}
    for ch_name, ch_cfg in channels.items():
        adapter_type = str(ch_cfg.get("type") or ch_name)
        if adapter_type == "feishu":
            app_id = str(ch_cfg.get("app_id") or "").strip()
            secret = (secrets_env.get("FEISHU_APP_SECRET") or os.getenv("FEISHU_APP_SECRET") or "").strip()
            if not app_id or not secret:
                continue
            domain = str(ch_cfg.get("feishu_domain") or "").strip()
            out["feishu"] = f"feishu|{app_id}|{secret}|{domain}"
        elif adapter_type == "wechat_clawbot":
            token = (secrets_env.get("WECHAT_BOT_TOKEN") or os.getenv("WECHAT_BOT_TOKEN") or "").strip()
            if not token:
                continue
            domain = str(ch_cfg.get("domain") or "").strip()
            bot_id = str(ch_cfg.get("bot_id") or "").strip()
            out["wechat"] = f"wechat|{token}|{domain}|{bot_id}"
    return out


__all__ = [
    "parse_channels",
    "create_adapters_from_config",
    "get_supported_types",
    "channel_signatures",
]
