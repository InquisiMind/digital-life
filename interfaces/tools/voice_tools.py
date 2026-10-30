"""语音会话专用工具（2026-10-29 zhp 设计，同日改瞬时语义）。

get_latest_input：瞬时拿一眼用户的最新语音信息——有没有新内容、他停下来
多久了、是否还在说。没有任何阻塞等待："等"不存在——模型拿到信息后自行
判断；用户还在说就安静收工（他再说会经事件系统自动把模型叫醒）。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict

from interfaces.tools import registry


def _handle_get_latest_input(args: Dict[str, Any], **kwargs) -> str:
    """瞬时返回：最近语音 utterance（带新旧）、距上次说话秒数、是否正在说。"""
    try:
        from infrastructure.config import get_app_instance_id
        from infrastructure.perception.web_voice import get_web_voice_hub

        iid = get_app_instance_id() or ""
        session = get_web_voice_hub()._session
        if not iid or session is None or session.instance_id != iid:
            return json.dumps(
                {"ok": False, "error": "当前没有活跃的语音会话（用户不在线上）"},
                ensure_ascii=False,
            )
        snapshot = session.latest_input_snapshot()
        since = snapshot["seconds_since_last_speech"]
        if snapshot["speaking_now"]:
            hint = "他正在说话——不要出声，安静休息，他说完的下一段会自动叫醒你。"
        elif since is not None and since >= 3.0:
            hint = "他已经停了一段时间——应该是说完了。判断这些内容是否足够：够就把整段合起来回应（或判断不需要回应就休息）。"
        else:
            hint = "他刚停下来不久——若内容明显没说完，安静休息等下一段（会自动叫醒你）；若已足够就回应。"
        return json.dumps(
            {"ok": True, **snapshot, "hint": hint},
            ensure_ascii=False,
        )
    except Exception as exc:
        return json.dumps({"ok": False, "error": f"get_latest_input failed: {exc}"}, ensure_ascii=False)


registry.register(
    name="get_latest_input",
    toolset="senses",
    schema={
        "name": "get_latest_input",
        "description": (
            "瞬时获取用户的最新语音信息：最近他说的话（最近几段）、距上次说话几秒、"
            "是否正在说。不等待——拿到后自行判断：还在说→安静休息（下一段会自动"
            "叫醒你）；停够了→说完了，判断要不要出声；内容足够→整段合起来回应。"
        ),
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
    handler=_handle_get_latest_input,
    check_fn=lambda: True,
    emoji="📥",
    schema_visible=True,
)
