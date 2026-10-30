"""project_todo_board 唤醒上下文 provider 回归测试。

历史缺陷：函数自诞生起引用了从未定义的模块常量 _TODO_ORDER/_TODO_ICON，
导致每次唤醒必抛 NameError、看板注入整段缺失（生产 worker 日志千余次）。
本测试锁定修复后的行为契约：不抛错、done/cancelled 不上板、排序=进行中→阻塞→待启动。
"""
from __future__ import annotations

from business.consulting import team_context


def _fake_env(monkeypatch, todos):
    monkeypatch.setattr(
        "infrastructure.persistence.services_registry.lookup_service",
        lambda rid: {"service_id": rid, "project_id": "prj-t", "display_name": "测试成员"},
        raising=True,
    )
    monkeypatch.setattr(
        team_context.projects_registry, "list_project_todos",
        lambda pid: todos, raising=True)
    monkeypatch.setattr(
        team_context.projects_registry, "list_project_members",
        lambda pid: [{"member_id": "svc-a", "role": "项目经理"},
                     {"member_id": "svc-b", "role": "研究员"}],
        raising=True)
    monkeypatch.setattr(
        team_context.projects_registry, "lookup_project",
        lambda pid: {"project_id": pid, "name": "测试项目"}, raising=True)


def test_board_renders_open_todos_without_error(monkeypatch):
    """挂项目的服务唤醒：看板正常返回，含排序与图标，不再 NameError。"""
    _fake_env(monkeypatch, [
        {"title": "启动项", "status": "open", "assignee_id": "svc-b"},
        {"title": "阻塞项", "status": "blocked", "assignee_id": "svc-a"},
        {"title": "进行项", "status": "in_progress", "assignee_id": "svc-b"},
        {"title": "已完成", "status": "done", "assignee_id": "svc-b"},
        {"title": "已取消", "status": "cancelled", "assignee_id": "svc-a"},
    ])
    board = team_context.project_todo_board("svc-a")
    assert board is not None and board["sys_tool"] == "todo_board"
    content = board["content"]
    # done/cancelled 不上板
    assert "已完成" not in content and "已取消" not in content
    # 排序：进行中 → 阻塞 → 待启动
    assert content.index("进行项") < content.index("阻塞项") < content.index("启动项")
    assert "◐" in content and "🔒" in content


def test_board_returns_none_outside_project(monkeypatch):
    """无项目服务：零注入（None），不抛错。"""
    monkeypatch.setattr(
        "infrastructure.persistence.services_registry.lookup_service",
        lambda rid: {"service_id": rid, "project_id": ""}, raising=True)
    assert team_context.project_todo_board("svc-x") is None
