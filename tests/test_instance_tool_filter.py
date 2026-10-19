# -*- coding: utf-8 -*-
"""实例级工具白名单行为锁定。

机制：apps/<iid>/config/app.yaml → tools.whitelist → AIAgent.tool_whitelist
  默认名单层（domain.tool_visibility.assemble ②）——白名单只会收紧。
  条目语义与 enabled_toolsets 对齐：toolset 名（整组）或工具名（单点）混合。
  通行证（rest preview 等 wake 级授予）不受白名单限制——授予优先。
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from infrastructure.config import get_tool_whitelist


@pytest.fixture()
def app_dir(tmp_path: Path, monkeypatch):
    """指 get_project_root()/apps 到临时目录。"""
    apps_root = tmp_path / "apps"
    iid = "test-instance-0001"
    cfg_dir = apps_root / iid / "config"
    cfg_dir.mkdir(parents=True)
    monkeypatch.setattr(
        "infrastructure.config.get_project_root", lambda: tmp_path
    )
    monkeypatch.setattr(
        "infrastructure.config.get_app_instance_id", lambda _iid=None: iid
    )
    return cfg_dir


def _write_cfg(cfg_dir: Path, yaml_text: str) -> None:
    (cfg_dir / "app.yaml").write_text(yaml_text, encoding="utf-8")


# ── config loader ─────────────────────────────────────────────

def test_no_config_returns_none(app_dir):
    assert get_tool_whitelist() is None


def test_empty_whitelist_returns_none(app_dir):
    _write_cfg(app_dir, "tools:\n  whitelist: []\n")
    assert get_tool_whitelist() is None


def test_missing_tools_section_returns_none(app_dir):
    _write_cfg(app_dir, "persona:\n  name: test\n")
    assert get_tool_whitelist() is None


def test_valid_whitelist_parsed_and_stripped(app_dir):
    _write_cfg(app_dir, "tools:\n  whitelist:\n    - actions\n    - app_stock_quote\n")
    assert get_tool_whitelist() == ["actions", "app_stock_quote"]


def test_broken_yaml_returns_none(app_dir):
    _write_cfg(app_dir, "tools: [unclosed\n  whitelist: oops\n")
    assert get_tool_whitelist() is None


# ── AIAgent 白名单层（经引擎）──────────────────────────────────

def _make_agent(monkeypatch, whitelist, toolsets=None, metadata=None):
    """轻量构造 AIAgent（不发起请求），只测 _enabled_tool_names。

    引擎在 interfaces.tools.registry 模块上取注册表（agent 模块属性不再
    被读），fake 打点换到那里；get_app_instance_id 钉死保证运行体确定。
    """
    from infrastructure.ai.agent import AIAgent

    metadata = metadata or {}
    agent = AIAgent.__new__(AIAgent)
    agent.enabled_toolsets = toolsets
    agent.tool_whitelist = whitelist
    agent.instance_id = "test-inst"
    reg = type(
        "R",
        (),
        {
            "get_all_tool_names": staticmethod(
                lambda include_retired=False: list(_FAKE_TOOLS)
            ),
            "get_toolset_for_tool": staticmethod(lambda n: _FAKE_TOOLSETS.get(n)),
            "get_tool_metadata": staticmethod(lambda n: metadata.get(n, {})),
        },
    )
    # 引擎在注册表模块上取单例；agent 模块还持有自己的顶层绑定——两处都要打
    mod = importlib.import_module("interfaces.tools.registry")
    monkeypatch.setattr(mod, "registry", reg)
    import infrastructure.ai.agent as agent_mod

    monkeypatch.setattr(agent_mod, "registry", reg)
    monkeypatch.setattr(
        "infrastructure.config.get_app_instance_id", lambda _iid=None: "test-inst"
    )
    return agent


_FAKE_TOOLS = ["app_a", "app_b", "app_c"]
_FAKE_TOOLSETS = {"app_a": "s1", "app_b": "s2", "app_c": "s9"}


def test_whitelist_none_keeps_all(monkeypatch):
    agent = _make_agent(monkeypatch, None)
    assert agent._enabled_tool_names() == _FAKE_TOOLS


def test_whitelist_toolset_name_passes_group(monkeypatch):
    agent = _make_agent(monkeypatch, ["s1"])
    assert agent._enabled_tool_names() == ["app_a"]


def test_whitelist_single_tool_name(monkeypatch):
    agent = _make_agent(monkeypatch, ["app_b"])
    assert agent._enabled_tool_names() == ["app_b"]


def test_whitelist_mix_toolset_and_tool(monkeypatch):
    agent = _make_agent(monkeypatch, ["s1", "app_c"])
    assert agent._enabled_tool_names() == ["app_a", "app_c"]


def test_wake_grant_bypasses_whitelist(monkeypatch, tmp_path):
    """通行证优先：白名单外的工具经本轮授予即可见（rest preview 等躯体机制）。"""
    from domain import tool_visibility as tv
    from infrastructure.persistence import tool_visibility as store

    monkeypatch.setenv("DIGITAL_LIFE_TOOL_VISIBILITY_DB", str(tmp_path / "tv.db"))
    store.reset_for_test()
    tv._wake_grants.clear()
    try:
        agent = _make_agent(monkeypatch, ["app_a"])
        tv.grant_wake("test-inst", ["app_b"], source="preview:rest")
        assert agent._enabled_tool_names() == ["app_a", "app_b"]
    finally:
        tv.clear_wake("test-inst")
        store.reset_for_test()
