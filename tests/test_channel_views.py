"""Channel views 账本单元测试。

覆盖 domain/lifecycle/channel_views.py：
- 显式 session_id 分桶、幂等 mark、has_viewed 查询
- session_id contextvar 兜底（monkey-patch）
- 并发安全（多线程 mark 同 session）
- 空 chat_id / 空 session_id 的安全放行语义
- reset_for_session 清理

这是 BUG #1 修复的核心账本——记录「本 session 查看过哪些通道历史」。
"""
from __future__ import annotations

import threading

from domain.lifecycle import channel_views


def setup_function() -> None:
    """每个测试前清空账本，避免互相污染（模块级全局状态）。"""
    with channel_views._lock:
        channel_views._viewed_channels.clear()


# ---------------------------------------------------------------------------
# 基础语义
# ---------------------------------------------------------------------------


class TestBasicSemantics:
    def test_unmarked_channel_is_not_viewed(self) -> None:
        """从未登记的通道 → 未查看。"""
        assert channel_views.has_viewed_channel("oc_new", session_id="tx_a") is False

    def test_mark_then_viewed(self) -> None:
        """登记一次后即已查看。"""
        channel_views.mark_channel_viewed("oc_a", session_id="tx_a")
        assert channel_views.has_viewed_channel("oc_a", session_id="tx_a") is True

    def test_mark_is_idempotent(self) -> None:
        """重复 mark 同一 (session, chat) 无副作用。"""
        channel_views.mark_channel_viewed("oc_a", session_id="tx_a")
        channel_views.mark_channel_viewed("oc_a", session_id="tx_a")
        channel_views.mark_channel_viewed("oc_a", session_id="tx_a")
        assert channel_views.has_viewed_channel("oc_a", session_id="tx_a") is True

    def test_multiple_chats_per_session(self) -> None:
        """一个 session 可查看多个通道。"""
        channel_views.mark_channel_viewed("oc_group1", session_id="tx_a")
        channel_views.mark_channel_viewed("ou_dm1", session_id="tx_a")
        channel_views.mark_channel_viewed("oc_group2", session_id="tx_a")
        assert channel_views.has_viewed_channel("oc_group1", session_id="tx_a") is True
        assert channel_views.has_viewed_channel("ou_dm1", session_id="tx_a") is True
        assert channel_views.has_viewed_channel("oc_group2", session_id="tx_a") is True
        assert channel_views.has_viewed_channel("oc_unseen", session_id="tx_a") is False


# ---------------------------------------------------------------------------
# Session 隔离
# ---------------------------------------------------------------------------


class TestSessionIsolation:
    def test_different_sessions_isolated(self) -> None:
        """session A 查看过 oc_a，不等于 session B 也看过。"""
        channel_views.mark_channel_viewed("oc_a", session_id="tx_a")
        assert channel_views.has_viewed_channel("oc_a", session_id="tx_b") is False

    def test_continuation_session_inherits(self) -> None:
        """续接场景：同一 session_id 复用 → viewed 集合天然继承。

        模拟 15min 内连续唤醒复用 tx_a 这个 session_id。
        """
        channel_views.mark_channel_viewed("oc_a", session_id="tx_a")
        # 续接后（同一 session_id），oc_a 仍记为已查看
        assert channel_views.has_viewed_channel("oc_a", session_id="tx_a") is True

    def test_new_session_does_not_inherit_others(self) -> None:
        """新 session_id 不看到别的 session 的查看记录。"""
        channel_views.mark_channel_viewed("oc_a", session_id="tx_old")
        assert channel_views.has_viewed_channel("oc_a", session_id="tx_new") is False


# ---------------------------------------------------------------------------
# 空值与 fail-open 语义
# ---------------------------------------------------------------------------


