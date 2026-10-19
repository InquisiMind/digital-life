"""工具面一致性守卫：所有 def 声明的工具必须能在注册表解析为定义。

背景（2026-10-16 事故）：web_search 工具加入了 def 的 app.yaml tools 列表，
但工具模块未进 agent worker 的装载清单 → agent 实际看不到该工具，
配置与实际漂移。本测试把"yaml 声明 ⊆ 注册表"固化为结构约束——
今后新增工具模块漏加装载清单时，这里会红。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from infrastructure.config import get_project_root


def _iter_def_tool_faces():
    root = get_project_root()
    for cfg in sorted((root / "apps").glob("*/config/app.yaml")):
        try:
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            continue
        tools = data.get("tools")
        if isinstance(tools, list) and tools:
            yield cfg.parent.parent.name, tools


def test_def_tool_declarations_resolve_in_registry():
    from interfaces.tools import ensure_loaded

    ensure_loaded()
    from interfaces.tools.registry import registry

    unresolved = []
    for def_id, tools in _iter_def_tool_faces():
        for name in tools:
            if name.startswith("mcp_") or name == "web_search":
                continue  # 外部配置门控类：未配置时按设计不注册
            if registry.get_schema(name) is None:
                unresolved.append(f"{def_id}: {name}")
    assert not unresolved, (
        "以下工具在 def 工具面中声明，但注册表中不存在"
        "（新增工具模块时漏了装载清单？清单唯一来源："
        "interfaces/tools/__init__.py 的 TOOL_MODULES）:\n  " + "\n  ".join(unresolved))


def test_web_search_gated_by_key():
    """web_search 是 key 门控工具：未配 key 时不得注册（对模型隐形）。"""
    from interfaces.tools import ensure_loaded

    ensure_loaded()
    from interfaces.tools.registry import registry
    from interfaces.tools.web_search_tool import _configured

    if _configured():
        assert registry.get_schema("web_search") is not None
    else:
        assert registry.get_schema("web_search") is None


def test_core_manifest_modules_register_tools():
    """TOOL_MODULES 清单的核心模块装载后都应贡献了工具（防清单项/模块名漂移）。"""
    from interfaces.tools import ensure_loaded

    ensure_loaded()
    from interfaces.tools.registry import registry

    names = registry.get_all_tool_names()
    for marker, sample in (
        ("domain.todos.tools", "todo_"),
        ("domain.project.tools", "project_todo_"),
        ("interfaces.tools.terminal_tool", "terminal"),
        ("interfaces.tools.skills_tool", "skill_"),
    ):
        assert any(n.startswith(sample) for n in names), f"{marker} 未注册任何工具"


# 已知刻意不接进任何场景集的 toolset（在所有唤醒路径都不可见）。
# "memory"：002 统一记忆的 2 个内部工具（find_conflict_buckets /
# recall_cognition_by_key），是否对模型开放待该特性拍板，先维持现状。
_KNOWN_HIDDEN_TOOLSETS = {"memory"}


def test_every_registered_toolset_is_wired_into_a_scene_set():
    """注册表里出现的 toolset 必须被某个 L4 场景集覆盖，否则工具在任何唤醒路径
    都不可见（2026-10-19 生产复盘：web_search 注册在 research 未接线，三天 0 调用）。
    """
    from interfaces.tools import ensure_loaded

    ensure_loaded()
    from domain.lifecycle.wake import L4_TASK_TOOLSETS, L4_TOOLSETS
    from interfaces.tools.registry import registry

    covered = set(L4_TOOLSETS) | set(L4_TASK_TOOLSETS)
    registered = set(registry.get_tool_to_toolset_map().values())
    orphans = registered - covered - _KNOWN_HIDDEN_TOOLSETS
    assert not orphans, (
        f"toolset {sorted(orphans)} 未接进任何场景集（domain/lifecycle/wake.py），"
        f"其工具在所有唤醒路径都不可见"
    )
    # research（web_search）必须在消息类唤醒也可见——客户提问即触发检索
    assert "research" in L4_TOOLSETS
