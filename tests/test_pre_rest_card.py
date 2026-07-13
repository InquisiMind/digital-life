"""_build_pre_rest_card 睡前提示卡回归测试。

设计原则（见 helper docstring）：
- 不强制 rest 前必做某事，只透明呈现该处理的事
- 覆盖三类容易睡一觉就忘的：todo / 项目 / insight / 闹钟
- 失败时返回空串，绝不阻断 rest 流程
"""
from __future__ import annotations

import pytest


@pytest.fixture
def isolated_instance(tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch):
    """给 helper 一个隔离的实例上下文，避免污染真实 zero。"""
    from infrastructure.config import set_current_instance_id, reset_current_instance_id
    iid = "test-pre-rest-card-iid"
    token = set_current_instance_id(iid)
    yield iid
    reset_current_instance_id(token)


def test_returns_empty_string_when_nothing_to_report(isolated_instance, monkeypatch):
    """所有数据源为空时返回空串（绝不出错，不污染 rest 返回）。"""
    # stub 掉所有数据源都返回空
    monkeypatch.setattr(
        "domain.todos.crud.list_tasks", lambda **kw: [], raising=False
    )
    monkeypatch.setattr(
        "domain.project.loader.load_all_projects", lambda: {}, raising=False
    )
    monkeypatch.setattr(
        "domain.memory.memory.consciousness.runtime.read_insights", lambda **kw: "", raising=False
    )
    monkeypatch.setattr(
        "domain.lifecycle.alarms.list_pending_alarms", lambda *a, **kw: [], raising=False
    )

    from interfaces.tools.action_tools import _build_pre_rest_card
    out = _build_pre_rest_card()
    assert out == ""


def test_card_contains_in_progress_and_planned_count(isolated_instance, monkeypatch):
    """提示卡含 'N in_progress · M planned' 概要。"""
    # 模拟 2 个 in_progress + 3 个 planned
    def fake_list_tasks(status_filter=None, **kw):
        if status_filter == "in_progress":
            return [
                {"id": 1, "title": "任务1", "deadline": ""},
                {"id": 2, "title": "任务2", "deadline": ""},
            ]
        if status_filter == "planned":
            return [{"id": 3, "title": "p1"}, {"id": 4, "title": "p2"}, {"id": 5, "title": "p3"}]
        return []
    monkeypatch.setattr("domain.todos.crud.list_tasks", fake_list_tasks, raising=False)
    monkeypatch.setattr(
        "domain.project.loader.load_all_projects", lambda: {}, raising=False
    )
    monkeypatch.setattr(
        "domain.memory.memory.consciousness.runtime.read_insights", lambda **kw: "", raising=False
    )
    monkeypatch.setattr(
        "domain.lifecycle.alarms.list_pending_alarms", lambda *a, **kw: [], raising=False
    )

    from interfaces.tools.action_tools import _build_pre_rest_card
    out = _build_pre_rest_card()
    assert "📋 待办" in out
    assert "2 in_progress" in out
    assert "3 planned" in out


def test_card_flags_overdue_in_progress(isolated_instance, monkeypatch):
    """deadline < now 的 in_progress 必须被 ⚠️ 标红 + 列出。"""
    from datetime import datetime, timedelta, timezone
    past_iso = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

    def fake_list_tasks(status_filter=None, **kw):
        if status_filter == "in_progress":
            return [
                {"id": 999, "title": "过期任务", "deadline": past_iso},
            ]
        return []
    monkeypatch.setattr("domain.todos.crud.list_tasks", fake_list_tasks, raising=False)
    monkeypatch.setattr(
        "domain.project.loader.load_all_projects", lambda: {}, raising=False
    )
    monkeypatch.setattr(
        "domain.memory.memory.consciousness.runtime.read_insights", lambda **kw: "", raising=False
    )
    monkeypatch.setattr(
        "domain.lifecycle.alarms.list_pending_alarms", lambda *a, **kw: [], raising=False
    )

    from interfaces.tools.action_tools import _build_pre_rest_card
    out = _build_pre_rest_card()
    assert "⚠️" in out, "过期 in_progress 必须用 ⚠️ 标记"
    assert "#999" in out, "过期 task id 必须出现在卡片里"
    assert "过期任务" in out  # title


