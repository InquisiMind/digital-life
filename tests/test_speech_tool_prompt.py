"""L4「你的嘴」按工具面渲染测试（提示词与工具面同一事实源）。

2026-10-13 事故回归守卫：服务 PM 的工具清单里没有 express_to_human，
但 L4 曾硬编码它"是你的嘴"——模型照文盲调（连 chat_id 都没带）→ 未送达。
"""
from __future__ import annotations

from domain.identity.system_prompts import render_l4_lifecycle


def test_render_names_given_speech_tool():
    t = render_l4_lifecycle("send_chat_message")
    assert "`send_chat_message` 是你的嘴" in t
    assert "express_to_human" not in t
    assert "{speech_tool}" not in t


def test_render_default_is_instance_mouth():
    t = render_l4_lifecycle()
    assert "`express_to_human` 是你的嘴" in t


def test_service_runtime_resolves_send_chat(monkeypatch):
    from business.consulting import tool_face as tf

    monkeypatch.setattr(
        "infrastructure.persistence.services_registry.resolve_service_def",
        lambda rid: ("consulting-partner-def" if rid == "svc-x" else None),
    )
    assert tf.speech_tool("svc-x") == "send_chat_message"   # 服务 → 业务嘴
    assert tf.speech_tool("00000000-0000-0000-0000-000000000001") is None  # 实例 → 基座默认
    assert tf.speech_tool("") is None


def test_extension_resolver_wiring(monkeypatch):
    """activate.py 注册的 ref 经 extension.resolve_speech_tool 生效（默认兜底）。"""
    from infrastructure import extension as ext

    monkeypatch.setattr(
        "infrastructure.persistence.services_registry.resolve_service_def",
        lambda rid: ("consulting-coordinator-def" if rid == "svc-y" else None),
    )
    # 手动注册与 activate.py 相同的 ref（不触发完整 activate，测试隔离）
    ext._SPEECH_TOOL_RESOLVERS.clear()
    ext.register_speech_tool_resolver("business.consulting.tool_face:speech_tool")
    try:
        assert ext.resolve_speech_tool("svc-y") == "send_chat_message"
        assert ext.resolve_speech_tool("whatever") == "express_to_human"  # 默认兜底
        # 组合渲染：服务运行体的 L4 里嘴=send_chat_message
        t = render_l4_lifecycle(ext.resolve_speech_tool("svc-y"))
        assert "`send_chat_message` 是你的嘴" in t
    finally:
        ext._SPEECH_TOOL_RESOLVERS.clear()
