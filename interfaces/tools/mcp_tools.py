"""MCP 工具桥 — 把 MCP Server 的工具注册进本平台工具注册表。

加载时机：agent 工具装载期（agent._ensure_tools 的模块清单里含本模块）。
对本机 local.yaml 里每个 mcp.servers 条目：
  1. 连接 server 枚举工具（失败仅告警跳过——server 挂了不拖垮整个 agent）；
  2. 每个工具注册为 mcp_<server>_<tool>（toolset="mcp"，异步），
     schema 由 MCP inputSchema 映射为 OpenAI parameters；
  3. 工具面（def app.yaml tools 列表）按名引用即可配给任意角色——
     未配置对应 server 时名字自动不出现（get_definitions 跳过）。

注册是幂等的（registry.register 覆盖同名），多实例/多 worker 重复加载安全。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("digital_life.mcp_tools")

registry = None  # import 时注入（见 _ensure_registry）


def _ensure_registry():
    global registry
    if registry is None:
        from interfaces.tools.registry import registry as _r

        registry = _r
    return registry


def _openai_schema(tool: dict) -> dict:
    """MCP inputSchema（JSON Schema）→ OpenAI function schema（扁平形态）。"""
    params = tool.get("inputSchema") or {"type": "object", "properties": {}}
    if not isinstance(params, dict) or params.get("type") != "object":
        params = {"type": "object", "properties": {}, **(
            {"required": params["required"]} if isinstance(params, dict) and params.get("required") else {})}
    return {
        "name": tool["_reg_name"],
        "description": (tool.get("description") or f"MCP tool {tool['name']}")[:800],
        "parameters": {
            "type": "object",
            "properties": params.get("properties") or {},
            **({"required": params["required"]} if params.get("required") else {}),
        },
    }


def _make_handler(server: str, tool: str):
    async def _handler(args: dict, **_) -> str:
        from infrastructure import mcp_client

        return await mcp_client.call_server_tool(server, tool, args or {})
    return _handler


def register_server(server: str) -> int:
    """枚举并注册某 MCP server 的全部工具。返回注册数；失败抛异常。"""
    import asyncio

    from infrastructure import mcp_client

    reg = _ensure_registry()
    tools = asyncio.run(mcp_client.list_server_tools(server))
    count = 0
    for t in tools:
        entry = {**t, "_reg_name": mcp_client.sanitize_tool_name(server, t["name"])}
        reg.register(
            name=entry["_reg_name"],
            toolset="mcp",
            schema=_openai_schema(entry),
            handler=_make_handler(server, t["name"]),
            check_fn=lambda s=server: mcp_client._server_cfg(s) is not None,
            is_async=True,
            emoji="🔌",
            max_result_size_chars=16_000,
        )
        count += 1
    logger.info("MCP tools registered server=%s tools=%d", server, count)
    return count


try:
    from infrastructure import mcp_client as _mcp_client

    for _server, _cfg in _mcp_client.mcp_servers_config().items():
        if _mcp_client._server_cfg(_server) is None:
            continue  # 配置不完整（缺 url/command/key 位）——静默跳过
        try:
            register_server(_server)
        except Exception as exc:  # noqa: BLE001 — 单 server 失败不拖垮工具装载
            logger.warning("MCP server %s tool listing failed: %s",
                           _server, exc)
except Exception:  # noqa: BLE001
    logger.warning("MCP tools module init failed", exc_info=True)
