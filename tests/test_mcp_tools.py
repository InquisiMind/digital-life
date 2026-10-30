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


def test_register_server_survives_running_event_loop(mcp_env):
    """master/worker 都是长驻事件循环进程——装载期枚举必须能在循环内存活。

    旧实现在循环线程里 asyncio.run() 必炸（RuntimeError），master 工具目录
    与 worker 装载就此双双静默失败（2026-10-19）。
    """
    import asyncio

    tmp_path, patch_cfg = mcp_env
    patch_cfg({"demo": {"transport": "stdio",
                        "command": sys.executable,
                        "args": [str(tmp_path / "demo_server.py")]}})
    mod, registry = _fresh_mcp_tools()
    registry.deregister("mcp_demo_echo")

    async def _master_like_wake():
        mod.register_server("demo")  # 在运行中的 loop 线程内同步枚举

    asyncio.run(_master_like_wake())
    assert "mcp_demo_echo" in registry.get_all_tool_names()


def test_async_tool_dispatch_inside_event_loop(mcp_env):
    """is_async 工具在运行中的 loop 线程内 dispatch 必须可用（桥的 loop 分支）。

    旧桥 run_coroutine_threadsafe 指回自身 loop 再阻塞 = 纯死锁；用线程+
    join 超时包装，死锁表现为可断言的失败而不是测试卡死。
    """
    import asyncio
    import threading

    tmp_path, patch_cfg = mcp_env
    patch_cfg({"demo": {"transport": "stdio",
                        "command": sys.executable,
                        "args": [str(tmp_path / "demo_server.py")]}})
    mod, registry = _fresh_mcp_tools()

    box: dict = {}

    def _wake():
        async def _main():
            return registry.dispatch("mcp_demo_echo", {"text": "hi"})
        box["result"] = asyncio.run(_main())

    th = threading.Thread(target=_wake, name="loop-thread-wake")
    th.start()
    th.join(timeout=30)
    assert not th.is_alive(), "loop 线程内 dispatch 死锁（桥的 loop 分支坏了）"
    assert box["result"] == "echo:hi"


def test_exclude_tools_keeps_auth_helper_off_the_face(mcp_env):
    """exclude_tools 在注册期跳过（如 dongjian_auth 授权引导工具）——
    名字从根上不进注册表，自然不进任何 agent 的工具面。"""
    tmp_path, patch_cfg = mcp_env
    patch_cfg({"demo": {"transport": "stdio",
                        "command": sys.executable,
                        "args": [str(tmp_path / "demo_server.py")],
                        "exclude_tools": ["echo"]}})
    mod, registry = _fresh_mcp_tools()
    # 前序用例会在共享注册表里留下 demo 工具——清掉再验证"exclude 后不再注册"
    for n in list(registry.get_all_tool_names()):
        if n.startswith("mcp_demo_"):
            registry.deregister(n)
    assert "mcp_demo_echo" not in registry.get_all_tool_names()

    # 面上没有 = dispatch 也明确拒绝（不是"看不见但调得动"）
    assert "Unknown tool" in registry.dispatch("mcp_demo_echo", {"text": "hi"})


def test_instance_hidden_metadata_reaches_registry(mcp_env):
    """instance_hidden 服务专属 server：旗标随注册写入（引擎按旗标排除实例）。"""
    tmp_path, patch_cfg = mcp_env
    patch_cfg({"demo": {"transport": "stdio",
                        "command": sys.executable,
                        "args": [str(tmp_path / "demo_server.py")],
                        "instance_hidden": True}})
    mod, registry = _fresh_mcp_tools()
    assert registry.get_tool_metadata("mcp_demo_echo").get("instance_hidden") is True


