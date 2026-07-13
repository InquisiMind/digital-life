"""默认 workdir 修复回归测试。

历史 bug（7/5 贝塔事件）：terminal/execute_code 在"无 active todo"时回退到
项目根（且 terminal_tool 的代码里还有 parents[3] 的 off-by-one → 实际是
探索项目/ 数字生命/ 的上一层），让 agent ad-hoc 写盘时越界把文章写到
项目根 articles/。

修复：默认 cwd 改为 ``apps/<instance_id>/workspace/``，让 agent 天然在
自己的 sandbox 里。仅 ContextVar 未设时才最后降级到项目根。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch


def test_default_workdir_is_instance_workspace(tmp_path: Path, monkeypatch):
    """无 active todo + ContextVar 已设 → 默认 cwd 落到 apps/<iid>/workspace/。"""
    from infrastructure.config import set_current_instance_id, reset_current_instance_id, get_project_root
    from interfaces.tools.terminal_tool import _get_task_workspace_for_tool

    # 准备一个 fake apps/<iid>/ 目录
    iid = "test-iid-default-ws"
    project_root = get_project_root()
    fake_apps = project_root / "apps" / iid
    fake_apps.mkdir(parents=True, exist_ok=True)
    try:
        token = set_current_instance_id(iid)
        try:
            # 模拟无 active todo
            with patch(
                "domain.todos._infra.get_active_task_workspace",
                return_value=(None, None),
            ):
                task_id, workdir = _get_task_workspace_for_tool()
            assert task_id is None
            # workdir 应是 apps/<iid>/workspace/
            assert workdir and workdir.endswith(f"apps/{iid}/workspace"), (
                f"实际 workdir={workdir}"
            )
            # 目录必须被自动创建
            assert Path(workdir).is_dir(), f"workspace 应自动创建: {workdir}"
        finally:
            reset_current_instance_id(token)
    finally:
        # 清理：删 test-iid-default-ws 目录
        import shutil
        shutil.rmtree(fake_apps, ignore_errors=True)


def test_default_workdir_for_code_execution_tool(tmp_path: Path, monkeypatch):
    """同上，对 execute_code 路径。"""
    from infrastructure.config import set_current_instance_id, reset_current_instance_id, get_project_root
    from interfaces.tools.code_execution_tool import _get_task_workspace_for_tool

    iid = "test-iid-codeexec-ws"
    project_root = get_project_root()
    fake_apps = project_root / "apps" / iid
    fake_apps.mkdir(parents=True, exist_ok=True)
    try:
        token = set_current_instance_id(iid)
        try:
            with patch(
                "domain.todos._infra.get_active_task_workspace",
                return_value=(None, None),
            ):
                task_id, workdir = _get_task_workspace_for_tool()
            assert task_id is None
            assert workdir and workdir.endswith(f"apps/{iid}/workspace"), (
                f"实际 workdir={workdir}"
            )
            assert Path(workdir).is_dir()
        finally:
            reset_current_instance_id(token)
    finally:
        import shutil
        shutil.rmtree(fake_apps, ignore_errors=True)


def test_active_todo_workdir_takes_priority(monkeypatch):
    """有 active todo 时，task workspace 优先于 instance workspace。"""
    from interfaces.tools.terminal_tool import _get_task_workspace_for_tool

    fake_task_ws = "/tmp/fake-task-workspace-12345"
    with patch(
        "domain.todos._infra.get_active_task_workspace",
        return_value=("task-xyz", Path(fake_task_ws)),
    ):
        task_id, workdir = _get_task_workspace_for_tool()
    assert task_id == "task-xyz"
    assert workdir == fake_task_ws


def test_fallback_to_repo_root_when_contextvar_missing(tmp_path: Path, monkeypatch):
    """ContextVar 未设 + 无 todo → 降级到项目根（不阻断工具调用）。

    这是防御性降级：不抛异常，避免让 agent 在边界场景下完全无手可用。
    正常运行场景下 ContextVar 总是被 middleware 设置。
    """
    from interfaces.tools.terminal_tool import _get_task_workspace_for_tool
    from infrastructure.config import get_project_root

    # 既无 task，又调用 get_app_instance_id 抛异常 → 落到最后降级
    with patch(
        "domain.todos._infra.get_active_task_workspace", return_value=(None, None)
    ), patch(
        "infrastructure.config.get_app_instance_id", side_effect=RuntimeError("no ctx")
    ):
        task_id, workdir = _get_task_workspace_for_tool()
    assert task_id is None
    # 应是项目根
    project_root = str(get_project_root())
    assert workdir == project_root, f"降级 workdir 应是项目根，实际={workdir}"


def test_terminal_tool_repo_root_uses_correct_parents_level():
    """历史 off-by-one bug 防御：terminal_tool.__file__ 在 interfaces/tools/，
    parents[2] = 项目根，parents[3] = 探索项目/（项目根的上一层）。
    本测试锁定降级路径用的是 parents[2]。
    """
    from pathlib import Path
    import interfaces.tools.terminal_tool as tt_module
    import interfaces.tools.code_execution_tool as ce_module

    project_root_parents_2 = Path(tt_module.__file__).resolve().parents[2].name
    project_root_parents_3 = Path(tt_module.__file__).resolve().parents[3].name
    assert project_root_parents_2 == "数字生命", (
        f"parents[2] 应是 '数字生命'，实际={project_root_parents_2}"
    )
    # parents[3] 应是上一层（探索项目），不是项目根本身
    assert project_root_parents_3 != "数字生命", "off-by-one bug: parents[3] 不是项目根本身"

    # 两个工具源码里降级路径都用 parents[2]
    tt_src = Path(tt_module.__file__).read_text(encoding="utf-8")
    ce_src = Path(ce_module.__file__).read_text(encoding="utf-8")
    assert "parents[2]" in tt_src, "terminal_tool 应使用 parents[2]"
    assert "parents[2]" in ce_src, "code_execution_tool 应使用 parents[2]"
    assert "parents[3]" not in tt_src, "terminal_tool 不应再有 parents[3] (off-by-one)"
    assert "parents[3]" not in ce_src, "code_execution_tool 不应再有 parents[3] (off-by-one)"
