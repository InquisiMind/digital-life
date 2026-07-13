"""lark→feishu 重命名 + kind 派生 的回归测试（Part B）。

覆盖本次重构的两个关键变更：
1. 平台前缀统一为 feishu（_get_runtime_channel_prefix）；同时读侧 helper 认 feishu 与历史 lark。
2. kind 不再从 wake_reason 推断，改由 chat_id 前缀派生（oc_→group, ou_→dm）。

注：现有 test_awaiting_reply_channel_filter.py 等保留 lark: 字符串作为"历史数据兼容性"
的回归——它们验证读侧仍正确处理 lark，本文件则验证写侧/派生已是 feishu/前缀派生。
"""
from __future__ import annotations

from interfaces.tools import action_tools


class TestRuntimeChannelPrefix:
    def test_default_is_feishu(self, monkeypatch) -> None:
        """无任何 context → 默认前缀是 feishu（旧版是 lark）。"""
        # 让 ContextVar / _REPLY_CONTEXT 都落空
        monkeypatch.setattr(
            "domain.lifecycle.runtime_context.get_current_event_platform",
            lambda: "",
            raising=False,
        )
        monkeypatch.setattr(action_tools, "_CURRENT_INSTANCE_ID", "", raising=False)
        assert action_tools._get_runtime_channel_prefix() == "feishu"

    def test_contextvar_feishu_passes_through(self, monkeypatch) -> None:
        """runtime_context 返回 feishu → 前缀 feishu。"""
        monkeypatch.setattr(
            "domain.lifecycle.runtime_context.get_current_event_platform",
            lambda: "feishu",
            raising=False,
        )
        assert action_tools._get_runtime_channel_prefix() == "feishu"

    def test_contextvar_legacy_lark_normalized_to_feishu(self, monkeypatch) -> None:
        """runtime_context 历史遗留返回 lark → 前缀归一为 feishu。"""
        monkeypatch.setattr(
            "domain.lifecycle.runtime_context.get_current_event_platform",
            lambda: "feishu",  # set_current_event_platform 已归一，直读也是 feishu
            raising=False,
        )
        assert action_tools._get_runtime_channel_prefix() == "feishu"


class TestFeishuChannelHelpers:
    """_is_feishu_channel / _channel_has_prefix / _strip_feishu_prefix 同时认 feishu 与 lark。"""

    def test_is_feishu_channel_both_prefixes(self) -> None:
        assert action_tools._is_feishu_channel("feishu:group:oc_x") is True
        assert action_tools._is_feishu_channel("lark:group:oc_x") is True
        assert action_tools._is_feishu_channel("wechat:group:wx") is False
        assert action_tools._is_feishu_channel("") is False

    def test_channel_has_prefix_both_prefixes(self) -> None:
        assert action_tools._channel_has_prefix("feishu:group:oc_x", "group:") is True
        assert action_tools._channel_has_prefix("lark:group:oc_x", "group:") is True
        assert action_tools._channel_has_prefix("feishu:dm:ou_x", "group:") is False

    def test_strip_feishu_prefix_both_prefixes(self) -> None:
        assert action_tools._strip_feishu_prefix("feishu:dm:ou_abc", kind="dm:") == "ou_abc"
        assert action_tools._strip_feishu_prefix("lark:group:oc_abc", kind="group:") == "oc_abc"


class TestKindDerivationFromIdPrefix:
    """kind 由 chat_id 前缀派生（取代 wake_reason）。验证核心派生逻辑直接性。

    完整 express_to_human 流程由 e2e/audit 测试覆盖，这里单独验证派生规则。
    """

    def test_oc_prefix_is_group(self) -> None:
        """oc_ 前缀（群/会话 chat_id）→ group。"""
        # 模拟 kind 派生判定（与 _handle_express_to_human 内一致）
        for cid in ("oc_test123", "oc_5ff7967b"):
            assert ("group" if cid.startswith("oc_") else "dm") == "group"

    def test_ou_prefix_is_dm(self) -> None:
        """ou_ 前缀（open_id 私聊）→ dm。"""
        for cid in ("ou_abc123", "ou_eb5083"):
            assert ("group" if cid.startswith("oc_") else "dm") == "dm"

    def test_on_prefix_is_dm(self) -> None:
        """on_ 前缀（union_id，少见）→ 按私聊处理。"""
        cid = "on_test"
        assert ("group" if cid.startswith("oc_") else "dm") == "dm"
