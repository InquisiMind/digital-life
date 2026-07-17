"""工具注册中心 — OpenAI 兼容的工具注册与调度。

ToolRegistry 提供：
  - register(): 注册工具（name/toolset/schema/handler/check_fn/emoji）
  - dispatch(): 按名称调度工具执行
  - get_definitions(): 获取 OpenAI function-calling 格式的工具定义列表
  - is_toolset_available(): 检查工具集是否可用（通过 check_fn）
  - get_available_toolsets(): 获取所有可用工具集及其工具列表

ToolEntry 数据结构：
  name, toolset, schema（OpenAI function schema）, handler（执行函数）,
  check_fn（可用性检查，如环境变量是否配置）, requires_env, is_async, emoji

每个工具按 toolset 分组（如 actions/senses/tasks/terminal），
toolset 级别的 check_fn 控制整组工具的可用性。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolEntry:
    name: str
    toolset: str
    schema: dict[str, Any]
    handler: Callable[..., Any]
    check_fn: Callable[[], bool] | None = None
    requires_env: tuple[str, ...] = ()
    is_async: bool = False
    description: str = ""
    emoji: str = ""
    max_result_size_chars: int | float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_visible: bool = True
    """schema_visible=False 的工具仍可被 dispatch(历史 tool_call 重放可用),
    但不出现在 get_definitions() 输出里(不进 system prompt 的 tools 数组)。

    用于"退役工具向新家转发"过渡期——模型看不到 schema 自然不会主动调,
    但旧 session 的历史 tool_call 重放 / 模型偶尔幻觉调用时,handler 仍工作。
    """


class ToolRegistry:
    """数字生命工具注册中心——OpenAI 兼容的函数调用格式。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolEntry] = {}
        self._toolset_checks: dict[str, Callable[[], bool]] = {}

    def register(
        self,
        name: str,
        toolset: str,
        schema: dict[str, Any],
        handler: Callable[..., Any],
        check_fn: Callable[[], bool] | None = None,
        requires_env: list[str] | tuple[str, ...] | None = None,
        is_async: bool = False,
        description: str = "",
        emoji: str = "",
        max_result_size_chars: int | float | None = None,
        schema_visible: bool = True,
        **metadata: Any,
    ) -> None:
        self._tools[name] = ToolEntry(
            name=name,
            toolset=toolset,
            schema={**(schema or {}), "name": name},
            handler=handler,
            check_fn=check_fn,
            requires_env=tuple(requires_env or ()),
            is_async=is_async,
            description=description or str((schema or {}).get("description", "")),
            emoji=emoji,
            max_result_size_chars=max_result_size_chars,
            metadata=dict(metadata),
            schema_visible=schema_visible,
        )
        if check_fn and toolset not in self._toolset_checks:
            self._toolset_checks[toolset] = check_fn

    def register_handler_only(
        self,
        name: str,
        toolset: str,
        handler: Callable[..., Any],
        *,
        description: str = "",
        emoji: str = "",
    ) -> None:
        """注册一个"退役中"工具——只挂 handler,不暴露 schema。

        场景:旧 tool_calls 重放 / 模型幻觉调用时仍可 dispatch(返回 _deprecated_hint),
        但 system_prompt 的 tools 数组不再包含它。

        schema 字段给一个 stub(只为 ToolEntry frozen 字段必填),不会被 get_definitions 取用。
        """
        self.register(
            name=name,
            toolset=toolset,
            schema={"name": name, "description": description or "[retired]"},
            handler=handler,
            check_fn=lambda: True,
            description=description,
            emoji=emoji,
            schema_visible=False,
        )

    def deregister(self, name: str) -> None:
        entry = self._tools.pop(name, None)
        if not entry:
            return
        if not any(tool.toolset == entry.toolset for tool in self._tools.values()):
            self._toolset_checks.pop(entry.toolset, None)

    def dispatch(self, name: str, args: dict[str, Any] | None = None, **kwargs: Any) -> str:
        entry = self._tools.get(name)
        if not entry:
            return tool_error(f"Unknown tool: {name}",
                              retired=False,
                              hint="此工具从未注册或被硬删除。请检查 tools 列表。")
        # 退役中工具 (schema_visible=False) 仍可 dispatch,但结果附 "_deprecated" 提示,
        # 让模型在下一轮自然改用新家。
        deprecated_prefix = ""
        if not entry.schema_visible:
            deprecated_prefix = "[retired] "
        try:
            payload = args or {}
            if entry.is_async:
                from interfaces.tools.async_utils import run_async

                result = run_async(entry.handler(payload, **kwargs))
            else:
                result = entry.handler(payload, **kwargs)
            # 统一序列化为字符串后再做尺寸治理。之前 str 早返回、dict 才
            # json.dumps 两条路径分叉，导致 str 返回的 tool 永远逃过 cap。
            if not isinstance(result, str):
                result = json.dumps(result, ensure_ascii=False, default=str)
            limit = entry.max_result_size_chars or DEFAULT_RESULT_SIZE_LIMIT
            return deprecated_prefix + truncate_result(result, limit)
        except Exception as exc:
            logger.exception("Tool %s dispatch error: %s", name, exc)
            return tool_error(f"{deprecated_prefix}Tool execution failed: {type(exc).__name__}: {exc}")

    def get_definitions(self, tool_names: set[str], quiet: bool = False) -> list[dict[str, Any]]:
        definitions: list[dict[str, Any]] = []
        for name in sorted(tool_names):
            entry = self._tools.get(name)
            # schema_visible=False 的工具(router 标 退役中)不进 system prompt
            if not entry or not entry.schema_visible:
                continue
            if not self._entry_available(entry, quiet=quiet):
                continue
            definitions.append({"type": "function", "function": dict(entry.schema)})
        return definitions

    def get_all_tool_names(self, *, include_retired: bool = False) -> list[str]:
        """返回所有工具名。include_retired=True 时也包含 schema_visible=False 的(默认不包含)。"""
        return sorted(
            name for name, e in self._tools.items()
            if include_retired or e.schema_visible
        )

    def get_schema(self, name: str) -> dict[str, Any] | None:
        entry = self._tools.get(name)
        return dict(entry.schema) if entry else None

    def get_toolset_for_tool(self, name: str) -> str | None:
        entry = self._tools.get(name)
        return entry.toolset if entry else None

    def get_emoji(self, name: str, default: str = "⚡") -> str:
        entry = self._tools.get(name)
        return entry.emoji or default if entry else default

    def get_tool_to_toolset_map(self) -> dict[str, str]:
        return {name: entry.toolset for name, entry in self._tools.items()}

    def is_toolset_available(self, toolset: str) -> bool:
        check = self._toolset_checks.get(toolset)
        if not check:
            return True
        try:
            return bool(check())
        except Exception:
            logger.debug("Toolset %s check failed", toolset, exc_info=True)
            return False

    def check_toolset_requirements(self) -> dict[str, bool]:
        return {toolset: self.is_toolset_available(toolset) for toolset in self._all_toolsets()}

    def check_tool_availability(self, quiet: bool = False):
        available: list[str] = []
        unavailable: list[dict[str, Any]] = []
        for toolset in self._all_toolsets():
            if self.is_toolset_available(toolset):
                available.append(toolset)
            else:
                unavailable.append({"name": toolset, "tools": self._tools_for_toolset(toolset)})
        return available, unavailable

    def get_available_toolsets(self) -> dict[str, dict[str, Any]]:
        return {
            toolset: {
                "available": self.is_toolset_available(toolset),
                "tools": self._tools_for_toolset(toolset),
                "requirements": sorted({env for entry in self._tools.values() if entry.toolset == toolset for env in entry.requires_env}),
            }
            for toolset in self._all_toolsets()
        }

    def get_toolset_requirements(self) -> dict[str, dict[str, Any]]:
        return {
            toolset: {
                "name": toolset,
                "env_vars": sorted({env for entry in self._tools.values() if entry.toolset == toolset for env in entry.requires_env}),
                "tools": self._tools_for_toolset(toolset),
                "check_fn": self._toolset_checks.get(toolset),
                "setup_url": None,
            }
            for toolset in self._all_toolsets()
        }

    def get_max_result_size(self, name: str, default: int | float | None = None) -> int | float | None:
        entry = self._tools.get(name)
        if entry and entry.max_result_size_chars is not None:
            return entry.max_result_size_chars
        return default

    def tool_error(self, message: Any, **extra: Any) -> str:
        return tool_error(message, **extra)

    def tool_result(self, data: Any = None, **kwargs: Any) -> str:
        return tool_result(data, **kwargs)

    def _entry_available(self, entry: ToolEntry, *, quiet: bool) -> bool:
        if not entry.check_fn:
            return True
        try:
            return bool(entry.check_fn())
        except Exception:
            if not quiet:
                logger.debug("Tool %s check failed", entry.name, exc_info=True)
            return False

    def _all_toolsets(self) -> list[str]:
        return sorted({entry.toolset for entry in self._tools.values()})

    def _tools_for_toolset(self, toolset: str) -> list[str]:
        return sorted(name for name, entry in self._tools.items() if entry.toolset == toolset)


