# -*- coding: utf-8 -*-
"""实例级工具白名单（B 方案）行为锁定。

机制：apps/<iid>/config/app.yaml → tools.whitelist → AIAgent.tool_whitelist
  AND 叠加在 reason 层 toolset 过滤之上——白名单只会收紧。
  条目语义与 enabled_toolsets 对齐：toolset 名（整组）或工具名（单点）混合。
  条件暴露工具（rest preview 等躯体机制）不受白名单限制。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

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


# ── AIAgent AND 层 ────────────────────────────────────────────

def _make_agent(monkeypatch, whitelist):
    """轻量构造 AIAgent（不发起请求），只测 _enabled_tool_names。"""
    from infrastructure.ai.agent import AIAgent

    agent = AIAgent.__new__(AIAgent)
    agent.enabled_toolsets = None
    agent.tool_whitelist = whitelist
    agent._conditionally_revealed_tools = []
    return agent


def test_whitelist_none_keeps_all(monkeypatch):
    from infrastructure.ai import agent as agent_mod

    agent = _make_agent(monkeypatch, None)
    fake_all = ["app_a", "app_b", "app_c"]
    fake_registry = type(
        "R",
        (),
        {
            "get_all_tool_names": staticmethod(lambda schema_visible=None: fake_all),
            "get_toolset_for_tool": staticmethod(lambda n: {"app_a": "s1", "app_b": "s2", "app_c": "s1"}[n]),
        },
    )
    monkeypatch.setattr(agent_mod, "registry", fake_registry)
    assert agent._enabled_tool_names() == fake_all


def test_whitelist_toolset_name_passes_group(monkeypatch):
    from infrastructure.ai import agent as agent_mod

    agent = _make_agent(monkeypatch, ["s1"])
    fake_registry = type(
        "R",
        (),
        {
            "get_all_tool_names": staticmethod(lambda schema_visible=None: ["app_a", "app_b"]),
            "get_toolset_for_tool": staticmethod(lambda n: {"app_a": "s1", "app_b": "s2"}[n]),
        },
    )
    monkeypatch.setattr(agent_mod, "registry", fake_registry)
    assert agent._enabled_tool_names() == ["app_a"]


def test_whitelist_single_tool_name(monkeypatch):
    from infrastructure.ai import agent as agent_mod

    agent = _make_agent(monkeypatch, ["app_b"])
    fake_registry = type(
        "R",
        (),
        {
            "get_all_tool_names": staticmethod(lambda schema_visible=None: ["app_a", "app_b"]),
            "get_toolset_for_tool": staticmethod(lambda n: "s1"),
        },
    )
    monkeypatch.setattr(agent_mod, "registry", fake_registry)
    assert agent._enabled_tool_names() == ["app_b"]


def test_whitelist_mix_toolset_and_tool(monkeypatch):
    from infrastructure.ai import agent as agent_mod

    agent = _make_agent(monkeypatch, ["s1", "app_c"])
    fake_all = ["app_a", "app_b", "app_c"]
    fake_registry = type(
        "R",
        (),
        {
            "get_all_tool_names": staticmethod(lambda schema_visible=None: fake_all),
            "get_toolset_for_tool": staticmethod(lambda n: {"app_a": "s1", "app_b": "s2", "app_c": "s9"}[n]),
        },
    )
    monkeypatch.setattr(agent_mod, "registry", fake_registry)
    assert agent._enabled_tool_names() == ["app_a", "app_c"]


def test_conditionally_revealed_tools_bypass_whitelist(monkeypatch):
    """躯体机制工具（rest preview 等）不受业务白名单限制。"""
    from infrastructure.ai import agent as agent_mod

    agent = _make_agent(monkeypatch, ["app_a"])
    agent._conditionally_revealed_tools = ["rest", "process"]
    fake_registry = type(
        "R",
        (),
        {
            "get_all_tool_names": staticmethod(lambda schema_visible=None: ["app_a", "app_b"]),
            "get_toolset_for_tool": staticmethod(lambda n: "s1"),
        },
    )
    monkeypatch.setattr(agent_mod, "registry", fake_registry)
    names = agent._enabled_tool_names()
    assert "rest" in names and "process" in names
