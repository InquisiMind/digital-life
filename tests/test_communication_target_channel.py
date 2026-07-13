"""check_before_send 的「目标通道是否查看过」拦截分支测试（BUG #1 回归）。

覆盖 domain/lifecycle/communication.py 新增的关卡二：
- target_chat_id 未传 → 行为不变（向后兼容，纯走关卡一）
- target_chat_id 已查看 → 放行（return None）
- target_chat_id 未查看 → 拦截，返回历史来自【目标通道】而非 event 通道
- 拦截后自动 mark：重发同一目标通道不再被拦（防循环）
- 账本不可用时 fail-open（不阻塞发送）

关键回归场景：模拟贝塔 wake355——timer 触发、无事件来源 chat、想发到未查看的 DM 通道。
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from domain.lifecycle import channel_views, communication


def setup_function() -> None:
    """清空账本，避免互染。"""
    with channel_views._lock:
        channel_views._viewed_channels.clear()


@pytest.fixture
def no_unread_events():
    """让关卡一（未读消息）始终放行，专注测关卡二。

    patch pop_due_events / peek_signalled_events 都返回空。
    """
    with patch("domain.lifecycle.events.pop_due_events", return_value=[]), \
         patch("domain.lifecycle.session_events.peek_signalled_events", return_value=[]):
        yield


# ---------------------------------------------------------------------------
# 关卡一行为不变（向后兼容）
# ---------------------------------------------------------------------------


class TestBackwardCompat:
    def test_no_target_no_unread_returns_none(self, no_unread_events) -> None:
        """不传 target_chat_id 且无未读 → 放行（原有行为）。"""
        result = communication.check_before_send("hi", session_id="tx_a")
        assert result is None

    def test_target_empty_string_treated_as_none(self, no_unread_events) -> None:
        """target_chat_id="" 等同未传。"""
        result = communication.check_before_send("hi", session_id="tx_a", target_chat_id="")
        assert result is None


# ---------------------------------------------------------------------------
# 关卡二核心：目标通道已查看 / 未查看
# ---------------------------------------------------------------------------


class TestTargetChannelGate:
    def test_viewed_target_passes(self, no_unread_events) -> None:
        """目标通道已查看 → 放行。"""
        channel_views.mark_channel_viewed("oc_seen", session_id="tx_a")
        result = communication.check_before_send(
            "hello", session_id="tx_a", target_chat_id="oc_seen"
        )
        assert result is None

    def test_unviewed_target_blocked(self, no_unread_events) -> None:
        """目标通道未查看 → 拦截（BUG #1 核心场景）。"""
        with patch("domain.conversations.list_chat_messages", return_value=[
            {"sender_name": "alice", "text": "previous msg"},
        ]):
            result = communication.check_before_send(
                "hello", session_id="tx_a", target_chat_id="oc_unseen"
            )
        assert result is not None
        assert result["sent"] is False
        assert "尚未查看" in result["result_summary"] or "还没看过" in result["result_summary"]
        assert "previous msg" in result["recent_chat_log"]

    def test_block_uses_target_channel_not_event_channel(self, no_unread_events) -> None:
        """拦截时补的历史来自【目标通道】，而非 event 来源 chat。

        关键防退化：被群 A 唤醒、要发群 B（未看群 B）时，补的应是群 B 的历史。
        """
        calls: list[str] = []

        def fake_list(chat_id: str, limit: int = 10):
            calls.append(chat_id)
            if chat_id == "oc_target":
                return [{"sender_name": "target_user", "text": "target history"}]
            return []  # event chat 应回空，不应被用到

        with patch("domain.conversations.list_chat_messages", side_effect=fake_list):
            result = communication.check_before_send(
                "hello", session_id="tx_a", target_chat_id="oc_target"
            )
        # 拉历史用的 chat_id 必须是目标通道
        assert "oc_target" in calls
        assert result is not None
        assert "target history" in result["recent_chat_log"]


# ---------------------------------------------------------------------------
# 防循环：拦截后自动 mark，重发放行
# ---------------------------------------------------------------------------


class TestNoInfiniteLoop:
    def test_block_auto_marks_then_resend_passes(self, no_unread_events) -> None:
        """拦截补历史后自动登记；模型重发同一目标通道时不再被拦。"""
        with patch("domain.conversations.list_chat_messages", return_value=[
            {"sender_name": "bob", "text": "ctx"},
        ]):
            result = communication.check_before_send(
                "first attempt", session_id="tx_a", target_chat_id="oc_x"
            )
        assert result is not None  # 首次拦截
        assert channel_views.has_viewed_channel("oc_x", session_id="tx_a") is True

        # 重发：目标已登记 → 放行
        result2 = communication.check_before_send(
            "second attempt", session_id="tx_a", target_chat_id="oc_x"
        )
        assert result2 is None


# ---------------------------------------------------------------------------
# fail-open：账本不可用时不阻塞
# ---------------------------------------------------------------------------


class TestFailOpen:
    def test_ledger_import_failure_passes(self, no_unread_events) -> None:
        """channel_views import 失败时 fail-open（不阻塞发送）。"""
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "domain.lifecycle.channel_views":
                raise ImportError("simulated")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            result = communication.check_before_send(
                "hi", session_id="tx_a", target_chat_id="oc_any"
            )
        assert result is None


# ---------------------------------------------------------------------------
# BUG #1 回归：模拟贝塔 wake355 场景
# ---------------------------------------------------------------------------


class TestWake355Regression:
    """贝塔 wake355：timer 触发、无事件来源 chat、凭 stale 全局变量发未查看的 DM。

    期望：被关卡二拦截。
    """

    def test_timer_wake_to_unviewed_dm_blocked(self, no_unread_events) -> None:
        with patch("domain.conversations.list_chat_messages", return_value=[
            {"sender_name": "zhp", "text": "stale DM history"},
        ]):
            result = communication.check_before_send(
                "开业！", session_id="tx_timer_0630_2008", target_chat_id="oc_0eedde94test"
            )
        assert result is not None
        assert result["sent"] is False
        assert "stale DM history" in result["recent_chat_log"]
