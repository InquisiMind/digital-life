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
    """MCP inputSchema（JSON Schema）→ OpenAI function schema（扁平形态）。

    tool["_note"] 非空时以 [使用注意] 后缀附加且保证不被截断——server 自带
    描述可能超长被截（reader 最长 1804 字符）甚至主动推荐有坑的用法
    （如"用 local_key 下载"），平台侧补注纠偏。
    """
    params = tool.get("inputSchema") or {"type": "object", "properties": {}}
    if not isinstance(params, dict) or params.get("type") != "object":
        params = {"type": "object", "properties": {}, **(
            {"required": params["required"]} if isinstance(params, dict) and params.get("required") else {})}
    base = tool.get("description") or f"MCP tool {tool['name']}"
    note = tool.get("_note") or ""
    suffix = f"\n[使用注意] {note}" if note else ""
    description = (base[:max(200, 800 - len(suffix))] + suffix)[:800]
    return {
        "name": tool["_reg_name"],
        "description": description,
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


def register_server(server: str, exclude: frozenset[str] = frozenset(),
                    instance_hidden: bool = False,
                    service_only: bool = False,
                    description_notes: dict[str, str] | None = None) -> int:
    """枚举并注册某 MCP server 的全部工具。返回注册数；失败抛异常。

    exclude：按裸名排除（如 dongjian_auth 这类"授权引导"工具——授权失败时
    业务工具结果自带 authorizationUrl，自愈不需要它， agent 面上不出现）。
    instance_hidden：服务型专属 server——实例默认面排除（工具可见性引擎旗标）。
    service_only：岗位授予制 server——从一切默认面排除，显式授予才可见
    （服务侧发放走岗位面清单，见 business/consulting/tool_face）。
    description_notes：裸工具名 → [使用注意] 补注，附加到描述末尾且保证可见
    （local.yaml mcp.servers 段 description_notes 键）。
    """
    from infrastructure import mcp_client
    from interfaces.tools.async_utils import run_async

    reg = _ensure_registry()
    # 走平台唯一 sync→async 桥：master/worker 常驻循环内也能安全枚举
    tools = run_async(mcp_client.list_server_tools(server), timeout=60.0)
    count = 0
    for t in tools:
        if t.get("name") in exclude:
            continue
        entry = {**t, "_reg_name": mcp_client.sanitize_tool_name(server, t["name"]),
                 "_note": (description_notes or {}).get(t.get("name") or "", "")}
        reg.register(
            name=entry["_reg_name"],
            toolset="mcp",
            schema=_openai_schema(entry),
            handler=_make_handler(server, t["name"]),
            check_fn=lambda s=server: mcp_client._server_cfg(s) is not None,
            is_async=True,
            emoji="🔌",
            max_result_size_chars=16_000,
            instance_hidden=instance_hidden,
            service_only=service_only,
        )
        count += 1
    logger.info("MCP tools registered server=%s tools=%d", server, count)
    return count


try:
    from infrastructure import mcp_client as _mcp_client

    for _server, _cfg in _mcp_client.mcp_servers_config().items():
        _cfg_v = _mcp_client._server_cfg(_server)
        if _cfg_v is None:
            continue  # 配置不完整（缺 url/command/key 位）——静默跳过
        _exclude = frozenset(str(x).strip() for x in (_cfg_v.get("exclude_tools") or []))
        _notes = {str(k): str(v) for k, v in (_cfg_v.get("description_notes") or {}).items()}
        try:
            register_server(_server, exclude=_exclude,
                            instance_hidden=bool(_cfg_v.get("instance_hidden")),
                            service_only=bool(_cfg_v.get("service_only")),
                            description_notes=_notes or None)
        except Exception as exc:  # noqa: BLE001 — 单 server 失败不拖垮工具装载
            logger.warning("MCP server %s tool listing failed: %s",
                           _server, exc)
except Exception:  # noqa: BLE001
    logger.warning("MCP tools module init failed", exc_info=True)
