"""Registration lifecycle — the domain core.

Full flow for registering a tool or skill:
  1. Safety checks (reserved name, valid chars)
  2. Resolve target path based on scope
  3. Write the code/content file
  4. Update manifest.json at the scope level
  5. Hot-load: import module → register into ToolRegistry (tools only)

This module owns the business rules; callers (interfaces/tools/) only
pass arguments and format the JSON result.
"""

from __future__ import annotations

import importlib
import logging
import sys
import textwrap
import traceback
from pathlib import Path
from typing import Any

from domain.capability import store
from domain.capability.paths import project_root, resolve_skill_path, resolve_tool_path

logger = logging.getLogger(__name__)


# ── Reserved names ───────────────────────────────────────────────────────────

RESERVED_TOOL_PREFIXES: tuple[str, ...] = (
    "express_to_human", "rest", "sense_", "task_", "todo_",
    "recall_", "skill_", "merge_", "manage_", "update_",
    "add_lesson", "update_rules", "update_self_knowledge",
    "write_diary", "record_thought", "remember_him",
    "process", "execute_code", "terminal", "register_",
)


def _is_reserved(name: str) -> bool:
    for p in RESERVED_TOOL_PREFIXES:
        if name == p or name.startswith(p):
            return True
    return False


def _validate_name(name: str) -> str | None:
    if not name:
        return "name is required"
    if _is_reserved(name):
        return f"'{name}' conflicts with a reserved system prefix"
    if not name.replace("_", "").isalnum():
        return "name must be alphanumeric + underscores only"
    return None


# ── Tool registration ────────────────────────────────────────────────────────


def register_tool(
    *,
    name: str,
    description: str,
    parameters: dict[str, Any],
    code: str,
    scope: str = "personal",
    project_id: str = "",
    emoji: str = "🔧",
    instance_id: str = "",
) -> dict[str, Any]:
    """Register a new tool or replace an existing one.

    Returns ``{"ok": True, ...}`` on success, ``{"ok": False, "reason": ...}`` on failure.
    """
    err = _validate_name(name)
    if err:
        return {"ok": False, "reason": err}
    if not description or not code:
        return {"ok": False, "reason": "description and code are required"}
    if not instance_id and scope == "personal":
        return {"ok": False, "reason": "personal scope requires instance_id in ContextVar"}

    root = project_root()
    try:
        rel_path = resolve_tool_path(scope, name, instance_id=instance_id, project_id=project_id)
    except Exception as exc:
        return {"ok": False, "reason": f"path resolution failed: {exc}"}

    full_path = root / rel_path

    # Write the .py source
    code_body = textwrap.indent(code.strip(), "    ")
    source = f'''"""Auto-registered tool: {name} (scope={scope})."""

from typing import Any, Dict


def handler(args: Dict[str, Any], **kwargs: Any) -> str:
{code_body}
'''
    try:
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(source, encoding="utf-8")
    except Exception as exc:
        return {"ok": False, "reason": f"write failed {full_path}: {exc}"}

    # Update manifest (含 parameters, 启动时重建 schema 需要)
    store.upsert_manifest_entry(
        scope, "tools",
        name=name, description=description[:200],
        instance_id=instance_id, project_id=project_id,
        extra={"emoji": emoji, "file": str(rel_path), "parameters": parameters},
    )

    # Hot-load into runtime ToolRegistry
    tool_full_name = f"app_{name}"
    hot_load_result = _hot_load_tool(full_path, tool_full_name, description, parameters, emoji)
    if not hot_load_result["ok"]:
        return hot_load_result

    return {
        "ok": True,
        "tool_name": tool_full_name,
        "file_path": str(rel_path),
        "scope": scope,
        "note": (
            f"已注册并热加载。下次 LLM call 就能调用 '{tool_full_name}'。"
            f"名字加了 app_ 前缀以防和系统工具冲突。"
        ),
    }


