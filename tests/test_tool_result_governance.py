"""工具结果尺寸治理测试（dispatch 截断 + per-tool 语义级限条）。

覆盖：
  1. truncate_result：head/tail 截断范式与小结果原样返回
  2. registry.dispatch：默认 8KB 兜底截断（str 与 dict 两条路径）
  3. per-tool 自定义上限（max_result_size_chars 覆盖默认值）
  4. sense_nurture_log：默认限 20 条 + count/returned/note 语义完整
  5. todo list/search：默认限条 + count 保留

设计参见 docs：tool 结果是上下文最大膨胀源（占 ~70%），实测 >8KB 的少数
行贡献 ~12% 字节；本测试守护「源头截断」不被回归。
"""

from __future__ import annotations

import json

from interfaces.tools.registry import (
    DEFAULT_RESULT_SIZE_LIMIT,
    ToolRegistry,
    truncate_result,
)


# ── 1. truncate_result 纯函数 ──────────────────────────────────────────────────


def test_truncate_small_result_untouched():
    """≤limit 原样返回，不加任何标记。"""
    text = "x" * 1000
    assert truncate_result(text, 8000) == text


def test_truncate_large_result_has_marker_and_len_bounded():
    """>limit 触发 head+tail+marker，总长 ≈ limit。"""
    text = "a" * 15000
    out = truncate_result(text, 8000)
    assert "RESULT TRUNCATED" in out
    assert f"{15000} total" in out
    # head 40%+marker+tail 60%，略超 limit（marker 约 90 字符）
    assert len(out) < 9000
    # 保留 head 与 tail 内容
    assert out.startswith("a" * 10)
    assert out.endswith("a" * 10)


def test_truncate_custom_limit_respected():
    """limit=2000 时按 2000 截断，不是默认 8000。"""
    out = truncate_result("b" * 5000, 2000)
    assert len(out) < 2500
    assert "RESULT TRUNCATED" in out


def test_truncate_empty_string_safe():
    assert truncate_result("", 8000) == ""


# ── 2. registry.dispatch 默认兜底截断 ──────────────────────────────────────────


def _make_registry() -> ToolRegistry:
    """独立 ToolRegistry，避免污染 interfaces.tools.registry 全局单例。"""
    return ToolRegistry()


def test_dispatch_truncates_str_result_over_limit():
    reg = _make_registry()
    reg.register(
        name="big_str_tool",
        toolset="test",
        schema={
            "name": "big_str_tool",
            "description": "",
            "parameters": {"type": "object"},
        },
        handler=lambda args, **_: "y" * 15000,
        check_fn=lambda: True,
    )
    out = reg.dispatch("big_str_tool", {})
    assert len(out) < 9000
    assert "RESULT TRUNCATED" in out


def test_dispatch_truncates_dict_result_over_limit():
    """dict 结果先 json.dumps 再截断（之前 dict 路径未截断）。

    关键：截断从 JSON 中间切断，导致尾部可能不完整。本测试只断言「头部
    仍含完整 JSON 起始 + 截断标记」（LLM 至少能识别结构并知道被裁）。
    """
    reg = _make_registry()
    big_list = [{"i": i, "data": "x" * 50} for i in range(500)]  # ~30KB
    reg.register(
        name="big_list_tool",
        toolset="test",
        schema={
            "name": "big_list_tool",
            "description": "",
            "parameters": {"type": "object"},
        },
        handler=lambda args, **_: {"log": big_list},
        check_fn=lambda: True,
    )
    out = reg.dispatch("big_list_tool", {})
    assert len(out) < 9000
    assert "RESULT TRUNCATED" in out
    # 头部应仍是合法 JSON 起始（log 数组的开头）
    assert out.startswith('{"log":')


def test_dispatch_small_result_not_truncated():
    reg = _make_registry()
    reg.register(
        name="small_tool",
        toolset="test",
        schema={
            "name": "small_tool",
            "description": "",
            "parameters": {"type": "object"},
        },
        handler=lambda args, **_: {"ok": True, "msg": "short"},
        check_fn=lambda: True,
    )
    out = reg.dispatch("small_tool", {})
    assert "RESULT TRUNCATED" not in out
    assert json.loads(out)["ok"] is True


# ── 3. per-tool 自定义上限 ─────────────────────────────────────────────────────


def test_dispatch_per_tool_custom_limit_overrides_default():
    """register(max_result_size_chars=N) 让该 tool 按 N 而非 8000 截断。"""
    reg = _make_registry()
    reg.register(
        name="capped_tool",
        toolset="test",
        schema={
            "name": "capped_tool",
            "description": "",
            "parameters": {"type": "object"},
        },
        handler=lambda args, **_: "z" * 5000,
        check_fn=lambda: True,
        max_result_size_chars=2000,
    )
    out = reg.dispatch("capped_tool", {})
    # 5000 > 2000 → 截断；按 2000 而非默认 8000
    assert len(out) < 2500
    assert "RESULT TRUNCATED" in out


