"""Digital Life toolset definitions.

注: 2026-07-17 工具治理后以下工具已退役(handler 保留, schema 不再注入):
  sense_work / sense_goals / sense_plans / sense_daily
  manage_work / manage_goals / manage_plan / manage_daily / remember_him
迁移目标见 commit 注释。这些列表不应再列已退役工具。
"""

from __future__ import annotations


CORE_TOOLS = [
    "sense_wake_reason",
    "sense_vitals",
    "sense_time",
    "sense_event_queue",
    "sense_event_detail",
    "sense_self",
    "sense_memory",
    "sense_nurture_log",
    "sense_scratchpad",
    "sense_todos",
    "skills_list",
    "skill_view",
    "express_to_human",
    "write_diary",
    "record_thought",
    "update_scratchpad",
    "todo",
    "todo_note",
    "todo_plan",
    "todo_trigger",
    "rest",
]


TOOLSETS = {
    "senses": {
        "description": "Digital Life sense tools for time, state, events, and memory",
        "tools": [
            "sense_wake_reason",
            "sense_vitals",
            "sense_time",
            "sense_event_queue",
            "sense_event_detail",
            "sense_self",
            "sense_memory",
            "sense_nurture_log",
            "sense_scratchpad",
            "sense_todos",
            "sense_contacts",
            "skills_list",
            "skill_view",
            "recall_tool_result",
        ],
        "includes": [],
    },
    "actions": {
        "description": "Digital Life action tools for expression, journaling, thoughts, and rest",
        "tools": [
            "express_to_human",
            "write_diary",
            "record_thought",
            "update_scratchpad",
            "rest",
            "terminal",
            "execute_code",
            "process",
        ],
        "includes": [],
    },
    "tasks": {
        "description": "Digital Life todo execution tools for assigned work",
        "tools": [
            "sense_todos",
            "todo",
            "todo_note",
            "todo_plan",
            "todo_trigger",
        ],
        "includes": [],
    },
}


def extend(*, toolsets: dict, core_tools: list[str]) -> None:
    for tool_name in CORE_TOOLS:
        if tool_name not in core_tools:
            core_tools.append(tool_name)
    toolsets.update(TOOLSETS)