class TestEmptyValuesFailOpen:
    def test_empty_chat_id_mark_is_noop(self) -> None:
        """空 chat_id 不应污染账本。"""
        channel_views.mark_channel_viewed("", session_id="tx_a")
        assert channel_views.has_viewed_channel("oc_anything", session_id="tx_a") is False

    def test_empty_chat_id_has_viewed_returns_true(self) -> None:
        """空 chat_id 查询 → True（放行）。

        校验层对空目标 fail-open：空目标另有 express_to_human 的拒绝逻辑兜底，
        不应让本账本阻塞。
        """
        assert channel_views.has_viewed_channel("", session_id="tx_a") is True

    def test_empty_session_id_has_viewed_returns_true(self) -> None:
        """无法解析 session_id 时查询 → True（放行，避免误伤）。"""
        # 显式传空字符串，且不让 contextvar 兜底成功
        assert channel_views.has_viewed_channel("oc_test", session_id="") is True

    def test_empty_session_id_mark_is_noop(self) -> None:
        """无法解析 session_id 时的 mark 应跳过，不污染全局桶。"""
        channel_views.mark_channel_viewed("oc_test", session_id="")
        # 验证没有 key 为 "" 的桶被污染
        with channel_views._lock:
            assert "" not in channel_views._viewed_channels


# ---------------------------------------------------------------------------
# contextvar 兜底（session_id 缺省时）
# ---------------------------------------------------------------------------


class TestContextVarFallback:
    def test_session_id_falls_back_to_contextvar(self, monkeypatch) -> None:
        """未显式传 session_id 时，从 get_current_session_id() 兜底。"""
        monkeypatch.setattr(
            "infrastructure.config.get_current_session_id",
            lambda: "tx_from_contextvar",
            raising=False,
        )
        channel_views.mark_channel_viewed("oc_ctx")
        assert channel_views.has_viewed_channel("oc_ctx") is True
        assert channel_views.has_viewed_channel("oc_other") is False

    def test_contextvar_empty_fails_open(self, monkeypatch) -> None:
        """contextvar 也无 session_id 时 → fail-open（查=放行，记=noop）。"""
        monkeypatch.setattr(
            "infrastructure.config.get_current_session_id",
            lambda: "",
            raising=False,
        )
        assert channel_views.has_viewed_channel("oc_test") is True
        channel_views.mark_channel_viewed("oc_test")  # 应 noop，不崩
        assert channel_views.has_viewed_channel("oc_test") is True  # 仍 fail-open


# ---------------------------------------------------------------------------
# reset_for_session
# ---------------------------------------------------------------------------


class TestReset:
    def test_reset_clears_session_bucket(self) -> None:
        """reset 清空指定 session 的全部记录。"""
        channel_views.mark_channel_viewed("oc_a", session_id="tx_a")
        channel_views.mark_channel_viewed("oc_b", session_id="tx_a")
        channel_views.reset_for_session("tx_a")
        assert channel_views.has_viewed_channel("oc_a", session_id="tx_a") is False
        assert channel_views.has_viewed_channel("oc_b", session_id="tx_a") is False

    def test_reset_does_not_touch_other_sessions(self) -> None:
        """reset 一个 session 不影响其他 session。"""
        channel_views.mark_channel_viewed("oc_shared", session_id="tx_a")
        channel_views.mark_channel_viewed("oc_shared", session_id="tx_b")
        channel_views.reset_for_session("tx_a")
        assert channel_views.has_viewed_channel("oc_shared", session_id="tx_a") is False
        assert channel_views.has_viewed_channel("oc_shared", session_id="tx_b") is True

    def test_reset_empty_session_is_noop(self) -> None:
        """空 session_id 的 reset 不崩。"""
        channel_views.reset_for_session("")


# ---------------------------------------------------------------------------
# 并发安全
# ---------------------------------------------------------------------------


class TestConcurrency:
    def test_concurrent_marks_to_same_session(self) -> None:
        """多线程同时往同一 session mark 不同 chat，最终全部可见、无丢、无崩。"""
        session = "tx_concurrent"
        n_threads = 20
        chats_per_thread = 50

        def worker(tid: int) -> None:
            for i in range(chats_per_thread):
                channel_views.mark_channel_viewed(f"oc_{tid}_{i}", session_id=session)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 全部 n_threads * chats_per_thread 个不同 chat 都应可见
        for tid in range(n_threads):
            for i in range(chats_per_thread):
                assert channel_views.has_viewed_channel(f"oc_{tid}_{i}", session_id=session) is True
