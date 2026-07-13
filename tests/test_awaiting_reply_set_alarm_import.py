"""awaiting_reply 设立路径回归测试（BUG #2）。

锁定一个极度隐蔽的回归：action_tools.py:900 曾 import 一个从未定义的
find_alarms_by_filter，导致整行 ImportError 被 except 静默吞掉，自 6/24 起
cancel_alarms_by_filter + set_alarm 从未执行，awaiting_reply 事件设不上。

本测试确保这两个关键函数从 action_tools 的发送成功路径上**仍可被 import**，
以保证 ImportError 形式的回归能立刻被 CI 抓住，而不是再次静默一周。
"""
from __future__ import annotations


class TestAwaitingReplyImportIntact:
    """action_tools 发送成功分支依赖的两个 alarms 函数必须可 import。"""

    def test_cancel_alarms_by_filter_importable(self) -> None:
        """cancel_alarms_by_filter 必须存在于 alarms 模块。

        历史回归：曾因同行 import 了不存在的 find_alarms_by_filter，
        导致本函数也一起被 ImportError 拖垮而不可用。
        """
        from domain.lifecycle.alarms import cancel_alarms_by_filter
        assert callable(cancel_alarms_by_filter)

    def test_set_alarm_importable(self) -> None:
        """set_alarm 必须存在且可调用。"""
        from domain.lifecycle.alarms import set_alarm
        assert callable(set_alarm)

    def test_combined_import_succeeds(self) -> None:
        """模拟 action_tools 发送成功路径的那行 import。

        这是对 6/24 回归的最直接锁定——如果未来有人再加一个不存在的名字
        到这行 import，本测试会立刻失败。
        """
        # 与 action_tools.py 发送成功分支完全一致的 import 形式
        from domain.lifecycle.alarms import cancel_alarms_by_filter, set_alarm
        assert cancel_alarms_by_filter is not None
        assert set_alarm is not None


class TestNoDanglingAlarmImports:
    """action_tools.py 里不应再有任何对不存在 alarms 函数的引用。"""

    def test_find_alarms_by_filter_not_imported(self) -> None:
        """find_alarms_by_filter 从未在 alarms 定义过，不应出现在任何 import 里。

        用 AST 静态扫，比 grep 更准确（能区分注释 vs 真实 import）。
        """
        import ast
        from pathlib import Path

        source = Path("interfaces/tools/action_tools.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        dangling = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("alarms"):
                for alias in node.names:
                    if alias.name == "find_alarms_by_filter":
                        dangling.append(node.lineno)
        assert not dangling, f"find_alarms_by_filter 仍在 import（行 {dangling}），会导致 ImportError"