def _hot_load_tool(
    file_path: Path,
    tool_full_name: str,
    description: str,
    parameters: dict[str, Any],
    emoji: str,
) -> dict[str, Any]:
    """Import the module, extract handler(), register into ToolRegistry."""
    from interfaces.tools.registry import registry

    try:
        dir_to_add = str(file_path.parent.resolve())
        if dir_to_add not in sys.path:
            sys.path.insert(0, dir_to_add)
        module = importlib.import_module(file_path.stem)
        importlib.reload(module)
        handler_fn = getattr(module, "handler", None)
        if not handler_fn or not callable(handler_fn):
            return {"ok": False, "reason": "handler(args, **kwargs) function not found in code"}

        schema = {
            "name": tool_full_name,
            "description": description,
            "parameters": parameters if isinstance(parameters, dict) else {"type": "object", "properties": {}},
        }
        # Remove old entry if exists (update case)
        try:
            if tool_full_name in registry._registry:
                registry._registry.pop(tool_full_name, None)
                registry._handlers.pop(tool_full_name, None)
        except Exception:
            pass

        registry.register(
            name=tool_full_name,
            toolset="actions",
            schema=schema,
            handler=handler_fn,
            check_fn=lambda: True,
            emoji=emoji,
        )
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "reason": f"hot-load failed: {exc}", "traceback": traceback.format_exc()}


def load_registered_tools(instance_id: str = "") -> dict[str, int]:
    """启动时从 manifest 重建所有已注册的工具到 registry。

    扫 personal + shared + project 的 tools manifest, 对每个条目调 _hot_load_tool。
    在 _ensure_tools_loaded 末尾调用, 确保重启后工具不丢失。

    返回 {"loaded": N, "failed": M}。
    """
    from domain.capability.store import _read_all_manifests_for_instance
    root = project_root()

    items = _read_all_manifests_for_instance("tools", instance_id)
    loaded = 0
    failed = 0
    for item in items:
        name = item.get("name", "")
        rel_file = item.get("file", "")
        if not name or not rel_file:
            continue
        full_path = root / rel_file
        if not full_path.exists():
            logger.debug("load_registered_tools: %s file missing (%s), skip", name, full_path)
            failed += 1
            continue
        tool_full_name = f"app_{name}"
        description = item.get("description", name)
        parameters = item.get("parameters") or {"type": "object", "properties": {}}
        emoji = item.get("emoji", "🔧")
        result = _hot_load_tool(full_path, tool_full_name, description, parameters, emoji)
        if result.get("ok"):
            loaded += 1
        else:
            logger.warning("load_registered_tools: %s failed: %s", name, result.get("reason", ""))
            failed += 1
    if loaded:
        logger.info("load_registered_tools: %d loaded, %d failed", loaded, failed)
    return {"loaded": loaded, "failed": failed}


# ── Skill registration ───────────────────────────────────────────────────────


def register_skill(
    *,
    name: str,
    content: str,
    scope: str = "personal",
    project_id: str = "",
    instance_id: str = "",
) -> dict[str, Any]:
    """Register a new skill (markdown methodology) or replace an existing one."""
    err = _validate_name(name)
    if err:
        return {"ok": False, "reason": err}
    if not content:
        return {"ok": False, "reason": "content is required"}
    if not instance_id and scope == "personal":
        return {"ok": False, "reason": "personal scope requires instance_id in ContextVar"}

    root = project_root()
    try:
        rel_path = resolve_skill_path(scope, name, instance_id=instance_id, project_id=project_id)
    except Exception as exc:
        return {"ok": False, "reason": f"path resolution failed: {exc}"}

    full_path = root / rel_path

    # Wrap with frontmatter if not already present
    final_content = content
    if not final_content.lstrip().startswith("---"):
        final_content = (
            "---\n"
            f"name: {name}\n"
            f"description: auto-registered skill\n"
            "version: 1.0.0\n"
            "platforms: []\n"
            "---\n\n"
            + content
        )

    try:
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(final_content, encoding="utf-8")
    except Exception as exc:
        return {"ok": False, "reason": f"write failed {full_path}: {exc}"}

    # Update manifest
    first_line = content.strip().split("\n")[0][:100]
    store.upsert_manifest_entry(
        scope, "skills",
        name=name, description=first_line,
        instance_id=instance_id, project_id=project_id,
        extra={"file": str(rel_path)},
    )

    return {
        "ok": True,
        "skill_name": name,
        "file_path": str(rel_path),
        "scope": scope,
        "note": (
            f"已写入。下次 wake 在 skill_index 就能看到 '{name}'，"
            f"调 skill_view('{name}') 看完整方法论。"
            f"修改时再调 register_skill 传同名即可覆盖。"
        ),
    }
