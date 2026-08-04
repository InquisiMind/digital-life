"""薄调度层 — LLM 工具入口。

不持有业务逻辑，只做：
  1. 从 args 提取参数
  2. 调 domain/capability/lifecycle 完成注册
  3. 格式化 JSON 返回

真正的注册逻辑（安全检查 / 文件写入 / manifest / 热加载）在
domain/capability/lifecycle.py。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from domain.capability import lifecycle
from interfaces.tools.registry import registry
from interfaces.tools.sense_tools import _burn, _j

logger = logging.getLogger(__name__)


def _get_instance_id() -> str:
    try:
        from infrastructure.config import get_app_instance_id
        iid = get_app_instance_id()
        if iid:
            return iid
    except Exception:
        pass
    import os as _os
    return _os.environ.get("DIGITAL_LIFE_INSTANCE_ID", "")


# ── register_tool ────────────────────────────────────────────────────────────


def _handle_register_tool(args: Dict[str, Any], **kwargs: Any) -> str:
    _burn(0.2)
    result = lifecycle.register_tool(
        name=(args.get("name") or "").strip(),
        description=(args.get("description") or "").strip(),
        parameters=args.get("parameters") or {},
        code=args.get("code") or "",
        scope=(args.get("scope") or "personal").strip(),
        project_id=(args.get("project_id") or "").strip(),
        emoji=(args.get("emoji") or "🔧").strip(),
        instance_id=_get_instance_id(),
    )
    return _j(result)


registry.register(
    name="register_tool",
    toolset="actions",
    schema={
        "name": "register_tool",
        "description": (
            "注册你自己写的新工具。提供工具名、描述、JSON schema 参数定义、"
            "Python handler 代码。写完热加载到 registry，下一次 LLM call 你能就能调它。"
            "scope: personal（仅自己）/ project（成员共享，需 project_id）/ shared（全实例可选启用）。"
            "系统强制加 app_ 前缀避免和系统工具撞名。"
            "handler 签名：def handler(args: dict, **kwargs) -> str"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "parameters": {"type": "object", "description": "OpenAI-style JSON schema"},
                "code": {"type": "string", "description": "handler 函数体（不含 def 行）"},
                "scope": {"type": "string", "enum": ["personal", "project", "shared"], "default": "personal"},
                "project_id": {"type": "string"},
                "emoji": {"type": "string"},
            },
            "required": ["name", "description", "parameters", "code"],
        },
    },
    handler=_handle_register_tool,
    check_fn=lambda: True,
    emoji="🛠️",
)


# ── register_skill ───────────────────────────────────────────────────────────


def _handle_register_skill(args: Dict[str, Any], **kwargs: Any) -> str:
    _burn(0.2)
    result = lifecycle.register_skill(
        name=(args.get("name") or "").strip(),
        content=args.get("content") or "",
        scope=(args.get("scope") or "personal").strip(),
        project_id=(args.get("project_id") or "").strip(),
        instance_id=_get_instance_id(),
    )
    return _j(result)


registry.register(
    name="register_skill",
    toolset="actions",
    schema={
        "name": "register_skill",
        "description": (
            "注册方法论 skill（markdown）。写完后下次 wake 在 skill_index 里能看到，"
            "需要完整内容时调 skill_view('<name>')。"
            "scope: personal / project / shared。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "content": {"type": "string", "description": "markdown 方法论内容"},
                "scope": {"type": "string", "enum": ["personal", "project", "shared"], "default": "personal"},
                "project_id": {"type": "string"},
            },
            "required": ["name", "content"],
        },
    },
    handler=_handle_register_skill,
    check_fn=lambda: True,
    emoji="📚",
)


# ── sense_my_tools ───────────────────────────────────────────────────────────


def _handle_sense_my_tools(args: Dict[str, Any], **kwargs: Any) -> str:
    """List all tools and skills the instance has registered."""
    _burn(0.1)
    from domain.capability.store import list_my_tools_and_skills
    from interfaces.tools.registry import registry
    inv = list_my_tools_and_skills(_get_instance_id())
    # 校验: manifest 有但 registry 没有的标 "⚠️未加载"
    registry_names = set(registry.get_all_tool_names(include_retired=True))
    for tool in inv.get("tools", []):
        full_name = f"app_{tool.get('name', '')}"
        tool["loaded"] = full_name in registry_names
        if not tool["loaded"]:
            tool["note"] = "⚠️未加载(重启后丢失?) — 重新注册即可恢复"
    return _j(inv)


registry.register(
    name="sense_my_tools",
    toolset="senses",
    schema={
        "name": "sense_my_tools",
        "description": "查看你自己和所在项目注册过的全部工具和技能（scope/name/description），确认能力现状。",
        "parameters": {"type": "object", "properties": {}},
    },
    handler=_handle_sense_my_tools,
    check_fn=lambda: True,
    emoji="📦",
)


__all__ = []