def test_dispatch_per_tool_large_limit_allows_big_result():
    """terminal/code 注册 100_000，5KB 结果不应被裁。"""
    reg = _make_registry()
    reg.register(
        name="terminal_like",
        toolset="test",
        schema={
            "name": "terminal_like",
            "description": "",
            "parameters": {"type": "object"},
        },
        handler=lambda args, **_: "w" * 5000,
        check_fn=lambda: True,
        max_result_size_chars=100_000,
    )
    out = reg.dispatch("terminal_like", {})
    assert "RESULT TRUNCATED" not in out
    assert len(out) == 5000


def test_default_limit_constant():
    assert DEFAULT_RESULT_SIZE_LIMIT == 8000


# ── 4. sense_nurture_log 限条（测真实 handler，mock get_nurture_log）───────────


def test_sense_nurture_log_limits_to_default_20():
    """真实 _handle_sense_nurture_log：50 条输入 → 默认只回 20 条，count=50。"""
    from unittest.mock import patch

    with patch("interfaces.tools.sense_tools.get_nurture_log") as m:
        m.return_value = [
            {"at": i, "kind": "feed", "delta": {"energy": 0.1}} for i in range(50)
        ]
        from interfaces.tools.sense_tools import _handle_sense_nurture_log

        data = json.loads(_handle_sense_nurture_log({}))
    assert data["count"] == 50
    assert data["returned"] == 20
    assert len(data["log"]) == 20
    assert "note" in data and "50" in data["note"]


def test_sense_nurture_log_respects_custom_limit():
    from unittest.mock import patch

    with patch("interfaces.tools.sense_tools.get_nurture_log") as m:
        m.return_value = [{"at": i} for i in range(50)]
        from interfaces.tools.sense_tools import _handle_sense_nurture_log

        data = json.loads(_handle_sense_nurture_log({"limit": 5}))
    assert data["count"] == 50
    assert data["returned"] == 5
    assert len(data["log"]) == 5


def test_sense_nurture_log_all_fits_no_note():
    """数据量 ≤ limit 时不应有 note（避免误导模型）。"""
    from unittest.mock import patch

    with patch("interfaces.tools.sense_tools.get_nurture_log") as m:
        m.return_value = [{"at": i} for i in range(10)]
        from interfaces.tools.sense_tools import _handle_sense_nurture_log

        data = json.loads(_handle_sense_nurture_log({}))
    assert data["count"] == 10
    assert data["returned"] == 10
    assert "note" not in data


# ── 5. todo list/search 限条（独立 ToolRegistry + 真实 register_task_tools）────


def _todo_registry() -> ToolRegistry:
    """独立 registry + 真实 todo tool 注册（handler 闭包绑定 module 级 list_tasks）。

    限条逻辑在 handler 内执行时取 args.limit 切片，不依赖 list_tasks 的返回量，
    所以测试时在 dispatch 调用期间 patch list_tasks 即可控制返回条数。
    """
    reg = ToolRegistry()
    from domain.todos.tools import register_task_tools

    register_task_tools(registry=reg)
    return reg


def test_todo_list_defaults_to_30():
    reg = _todo_registry()
    fake = [{"id": str(i), "title": f"task-{i}"} for i in range(100)]
    from unittest.mock import patch

    with patch("domain.todos.tools.list_tasks", return_value=fake):
        out = reg.dispatch("todo", {"action": "list"})
    data = json.loads(out)
    assert data["count"] == 100
    assert data["returned"] == 30
    assert len(data["tasks"]) == 30
    assert "note" in data and "100" in data["note"]


def test_todo_list_custom_limit():
    reg = _todo_registry()
    fake = [{"id": str(i), "title": f"task-{i}"} for i in range(100)]
    from unittest.mock import patch

    with patch("domain.todos.tools.list_tasks", return_value=fake):
        out = reg.dispatch("todo", {"action": "list", "limit": 10})
    data = json.loads(out)
    assert data["returned"] == 10
    assert len(data["tasks"]) == 10


def test_todo_search_defaults_to_20():
    reg = _todo_registry()
    fake = [{"id": str(i), "title": f"hit-{i}"} for i in range(50)]
    from unittest.mock import patch

    with patch("domain.todos.tools.search_tasks", return_value=fake):
        out = reg.dispatch("todo", {"action": "search", "query": "x"})
    data = json.loads(out)
    assert data["count"] == 50
    assert data["returned"] == 20
    assert len(data["tasks"]) == 20


# ── 6. 集成契约：dispatch 对真实 ToolRegistry 的端到端裁剪 ──────────────────────


def test_dispatch_async_path_also_truncated():
    """is_async=True 的 tool 也走截断（dispatch 的 async 分支不能漏）。"""
    reg = _make_registry()

    async def big_async(args, **_):
        return {"data": "q" * 20000}

    reg.register(
        name="async_big",
        toolset="test",
        schema={
            "name": "async_big",
            "description": "",
            "parameters": {"type": "object"},
        },
        handler=big_async,
        check_fn=lambda: True,
        is_async=True,
    )
    out = reg.dispatch("async_big", {})
    assert len(out) < 9000
    assert "RESULT TRUNCATED" in out
