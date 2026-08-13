"""视觉上下文精简器（spec US3 / FR-007~FR-009）。

视觉模型需要带背景信息理解录屏，但上下文要比主意识精简（它不做 react）。
本模块**只读**从主意识的 audit 表（runtime_log.turn）取最近几轮主对话，
剥成符合 OpenAI/GLM 协议的 messages，让视觉模型能"带着背景看"。

关键事实（spec 调研确认）：
  - 主意识 turn 表的思考列叫 ``reasoning``（DB 列名）；
    GLM 协议入站字段叫 ``reasoning_content``。必须重命名，视觉模型才读得到。
  - turn 行还带 id/timestamp/segment_index/chat_id 等非协议字段，必须剥离，
    否则虽不致 400 但会污染。
  - **绝不**调 ``recall_session(当前 session_id)``——当前 session 摘要只在
    wake 结束后才生成，进行中调用只返回占位串。

本模块是纯读投影：不写库、不调 WakeContext 写入 API、不进 scheduler 注入链路。
范本：infrastructure/ai/assembly.py 的"纯读、DB 唯一事实源、不写库"模式。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# OpenAI/GLM chat 协议允许的 message 字段白名单。
# turn 表里其它字段（id/timestamp/wake_id/...）一律剥离。
_PROTOCOL_KEYS: frozenset[str] = frozenset(
    {"role", "content", "tool_calls", "tool_call_id", "name", "reasoning_content"}
)


def _clean_turn_row(row: dict[str, Any]) -> dict[str, Any]:
    """把 audit turn 行剥成协议 message。

    - 重命名 ``reasoning`` → ``reasoning_content``（字符串内容不变）
    - 剥离 id/timestamp/wake_id 等非协议字段
    - tool_calls 若存在保持（已由 list_turns 反序列化为 list[dict]）
    - content 为 None 时规整为空串（协议要求 str）
    """
    msg: dict[str, Any] = {}
    role = row.get("role") or "assistant"
    msg["role"] = role
    content = row.get("content")
    msg["content"] = content if isinstance(content, str) else (content or "")

    tool_calls = row.get("tool_calls")
    if tool_calls:
        msg["tool_calls"] = tool_calls
    tool_call_id = row.get("tool_call_id")
    if tool_call_id:
        msg["tool_call_id"] = tool_call_id
    name = row.get("name") or row.get("tool_name")
    if name:
        msg["name"] = name

    # spec FR-008：保留最近一轮的思考（reasoning → reasoning_content）
    reasoning = row.get("reasoning")
    if reasoning and isinstance(reasoning, str):
        msg["reasoning_content"] = reasoning

    return msg


def _filter_conversational(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """只保留对话型 turn（user / assistant / tool），过滤 system 注入行。

    turn 表里的 ``role`` 可能是 system（slow_ctx 注入的伪消息），这些是主意识
    的内部上下文切片，视觉模型不需要——它要的是"对话流水"。
    """
    keep: list[dict[str, Any]] = []
    for r in rows:
        role = r.get("role") or ""
        if role in {"user", "assistant", "tool"}:
            keep.append(r)
    return keep


def _trim_reasoning_to_last_assistant(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """只保留最后一条 assistant 消息的 reasoning_content，更早的摘掉。

    spec FR-008："保留最近一轮的思考"。把多轮 think 全塞给视觉模型既浪费 token
    又稀释信号——视觉模型只需要知道"主意识刚才在想什么"。
    """
    last_assistant_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "assistant":
            last_assistant_idx = i
            break
    if last_assistant_idx < 0:
        return messages
    out: list[dict[str, Any]] = []
    for i, m in enumerate(messages):
        if m.get("role") == "assistant" and i != last_assistant_idx:
            m = {k: v for k, v in m.items() if k != "reasoning_content"}
        out.append(m)
    return out


def build_slim_context(
    instance_id: str,
    *,
    session_id: str | None = None,
    chat_id: str | None = None,
    recent_turns: int = 5,
) -> list[dict[str, Any]]:
    """构建精简视觉上下文（只读投影）。

    数据源优先级：
      1. ``session_id`` 给定 → :meth:`RuntimeLogDB.list_turns_by_session`
      2. ``chat_id`` 给定 → :meth:`RuntimeLogDB.list_turns_by_chat`
      3. 都不给 → 取该实例最近 ``recent_turns`` 条 turn（跨 session/chat）

    返回的 messages 列表可直接拼到视觉模型调用前（作为历史轮次）。

    Args:
        instance_id: 实例 UUID（决定读哪个实例的 audit）。
        session_id: 指定 session（可选）。
        chat_id: 指定会话（可选，与 session_id 二选一）。
        recent_turns: 取最近几条对话（默认 5）。

    Returns:
        协议格式 messages 列表（已剥杂质、重命名 reasoning、只留最后一轮思考）。
        取不到任何数据时返回空列表（视觉模型退化为"无背景"，不报错）。
    """
    if not instance_id:
        logger.debug("build_slim_context: no instance_id, return empty context")
        return []

    try:
        from infrastructure.persistence.instance import get_audit

        audit = get_audit(instance_id)
    except Exception as exc:
        logger.warning("build_slim_context: cannot open audit for %s: %s", instance_id, exc)
        return []

    rows: list[dict[str, Any]] = []
    try:
        if session_id:
            rows = audit.list_turns_by_session(session_id)
        elif chat_id:
            rows = audit.list_turns_by_chat(chat_id, limit=recent_turns * 2)
        # 都不给：取最近 turn（用 list_turns_by_chat 需 chat，这里用一个轻量兜底——
        # 取最近 wake 的 turns）
        if not rows and not session_id and not chat_id:
            wakes = audit.list_wakes(limit=1)
            if wakes:
                wid = wakes[0].get("id")
                if wid:
                    rows = audit.list_turns(int(wid))
    except Exception as exc:
        logger.warning("build_slim_context: read audit failed for %s: %s", instance_id, exc)
        return []

    rows = _filter_conversational(rows)
    if not rows:
        return []

    # 取最近 N 条（audit 已按时间序，但 list_turns_by_chat 是 DESC+reverse，统一裁尾）
    if len(rows) > recent_turns * 2:
        rows = rows[-(recent_turns * 2):]

    messages = [_clean_turn_row(r) for r in rows]
    messages = _trim_reasoning_to_last_assistant(messages)

    # 再按 recent_turns 裁一次（按对话条数，不是 turn 行数——一个 LLM call 可能多行）
    if len(messages) > recent_turns * 2:
        messages = messages[-(recent_turns * 2):]

    logger.info(
        "build_slim_context: instance=%s session=%s chat=%s → %d messages",
        instance_id[:8], bool(session_id), bool(chat_id), len(messages),
    )
    return messages


def wake_meta_snapshot(instance_id: str, *, wake_id: int | None = None) -> dict[str, Any]:
    """取当前/指定 wake 的精简 meta（触发原因、chat），作为视觉背景的一部分。

    同样只读。取不到返回空 dict。
    """
    if not instance_id:
        return {}
    try:
        from infrastructure.persistence.instance import get_audit

        audit = get_audit(instance_id)
        if wake_id:
            w = audit.get_wake(wake_id)
        else:
            wakes = audit.list_wakes(limit=1)
            w = wakes[0] if wakes else None
        if not w:
            return {}
        meta = w.get("meta_json") or {}
        return {
            "trigger_type": meta.get("trigger_type") or "",
            "trigger_chat_id": meta.get("trigger_chat_id") or "",
            "reason": (meta.get("reason") or "")[:200],
        }
    except Exception as exc:
        logger.debug("wake_meta_snapshot failed for %s: %s", instance_id, exc)
        return {}
