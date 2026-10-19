"""Adapter-neutral wake session planning helpers."""

from __future__ import annotations

import uuid
from datetime import datetime


# 场景工具集（按 wake reason 选组）。新注册的 toolset 必须接进这里，否则在
# 任何唤醒路径都不可见——2026-10-19 生产复盘：web_search(bocha) 注册在
# "research" 却未接线，三天 0 调用，agent 全靠自注册的 Bing 爬虫 app_web_search
# 搜索。tests/test_tool_face_consistency.py 有守卫，漏接线 CI 变红。
L4_TOOLSETS = ["actions", "senses", "research", "mcp", "trading"]
L4_TASK_TOOLSETS = ["actions", "senses", "tasks", "web", "terminal", "research", "mcp", "trading"]


def enabled_toolsets_for_reason(reason: str) -> list[str]:
    if reason == "task_reminder":
        return L4_TASK_TOOLSETS
    return L4_TOOLSETS


def make_wake_session_id(reason: str, now: datetime | None = None) -> str:
    from domain.lifecycle import clock as _clock
    timestamp = (now or _clock.now_dt()).strftime("%m%d_%H%M")
    reason_slug = "".join(ch if ch.isalnum() else "_" for ch in reason.lower()).strip("_")
    reason_slug = reason_slug or "wake"
    return f"tx_{reason_slug}_{timestamp}_{uuid.uuid4().hex[:6]}"


def make_wake_session_log_filename(session_id: str) -> str:
    return f"{session_id}.json"


__all__ = [
    "L4_TASK_TOOLSETS",
    "L4_TOOLSETS",
    "enabled_toolsets_for_reason",
    "make_wake_session_id",
    "make_wake_session_log_filename",
]
