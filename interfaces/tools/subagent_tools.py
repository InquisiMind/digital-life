"""后台子代理工具（基座）—— spawn / 追问 / 汇报 三个动词。

设计（docs/design/background-subagent.md，2026-10-30 zhp 定稿）：
  - spawn_subagent：立项即返回联系人 ID，执行异步（ServiceWorkerLoop）。
  - message_subagent：委托人追问/补充/取消（归档自动复活）。
  - report_to_delegator：子代理汇报，done=True 汇报后归档封存。
三个都是 domain/service/delegate.py 的薄壳；发送关卡照 collab_tools 惯例
装在工具层（数据源按当前身份路由，故障 fail-open）。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

from interfaces.tools import registry

logger = logging.getLogger(__name__)


def _current_runtime_id() -> str:
    from infrastructure.config import get_app_instance_id

    return (get_app_instance_id() or "").strip()


def _before_send_gate(text: str, *, target_chat_id: str) -> str | None:
    """发送关卡（与 express_to_human / send_chat_message 同一道闸）。"""
    try:
        from domain.lifecycle.communication import check_before_send

        block = check_before_send(text, target_chat_id=target_chat_id)
        if block:
            return json.dumps(block, ensure_ascii=False)
    except Exception:
        pass
    return None


def _handle_spawn_subagent(args: Dict[str, Any], **_) -> str:
    from domain.service import delegate

    sid = _current_runtime_id()
    # 防套娃：通用子代理不能再开子代理（subagent-def 的工具面排除不可行——
    # 服务默认面是全量减旗标，守卫放 handler 层）。
    try:
        from infrastructure.persistence import services_registry

        if sid and services_registry.resolve_service_def(sid) == delegate.SUBAGENT_DEF_ID:
            return registry.tool_error("子代理不能再委派子代理")
    except Exception:
        pass

    result = delegate.spawn_subagent(
        str(args.get("task") or ""),
        name=str(args.get("name") or ""),
        ttl_hours=args.get("ttl_hours") or delegate.DEFAULT_TTL_HOURS,
    )
    if not result.get("ok"):
        return registry.tool_error(result.get("error") or "创建失败")
    return registry.tool_result(
        {
            "ok": True,
            "contact_id": result["contact_id"],
            "display_name": result.get("display_name"),
            "expires_at": result.get("expires_at"),
            "note": (
                f"子代理 {result['contact_id']} 已在后台启动（异步执行）。"
                "完成后它会主动向你汇报；期间可用 message_subagent 追问/补充。"
            ),
        }
    )


def _handle_message_subagent(args: Dict[str, Any], **_) -> str:
    from domain.service import delegate

    contact_id = str(args.get("contact_id") or "").strip()
    text = str(args.get("text") or "").strip()
    if not contact_id or not text:
        return registry.tool_error("必须传 contact_id 和 text")

    # 发送前关卡：目标窗口（subagent:{sid}）本 session 没看过 → 附流水重写。
    block = _before_send_gate(text, target_chat_id=f"subagent:{contact_id}")
    if block:
        return block

    result = delegate.message_subagent(contact_id, text)
    if not result.get("ok"):
        return registry.tool_error(result.get("error") or "发送失败")
    return registry.tool_result(result)


def _handle_report_to_delegator(args: Dict[str, Any], **_) -> str:
    from domain.service import delegate

    text = str(args.get("text") or "").strip()
    if not text:
        return registry.tool_error("必须传 text")
    done = args.get("done")
    done = True if done is None else bool(done)

    # 发送前关卡：委托人窗口本 session 没看过 → 附流水重写；委托人中途的
    # 追问属于未读，必须先消化再汇报（防止拿旧上下文交差）。
    owner_core = (_delegate_service_row().get("owner_ref") or "").split(":", 1)[-1]
    if owner_core:
        block = _before_send_gate(text, target_chat_id=f"peer:{owner_core}")
        if block:
            return block

    result = delegate.report_to_delegator(text, done=done)
    if not result.get("ok"):
        return registry.tool_error(result.get("error") or "汇报失败")
    note = (
        "已归档：任务闭环，等待委托人下一步。"
        if result.get("archived")
        else "已通知委托人，可继续手头任务。"
    )
    return registry.tool_result({**result, "note": note})


def _delegate_service_row() -> dict:
    from infrastructure.persistence import services_registry

    return services_registry.lookup_service(_current_runtime_id()) or {}


# ── 注册 ──────────────────────────────────────────────────────────────

registry.register(
    name="spawn_subagent",
    toolset="subagent",
    emoji="🧩",
    schema={
        "description": (
            "把一项任务委派给后台子代理（异步执行，立即返回联系人 ID）。"
            "任务描述要自包含：目标、背景、期望产出、验收标准。"
            "子代理完成后会主动向你汇报；期间可随时 message_subagent 追问或补充。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "任务描述（自包含，子代理看不到你们的对话上下文）",
                },
                "name": {"type": "string", "description": "可选，子代理显示名（缺省取任务摘要）"},
                "ttl_hours": {
                    "type": "number",
                    "description": "可选，硬截止小时数（缺省 24；到期强制收尾汇报）",
                },
            },
            "required": ["task"],
        },
    },
    handler=_handle_spawn_subagent,
)

registry.register(
    name="message_subagent",
    toolset="subagent",
    emoji="📨",
    schema={
        "description": (
            "给你自己的后台子代理发消息：追问进度、补充信息、或要求取消任务。"
            "已完成的子代理会自动被唤醒继续。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "contact_id": {"type": "string", "description": "spawn_subagent 返回的联系人 ID"},
                "text": {"type": "string", "description": "消息内容"},
            },
            "required": ["contact_id", "text"],
        },
    },
    handler=_handle_message_subagent,
)

registry.register(
    name="report_to_delegator",
    toolset="subagent",
    emoji="📤",
    schema={
        "description": (
            "子代理向委托人汇报：任务完成、无法完成、或阶段性进展。"
            "done=true（缺省）表示本次汇报后任务闭环归档；done=false 为进度通报，继续干活。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "汇报内容（结果、结论、或卡点）"},
                "done": {
                    "type": "boolean",
                    "description": "缺省 true：汇报后归档结束；false：仅通报进度",
                },
            },
            "required": ["text"],
        },
    },
    handler=_handle_report_to_delegator,
)
