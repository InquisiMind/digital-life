"""interfaces.tools.perception_tools 观察工具测试（spec FR-014/FR-015）。

验证：
  - 两个工具注册成功、schema 可见（进 get_definitions）
  - toolset = actions
  - handler 在缺少采集依赖时返回友好错误（不抛异常）
  - sense_media 对不存在的文件返回错误

（sense_screen 已废弃删除——2026-09-29，随工具退役其测试。）
"""
from __future__ import annotations

import pytest

from interfaces.tools import registry


# 确保工具模块已导入注册
def _ensure_loaded():
    import interfaces.tools.perception_tools  # noqa: F401


_ensure_loaded()


def test_tools_registered():
    """两个感知工具注册成功。"""
    for name in ("sense_audio", "sense_media"):
        entry = registry._tools.get(name)
        assert entry is not None, f"{name} 未注册"
        assert entry.toolset == "actions"
        assert entry.schema_visible is True


def test_tools_in_definitions():
    """工具出现在 get_definitions（进 system prompt）。"""
    defs = registry.get_definitions({"sense_audio", "sense_media"})
    names = {d["function"]["name"] for d in defs}
    assert {"sense_audio", "sense_media"} <= names


def test_sense_media_missing_path_returns_error():
    """sense_media 缺 media_path → 工具错误。"""
    _ensure_loaded()
    result = registry.dispatch("sense_media", {"media_path": ""})
    assert "error" in result or "必须传" in result or "error" in str(result)


def test_sense_media_nonexistent_file_returns_error():
    """sense_media 文件不存在 → 友好错误，不抛异常。"""
    _ensure_loaded()
    result = registry.dispatch("sense_media", {"media_path": "/nonexistent/xxx.png"})
    assert "不存在" in result or "error" in result


def test_sense_audio_missing_deps_returns_error(monkeypatch):
    """sense_audio 在无实例上下文时返回友好错误。"""
    _ensure_loaded()
    from infrastructure.config import _instance_id_var

    token = _instance_id_var.set("")
    try:
        result = registry.dispatch("sense_audio", {"seconds": 3})
        assert "实例" in result or "error" in result or "ContextVar" in result
    finally:
        _instance_id_var.reset(token)


def test_perception_tools_in_agent_load_list():
    """_ensure_tools_loaded 应包含 perception_tools（spec FR-014 工具可见性）。"""
    import inspect

    from infrastructure.ai import agent

    src = inspect.getsource(agent.AIAgent._ensure_tools_loaded)
    assert "interfaces.tools.perception_tools" in src
