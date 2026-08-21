"""/new 会话翻篇测试（session 实体状态语义）。

实体模型（2026-08-21 重构）：
  - /new = 对 session 实体的关闭操作：end_session(user_reset)
  - 接续判定 = 实体终态查询：上一个终态 session 的 end_reason 不可接续
    （user_reset）或超出 15min 窗口 → 开新 session
  - 与触发唤醒的事件类型无关（语音/群/私聊/timer 都做同一实体判定）

取代旧的 ContextVar force_new 标志（链路伪状态，跨事件线程即丢——
16:23 实证：/new 后 9 秒的语音唤醒仍接续旧 session）。
"""
from __future__ import annotations

import pytest

from domain.lifecycle import scheduler
from domain.lifecycle.scheduler import (
    _check_continuation,
    close_session_user_reset,
)


@pytest.fixture(autouse=True)
def _clean():
    yield
    scheduler._last_session_end.pop("test-ent-iid", None)
    scheduler._last_session_end.pop("test-ent2-iid", None)


def _set_prev(iid, sid, reason="", minutes_ago=0.0):
    from domain.lifecycle.clock import now_dt
    from datetime import timedelta
    scheduler._last_session_end[iid] = {
        "session_id": sid,
        "at": now_dt() - timedelta(minutes=minutes_ago),
        "end_reason": reason,
    }


def test_normal_continuation_within_window():
    """completed 终态 + 窗口内 → 接续。"""
    _set_prev("test-ent-iid", "s1", "completed")
    assert _check_continuation("test-ent-iid") == "s1"


def test_user_reset_blocks_continuation():
    """user_reset 终态 → 不接续（/new 生效的实体语义）。"""
    _set_prev("test-ent-iid", "s1", "user_reset")
    assert _check_continuation("test-ent-iid") is None


def test_expired_window_blocks_continuation():
    """窗口外 → 不接续（原有行为保持）。"""
    _set_prev("test-ent-iid", "s1", "completed", minutes_ago=30)
    assert _check_continuation("test-ent-iid") is None


def test_error_reason_still_continues():
    """error 终态（如 429）在窗口内仍接续——只有 user_reset 翻篇。"""
    _set_prev("test-ent-iid", "s1", "error:429")
    assert _check_continuation("test-ent-iid") == "s1"


def test_no_prev_session_returns_none():
    scheduler._last_session_end.pop("test-ent2-iid", None)
    assert _check_continuation("test-ent2-iid-none") is None


def test_close_user_reset_flips_memory_state(monkeypatch, tmp_path):
    """close_session_user_reset 把内存终态改为 user_reset → 接续断开。"""
    _set_prev("test-ent-iid", "s1", "completed")
    assert _check_continuation("test-ent-iid") == "s1"

    class FakeDB:
        def __init__(self):
            self.calls = []

        @property
        def _conn(self):
            class C:
                def execute(self, *a, **k):
                    class R:
                        id = "s1"
                        ended_at = 123.0
                        def __getitem__(self, key):
                            return getattr(self, key)
                        def fetchone(self):
                            return self
                    return R()
            return C()

        def end_session(self, sid, reason, **k):
            self.calls.append((sid, reason))

    fake = FakeDB()
    monkeypatch.setattr(
        "infrastructure.ai.session_db.SessionDB", lambda: fake)

    sid = close_session_user_reset("test-ent-iid")
    assert sid == "s1"
    assert fake.calls == [("s1", "user_reset")]
    assert _check_continuation("test-ent-iid") is None


def test_parse_new_command_strips_prefix():
    text = "/new 帮我查个东西"
    assert text.lstrip().startswith("/new")
    stripped = text.lstrip()[4:].strip()
    assert stripped == "帮我查个东西"


def test_parse_new_command_empty_body():
    text = "/new"
    stripped = text.lstrip()[4:].strip()
    assert stripped == ""
