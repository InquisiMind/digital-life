"""P2 — 统一记忆检索面 (single facade) 的入口点。

消费侧入口:
    from domain.memory.memory.recall.unified import unified_recall, render_breadcrumbs

详见 facade.py。
"""

from domain.memory.memory.recall.unified.facade import (
    unified_recall,
    render_breadcrumbs,
)

__all__ = ["unified_recall", "render_breadcrumbs"]
