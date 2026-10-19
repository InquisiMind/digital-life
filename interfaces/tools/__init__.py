"""Project-owned tool runtime package."""

from .registry import ToolRegistry, registry, tool_error, tool_result

__all__ = ["ToolRegistry", "registry", "tool_error", "tool_result", "TOOL_MODULES", "ensure_loaded"]

# 全部工具模块的唯一清单（single source of truth）。
# 新增工具模块只改这里——agent worker 装载与服务台工具目录都调 ensure_loaded()，
# 杜绝"两份清单各自维护、新增工具只在一边出现"的漂移。
# 业务层扩展模块（business/*/activate.py 声明）由 ensure_loaded() 一并装载。
TOOL_MODULES = [
    "interfaces.tools.sense_tools",
    "interfaces.tools.action_tools",
    "interfaces.tools.skills_tool",
    "interfaces.tools.capability_tools",
    "interfaces.tools.vision_tool",
    "interfaces.tools.perception_tools",
    "interfaces.tools.memory_cognition_tools",
    "domain.todos.tools",
    "domain.project.tools",
    "interfaces.tools.terminal_tool",
    "interfaces.tools.code_execution_tool",
    "interfaces.tools.mcp_tools",
    "interfaces.tools.web_search_tool",
]

_loaded = False


def ensure_loaded() -> None:
    """装载全部工具模块（幂等）：核心清单 + 业务扩展模块。

    工具注册是 import 副作用；重复调用为缓存命中，无开销。
    """
    global _loaded
    if _loaded:
        return
    import importlib

    for module_name in TOOL_MODULES:
        try:
            importlib.import_module(module_name)
        except Exception:  # noqa: BLE001 — 单模块装载失败不拦其余
            import logging

            logging.getLogger(__name__).warning(
                "Tool module load failed: %s", module_name, exc_info=True)
    try:
        from infrastructure import extension

        for module_name in extension.tool_modules():
            try:
                importlib.import_module(module_name)
            except Exception:  # noqa: BLE001
                import logging

                logging.getLogger(__name__).warning(
                    "Tool module load failed: %s", module_name, exc_info=True)
    except Exception:  # noqa: BLE001 — 扩展点不可用按核心清单
        pass
    _loaded = True
