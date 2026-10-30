"""MCP 客户端基建 — 连接 MCP Server，把外部工具动态暴露进工具注册表。

配置（config/local.yaml 的 mcp.servers 段；机器级密钥不入 git）：

  mcp:
    servers:
      bocha:                       # 服务名 → 工具名前缀 mcp_<server>_*
        transport: http            # http（远程托管 MCP）| stdio（本地子进程）
        url: https://mcp.bochaai.com/mcp
        headers:                   # 可选（鉴权头）
          Authorization: "Bearer ..."
      local-tools:
        transport: stdio
        command: /opt/engine-venv/bin/python
        args: [/path/to/server.py]
        env: {API_KEY: "..."}      # 可选，追加到子进程环境

行为：
  - interfaces/tools/mcp_tools.py 在工具装载期为本配置的每个 server
    枚举工具并注册为 mcp_<server>_<tool>（toolset="mcp"，异步）；
  - 调用为"按次连接"（HTTP 每调用一连接、stdio 每调用一进程）——
    搜索类低频调用足够；高频场景后续再换长连接会话；
  - server 不可达/未配 key → 枚举失败 → 该 server 的工具不注册
    （工具面里引用了也不会出现），不影响其他工具。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("digital_life.mcp")


def mcp_servers_config() -> dict[str, dict[str, Any]]:
    """读 config/local.yaml 的 mcp.servers 段（机器级；缺段返回空）。"""
    try:
        import yaml

        from infrastructure.config import get_project_root

        path = get_project_root() / "config" / "local.yaml"
        if not path.exists():
            return {}
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        servers = (data.get("mcp") or {}).get("servers") or {}
        return {str(k): v for k, v in servers.items() if isinstance(v, dict) and v}
    except Exception:  # noqa: BLE001 — 配置缺失/损坏不致命，等同无 MCP
        return []


def _server_cfg(server: str) -> dict[str, Any] | None:
    cfg = mcp_servers_config().get(server)
    if not cfg:
        return None
    transport = str(cfg.get("transport") or ("url" in cfg and "http") or "stdio")
    if transport == "http" and not cfg.get("url"):
        return None
    if transport == "stdio" and not cfg.get("command"):
        return None
    return {**cfg, "transport": transport}


def _result_text(result: Any) -> str:
    """MCP CallToolResult → 文本。

    content 文本块优先——协议规定带 structuredContent 时 content 必须携带
    完整内容，二者是同一份数据的两种视图；structuredContent 只在 content
    没有任何文本时兜底。两者都拼会把同一份 JSON 喂给模型两遍（FastMCP
    对纯字符串返回也包一层 {"result": ...} structuredContent）。
    """
    parts: list[str] = []
    has_text = False
    for block in (getattr(result, "content", None) or []):
        block_type = getattr(block, "type", None)
        if block_type == "text" and getattr(block, "text", None):
            parts.append(block.text)
            has_text = True
        else:
            import json

            parts.append(json.dumps(
                {"type": block_type or "unknown", "data": str(block)},
                ensure_ascii=False, default=str))
    if not has_text:
        structured = getattr(result, "structuredContent", None)
        if structured:
            import json

            parts.append(json.dumps(structured, ensure_ascii=False, default=str))
    if getattr(result, "isError", False):
        raise RuntimeError("; ".join(p for p in parts) or "MCP tool error")
    return "\n".join(parts) or "(empty result)"


async def _with_session(cfg: dict[str, Any], action) -> Any:
    """按传输方式建立会话并执行 action(session)；按次连接。"""
    from mcp import ClientSession

    if cfg["transport"] == "http":
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(
            cfg["url"], headers=cfg.get("headers") or None,
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await action(session)
    from mcp import StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = {**os.environ, **{str(k): str(v) for k, v in (cfg.get("env") or {}).items()}}
    params = StdioServerParameters(
        command=str(cfg["command"]),
        args=[str(a) for a in (cfg.get("args") or [])],
        env=env,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await action(session)


async def list_server_tools(server: str) -> list[dict[str, Any]]:
    """枚举某 server 的工具：[{name, description, inputSchema}]。连接失败抛异常。"""
    cfg = _server_cfg(server)
    if not cfg:
        raise ValueError(f"MCP server 未配置或配置不完整: {server}")

    async def action(session):
        resp = await session.list_tools()
        return [
            {"name": t.name,
             "description": t.description or "",
             "inputSchema": getattr(t, "inputSchema", None) or {"type": "object", "properties": {}}}
            for t in resp.tools
        ]

    return await _with_session(cfg, action)


async def call_server_tool(server: str, tool: str, args: dict[str, Any] | None) -> str:
    """调用某 server 的工具，返回文本结果。连接失败/工具报错抛异常。"""
    cfg = _server_cfg(server)
    if not cfg:
        raise ValueError(f"MCP server 未配置或配置不完整: {server}")

    async def action(session):
        return await session.call_tool(tool, args or {})

    return _result_text(await _with_session(cfg, action))


def server_status() -> dict[str, Any]:
    """配置摘要（服务台/诊断用）：server → {transport, configured}。"""
    return {
        name: {"transport": (cfg.get("transport")
                             or ("http" if cfg.get("url") else "stdio")),
               "configured": _server_cfg(name) is not None}
        for name, cfg in mcp_servers_config().items()
    }


def sanitize_tool_name(server: str, tool: str) -> str:
    import re

    return f"mcp_{re.sub(r'[^A-Za-z0-9_]', '_', server)}_{re.sub(r'[^A-Za-z0-9_]', '_', tool)}"