def test_card_contains_insight_count(isolated_instance, monkeypatch):
    """今日有 idea / doubt 类 insight 时，提示固化成 todo 避免丢失。"""
    monkeypatch.setattr(
        "domain.todos.crud.list_tasks", lambda **kw: [], raising=False
    )
    monkeypatch.setattr(
        "domain.project.loader.load_all_projects", lambda: {}, raising=False
    )
    monkeypatch.setattr(
        "domain.memory.memory.consciousness.runtime.read_insights",
        lambda **kw: (
            "- [idea] 2026-07-13T10:00:00+08:00 想法1\n"
            "- [doubt] 2026-07-13T11:00:00+08:00 疑问1\n"
            "- [warning] 2026-07-13T12:00:00+08:00 警告1\n"
        ),
        raising=False,
    )
    monkeypatch.setattr(
        "domain.lifecycle.alarms.list_pending_alarms", lambda *a, **kw: [], raising=False
    )

    from interfaces.tools.action_tools import _build_pre_rest_card
    out = _build_pre_rest_card()
    assert "💡 今日灵感碎片：3 条" in out
    assert "idea=1" in out
    assert "doubt=1" in out
    assert "warning=1" in out
    assert "todo create" in out or "task_note" in out  # 提示怎么记录


def test_card_lists_upcoming_alarms(isolated_instance, monkeypatch):
    """未来 8h 内的闹钟列出，避免冲突。"""
    from datetime import datetime, timedelta, timezone
    soon_iso = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    far_iso = (datetime.now(timezone.utc) + timedelta(hours=20)).isoformat()

    monkeypatch.setattr(
        "domain.todos.crud.list_tasks", lambda **kw: [], raising=False
    )
    monkeypatch.setattr(
        "domain.project.loader.load_all_projects", lambda: {}, raising=False
    )
    monkeypatch.setattr(
        "domain.memory.memory.consciousness.runtime.read_insights", lambda **kw: "", raising=False
    )

    call_count = {"n": 0}

    def fake_list_alarms(kind=None):
        call_count["n"] += 1
        if kind == "timer":
            return [{"id": 42, "fire_at": soon_iso, "payload_json": ""}]
        if kind == "routine":
            return [{"id": 100, "fire_at": far_iso, "payload_json": ""}]
        return []
    monkeypatch.setattr(
        "domain.lifecycle.alarms.list_pending_alarms", fake_list_alarms, raising=False
    )

    from interfaces.tools.action_tools import _build_pre_rest_card
    out = _build_pre_rest_card()
    assert "⏰" in out
    assert "#42" in out, "8h 内的 timer 应该列出"
    assert "#100" not in out, "20h 后的闹钟不应出现在卡里"


def test_card_never_raises_when_data_source_fails(isolated_instance, monkeypatch):
    """数据源抛异常时 helper 必须不崩、返回空（绝不让 rest 因提示卡挂掉）。"""
    def boom(*a, **kw):
        raise RuntimeError("simulated DB down")
    monkeypatch.setattr("domain.todos.crud.list_tasks", boom, raising=False)
    monkeypatch.setattr(
        "domain.project.loader.load_all_projects", boom, raising=False
    )
    monkeypatch.setattr(
        "domain.memory.memory.consciousness.runtime.read_insights", boom, raising=False
    )
    monkeypatch.setattr(
        "domain.lifecycle.alarms.list_pending_alarms", boom, raising=False
    )

    from interfaces.tools.action_tools import _build_pre_rest_card
    out = _build_pre_rest_card()
    assert isinstance(out, str)
    # 各数据源都炸了 → 静默返回空（不污染 rest 返回）


def test_card_header_always_starts_with_moon_emoji_when_nonempty(isolated_instance, monkeypatch):
    """非空时的卡头部有 🌙 月亮 + 「睡前提示卡」标志，让模型一眼识别。"""
    monkeypatch.setattr(
        "domain.todos.crud.list_tasks",
        lambda status_filter=None, **kw: [{"id": 1, "title": "x", "deadline": ""}] if status_filter == "in_progress" else [],
        raising=False,
    )
    monkeypatch.setattr(
        "domain.project.loader.load_all_projects", lambda: {}, raising=False
    )
    monkeypatch.setattr(
        "domain.memory.memory.consciousness.runtime.read_insights", lambda **kw: "", raising=False
    )
    monkeypatch.setattr(
        "domain.lifecycle.alarms.list_pending_alarms", lambda *a, **kw: [], raising=False
    )

    from interfaces.tools.action_tools import _build_pre_rest_card
    out = _build_pre_rest_card()
    assert "🌙 睡前提示卡" in out