def test_service_only_flag_reaches_registry(mcp_env):
    """service_only 岗位授予制 server：旗标随注册写入（引擎按旗标从一切
    默认面排除，显式授予才可见——如企业研究员专属的年报/招股书工具）。"""
    tmp_path, patch_cfg = mcp_env
    patch_cfg({"demo": {"transport": "stdio",
                        "command": sys.executable,
                        "args": [str(tmp_path / "demo_server.py")],
                        "service_only": True}})
    mod, registry = _fresh_mcp_tools()
    assert registry.get_tool_metadata("mcp_demo_echo").get("service_only") is True
    registry.deregister("mcp_demo_echo")  # 共享注册表清场，不污染后续等价性闸


def test_description_notes_survive_truncation(mcp_env):
    """description_notes 以 [使用注意] 后缀附加且保证可见。

    server 自带描述可能超长被截（reader 最长 1804 字符）甚至主动推荐有坑
    的用法——补注必须挤掉原文而不是被原文截断吞掉。
    """
    tmp_path, patch_cfg = mcp_env
    patch_cfg({"demo": {"transport": "stdio",
                        "command": sys.executable,
                        "args": [str(tmp_path / "demo_server.py")],
                        "description_notes": {"echo": "首次下载必须用日期+关键词定位；local_key 仅能重取已下载过的公告。"}}})
    mod, registry = _fresh_mcp_tools()
    desc = registry.get_schema("mcp_demo_echo")["description"]
    assert "[使用注意]" in desc
    assert desc.endswith("已下载过的公告。")  # 补注完整保留在末尾
    assert len(desc) <= 800
    registry.deregister("mcp_demo_echo")  # 共享注册表清场，不污染后续等价性闸


def test_role_face_grants_declared_mcp_tools(mcp_env):
    """岗位面点名的协作组外工具随 source=role 发放；未注册的条目落空。

    apply_role_face 的协作交集数学不变（没点名的协作工具不漫游），
    组外点名（mcp_* 等授予制工具）在注册表里有才发——对应 server 未
    配置时声明自动失效，配置与实际不漂移。
    """
    tmp_path, patch_cfg = mcp_env
    patch_cfg({"demo": {"transport": "stdio",
                        "command": sys.executable,
                        "args": [str(tmp_path / "demo_server.py")],
                        "service_only": True}})
    _fresh_mcp_tools()

    def_dir = tmp_path / "apps" / "researcher-def" / "config"
    def_dir.mkdir(parents=True)
    (def_dir / "app.yaml").write_text("runtime_kind: definition\n", encoding="utf-8")

    from business.consulting.tool_face import apply_role_face
    from domain import tool_visibility
    from domain.service import create_service

    svc = create_service("researcher-def", display_name="研究员")["service_id"]
    apply_role_face(svc, ["send_chat_message", "mcp_demo_echo", "mcp_ghost_missing"])

    grants = tool_visibility.durable_grants(svc).get("role", [])
    assert "mcp_demo_echo" in grants  # 已注册的组外点名 → 发放
    assert "mcp_ghost_missing" not in grants  # 未注册 → 落空
    assert "send_chat_message" in grants  # 协作交集数学不变
    assert "sense_project_peers" not in grants  # 未点名的协作工具不漫游

    from interfaces.tools.registry import registry

    registry.deregister("mcp_demo_echo")  # 共享注册表清场，不污染后续等价性闸


def test_result_text_prefers_content_over_structured_duplicate():
    """content 文本与 structuredContent 是同一份数据的两种视图——只取 content。

    FastMCP 对纯字符串返回也包一层 {"result": <同一份 JSON>} 的
    structuredContent，两者都拼会把查询结果喂给模型两遍
    （list_cninfo_announcements 实测 720→1541 字节）。
    """
    from types import SimpleNamespace

    from infrastructure.mcp_client import _result_text

    twin = '{"status": "ok", "data": [1, 2]}'
    both_views = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=twin)],
        structuredContent={"result": twin},
        isError=False)
    assert _result_text(both_views) == twin  # 不重复

    # content 没有文本时 structuredContent 兜底（协议允许的形态）
    bare = SimpleNamespace(content=[], structuredContent={"n": 1}, isError=False)
    assert _result_text(bare) == '{"n": 1}'
