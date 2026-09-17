"""默认 workdir 回归测试（workspace 浅层配置 v0.2 消费层版）。

历史 bug（7/5 贝塔事件 + 9/17 影子目录事件）→ 现在统一走引擎
``get_workspace_dir``（infrastructure/config，commit 0d0ad3f）：
- 有 active todo → todo workspace（工具层分支 1，保留不动）
- 注册实例 → app.yaml workspace_root / 全局 root_template（引擎分支 A/B）
- 未注册实例 → unregistered_fallback 三档（warn_repo_root / tmp / reject）
- ContextVar/env 全失效 → 工具层最后降级 repo 根
"""
from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import patch

import pytest


def _mk_fake_registered(iid: str, *, with_root: Path | None) -> Path:
    """在 apps/<iid>/config/app.yaml 造一个 mock 注册实例。

    with_root=None 模拟「无显式 workspace_root」→ 走全局模板分支。
    """
    inst = Path("apps") / iid
    cfg_dir = inst / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    lines = ["active: true", f"display_name: {iid}"]
    if with_root is not None:
        lines.append(f"workspace_root: {with_root}")
    (cfg_dir / "app.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return inst


def test_default_workdir_registered_instance(tmp_path: Path):
    """注册实例（显式 workspace_root）→ 默认 cwd 落到该 root。"""
    from infrastructure.config import set_current_instance_id, reset_current_instance_id
    from interfaces.tools.terminal_tool import _get_task_workspace_for_tool

    iid = "test-iid-reg-ws"
    root = tmp_path / "ws-reg"
    inst = _mk_fake_registered(iid, with_root=root)
    try:
        token = set_current_instance_id(iid)
        try:
            with patch(
                "domain.todos._infra.get_active_task_workspace",
                return_value=(None, None),
            ):
                task_id, workdir = _get_task_workspace_for_tool()
            assert task_id is None
            assert workdir == str(root), f"实际 workdir={workdir}"
            assert root.is_dir(), f"workspace 应自动创建: {root}"
        finally:
            reset_current_instance_id(token)
    finally:
        shutil.rmtree(inst, ignore_errors=True)


def test_default_workdir_template_branch(tmp_path: Path, monkeypatch):
    """注册但无显式 workspace_root → 全局 root_template 分支（交接点①回归）。"""
    import infrastructure.config as ic
    from infrastructure.config import set_current_instance_id, reset_current_instance_id
    from interfaces.tools.code_execution_tool import _get_task_workspace_for_tool

    tpl_root = tmp_path / "工作区"
    monkeypatch.setattr(
        ic, "_workspace_global_cfg",
        lambda: {
            "mode": "shallow",
            "root_template": str(tpl_root / "{instance_name}"),
            "unregistered_fallback": "warn_repo_root",
        },
    )

    iid = "test-iid-tpl-ws"
    inst = _mk_fake_registered(iid, with_root=None)
    try:
        token = set_current_instance_id(iid)
        try:
            with patch(
                "domain.todos._infra.get_active_task_workspace",
                return_value=(None, None),
            ):
                task_id, workdir = _get_task_workspace_for_tool()
            assert task_id is None
            assert workdir == str(tpl_root / iid), f"模板分支落点错: {workdir}"
            assert (tpl_root / iid).is_dir()
        finally:
            reset_current_instance_id(token)
    finally:
        shutil.rmtree(inst, ignore_errors=True)


def test_unregistered_instance_falls_back_to_repo_root():
    """未注册实例（无 config/app.yaml）→ warn_repo_root 档：repo 根，且不在
    apps/ 下出生任何影子目录（d88811d 影子目录防护语义延续）。"""
    from infrastructure.config import (
        set_current_instance_id, reset_current_instance_id, get_project_root,
    )
    from interfaces.tools.terminal_tool import _get_task_workspace_for_tool

    iid = "test-iid-ghost-ws"
    ghost = Path("apps") / iid
    assert not ghost.exists(), "前置：apps/ 下不能预置该 id"
    try:
        token = set_current_instance_id(iid)
        try:
            with patch(
                "domain.todos._infra.get_active_task_workspace",
                return_value=(None, None),
            ):
                task_id, workdir = _get_task_workspace_for_tool()
            assert task_id is None
            assert workdir == str(get_project_root()), (
                f"未注册应回落 repo 根，实际={workdir}"
            )
            assert not ghost.exists(), "未注册实例不得在 apps/ 出生影子目录"
        finally:
            reset_current_instance_id(token)
    finally:
        shutil.rmtree(ghost, ignore_errors=True)


def test_reject_policy_raises_clear_error(tmp_path: Path, monkeypatch):
    """unregistered_fallback=reject → WorkspaceRefusedError 被工具层转译为
    明确 RuntimeError，不静默降级。"""
    import infrastructure.config as ic
    from infrastructure.config import set_current_instance_id, reset_current_instance_id
    from interfaces.tools.terminal_tool import _get_task_workspace_for_tool

    monkeypatch.setattr(
        ic, "_workspace_global_cfg",
        lambda: {"mode": "shallow", "unregistered_fallback": "reject"},
    )
    iid = "test-iid-reject-ws"
    assert not (Path("apps") / iid).exists()
    token = set_current_instance_id(iid)
    try:
        with patch(
            "domain.todos._infra.get_active_task_workspace", return_value=(None, None)
        ):
            with pytest.raises(RuntimeError, match="reject"):
                _get_task_workspace_for_tool()
    finally:
        reset_current_instance_id(token)


def test_active_todo_workdir_takes_priority(monkeypatch):
    """有 active todo 时，task workspace 优先（工具层分支 1 保留）。"""
    from interfaces.tools.terminal_tool import _get_task_workspace_for_tool

    fake_task_ws = "/tmp/fake-task-workspace-12345"
    with patch(
        "domain.todos._infra.get_active_task_workspace",
        return_value=("task-xyz", Path(fake_task_ws)),
    ):
        task_id, workdir = _get_task_workspace_for_tool()
    assert task_id == "task-xyz"
    assert workdir == fake_task_ws


def test_fallback_to_repo_root_when_contextvar_missing(monkeypatch):
    """ContextVar/env 全失效 → 工具层最后降级（不阻断工具调用）。"""
    from interfaces.tools.terminal_tool import _get_task_workspace_for_tool
    from infrastructure.config import get_project_root

    with patch(
        "domain.todos._infra.get_active_task_workspace", return_value=(None, None)
    ), patch(
        "infrastructure.config.get_app_instance_id", side_effect=RuntimeError("no ctx")
    ):
        task_id, workdir = _get_task_workspace_for_tool()
    assert task_id is None
    assert workdir == str(get_project_root()), f"降级 workdir 应是项目根，实际={workdir}"


def test_scheduler_probe_does_not_mkdir(tmp_path: Path, monkeypatch):
    """scheduler 只读展示（§8.3）：probe 解析不建目录、不因未注册抛错。"""
    from domain.lifecycle import scheduler

    intro = scheduler._render_workspace_intro("ghost-id-no-such")
    assert "工作空间" in intro or "workspace" in intro.lower()
    # 未注册 id：不出生影子目录
    assert not (Path("apps") / "ghost-id-no-such").exists()


def test_no_off_by_one_parents_regression():
    """历史 off-by-one 防御：工具源码降级路径只允许 parents[2]（项目根）。"""
    import interfaces.tools.terminal_tool as tt_module
    import interfaces.tools.code_execution_tool as ce_module

    tt_src = Path(tt_module.__file__).read_text(encoding="utf-8")
    ce_src = Path(ce_module.__file__).read_text(encoding="utf-8")
    assert "parents[3]" not in tt_src, "terminal_tool 不应出现 parents[3] (off-by-one)"
    assert "parents[3]" not in ce_src, "code_execution_tool 不应出现 parents[3]"

def test_probe_never_raises_under_reject(monkeypatch):
    """回归（0d0ad3f→patch）：only_probe=True + reject 档 + 未注册 id 不得 raise。

    场景：scheduler wake 注入走 only_probe 只读展示，若 reject 抛
    WorkspaceRefusedError 且 scheduler 无兜底，wake 注入直接炸。
    probe 语义 = 展示用降级路径即可，永不 raise。
    """
    from infrastructure.config import get_workspace_dir, WorkspaceRefusedError, get_project_root
    import infrastructure.config as ic

    monkeypatch.setattr(
        ic, "_workspace_global_cfg",
        lambda: {"mode": "shallow", "unregistered_fallback": "reject"},
    )
    # probe：返回 repo root，不 raise
    p = get_workspace_dir("ghost-probe-iid", only_probe=True)
    assert p == get_project_root()
    # 非 probe：照常 raise（穿透语义给 tool 层转译）
    try:
        get_workspace_dir("ghost-probe-iid")
        raise AssertionError("expected WorkspaceRefusedError")
    except WorkspaceRefusedError:
        pass


def test_probe_tmp_policy_returns_tmp(monkeypatch):
    """probe + tmp 档：返回 tmp 路径不 mkdir（无副作用）。"""
    from infrastructure.config import get_workspace_dir
    import infrastructure.config as ic

    monkeypatch.setattr(
        ic, "_workspace_global_cfg",
        lambda: {"mode": "shallow", "unregistered_fallback": "tmp"},
    )
    p = get_workspace_dir("ghost-probe-tmp", only_probe=True)
    assert p.name == "ghost-probe-tmp" and not p.exists()
