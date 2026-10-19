"""MCP 工具桥契约：配置解析、工具注册、调用回环、坏配置跳过。"""

from __future__ import annotations

import sys
import textwrap

import pytest

SERVER_SCRIPT = """
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("demo")

@mcp.tool()
def echo(text: str) -> str:
    \"\"\"Echo the text back.\"\"\"
    return "echo:" + text

mcp.run()
"""


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True)

    server = tmp_path / "demo_server.py"
    server.write_text(SERVER_SCRIPT, encoding="utf-8")

    def patch_cfg(cfg: dict):
        import yaml

        (cfg_dir / "local.yaml").write_text(
            yaml.safe_dump({"mcp": {"servers": cfg}}, allow_unicode=True),
            encoding="utf-8")

    monkeypatch.setattr("infrastructure.config.get_project_root", lambda: tmp_path)
    from business.consulting import api_admin  # noqa: F401  — 保证模块可导入
    return tmp_path, patch_cfg


def _fresh_mcp_tools():
    import importlib

    from interfaces.tools.registry import registry

    mod = importlib.import_module("interfaces.tools.mcp_tools")
    importlib.reload(mod)
    return mod, registry


def test_sanitize_tool_name():
    from infrastructure import mcp_client

    assert mcp_client.sanitize_tool_name("bocha", "web-search") == "mcp_bocha_web_search"
    assert mcp_client.sanitize_tool_name("exa.ai", "company.research") == "mcp_exa_ai_company_research"


def test_register_stdio_server_and_call(mcp_env, monkeypatch):
    tmp_path, patch_cfg = mcp_env
    patch_cfg({"demo": {"transport": "stdio",
                        "command": sys.executable,
                        "args": [str(tmp_path / "demo_server.py")]}})
    mod, registry = _fresh_mcp_tools()

    names = registry.get_all_tool_names()
    assert "mcp_demo_echo" in names

    schema = registry.get_schema("mcp_demo_echo")
    assert schema["parameters"]["properties"]["text"]["type"] == "string"
    assert schema["parameters"]["required"] == ["text"]

    result = registry.dispatch("mcp_demo_echo", {"text": "hi"})
    assert result == "echo:hi"


def test_incomplete_config_skipped(mcp_env, monkeypatch):
    tmp_path, patch_cfg = mcp_env
    patch_cfg({"broken": {"transport": "http"}})  # http 缺 url → 配置不完整
    mod, registry = _fresh_mcp_tools()
    assert "mcp_broken_anything" not in " ".join(registry.get_all_tool_names())
    assert mod._mcp_client.server_status()["broken"]["configured"] is False


def test_server_down_does_not_break_others(mcp_env, monkeypatch):
    tmp_path, patch_cfg = mcp_env
    # 指向不存在的 stdio 命令 → 枚举失败 → 该 server 静默跳过
    patch_cfg({"dead": {"transport": "stdio",
                        "command": sys.executable,
                        "args": ["-c", "import sys; sys.exit(3)"]}})
    mod, registry = _fresh_mcp_tools()  # 不应抛异常
    assert all(not n.startswith("mcp_dead") for n in registry.get_all_tool_names())