def tool_error(message: Any, **extra: Any) -> str:
    payload = {"error": str(message)}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False, default=str)


def tool_result(data: Any = None, **kwargs: Any) -> str:
    payload = data if data is not None else kwargs
    return json.dumps(payload, ensure_ascii=False, default=str)


# 工具结果尺寸治理：dispatch 的统一兜底上限。
# 实测 >8KB 的 tool 结果虽只占行数 ~1-2%，却贡献 ~12% 字节（且会随 session
# 回放逐轮累积）。>8KB 才截断，77% 的 <1KB 小结果完全不受影响。
# 单个 tool 可通过 register(max_result_size_chars=N) 覆盖（如 terminal 用 50K）。
DEFAULT_RESULT_SIZE_LIMIT = 8000


def truncate_result(text: str, limit: int | float) -> str:
    """超长 tool 结果的 head/tail 截断（复用 terminal_tool 的成熟范式）。

    head 占 40%、tail 占 60%，中间插入明确的截断标记，告知模型「数据被裁」，
    以便按需再查（如 todo action=get / sense 详细参数）。≤limit 原样返回。
    """
    if not text:
        return text
    limit = int(limit) if isinstance(limit, (int, float)) else DEFAULT_RESULT_SIZE_LIMIT
    if len(text) <= limit:
        return text
    head_n = int(limit * 0.4)
    tail_n = limit - head_n
    omitted = len(text) - head_n - tail_n
    marker = (
        f"\n... [RESULT TRUNCATED — {omitted} chars omitted "
        f"out of {len(text)} total, tool-result capped at {limit}] ...\n"
    )
    return text[:head_n] + marker + text[-tail_n:]


registry = ToolRegistry()

