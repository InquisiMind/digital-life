"""Digital Life toolset definitions.

注: 2026-07-17 工具治理后以下工具已退役(handler 保留, schema 不再注入):
  sense_work / sense_goals / sense_plans / sense_daily
  manage_work / manage_goals / manage_plan / manage_daily / remember_him
迁移目标见 commit 注释。这些列表不应再列已退役工具。
"""

from __future__ import annotations


CORE_TOOLS = [
    # 写入
    "add_cognition", "record_thought",
    # 覆盖/删除
    "supersede_memory", "mark_obsolete", "delete_cognition",
    # 召回
    "recall_memory", "recall_cognition_by_key", "find_conflict_buckets", "search_history",
    # 感知
    "sense_file", "sense_status", "sense_conversation", "sense_entity",
    "sense_social_feed", "sense_schedule", "sense_my_projects", "sense_my_tools", "sense_image",
    # 文件
    "write_file",
    # 飞书 API 代理 (读写统管, token 隐藏, 写操作两步确认)
    "feishu_call", "feishu_download",
    # 行动
    "express_to_human", "rest", "terminal", "execute_code", "process",
    "register_attachment", "register_tool",
    # 技能
    "skill_view", "skills_list", "register_skill",
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
