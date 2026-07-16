"""Feature 002 — 认知演化工具集(给模型与 memory_hygiene skill 用)。

包装 domain/memory/memory/recall/unified/cognition_store.py 的 5 个 API:
  - promote_memory          经历→认知晋升
  - supersede_memory        认知被新认知取代(带 derived_from 反链)
  - revise_memory           模型原地修订认知 body
  - cluster_born_memory     多认知聚成更高阶元认知
  - signal_memory           给切片打 access/reference/verified/falsified 信号

设计原则:
  - 严格不重复 5 个底层函数的错误处理;每个工具把 args clean + 调底层 + JSON 输出,
    失败时返 ok=False + reason(让模型能看懂失败原因、可重试或放弃)
  - 与 set_entity_profile 同 schema 风格(给模型一致的"内存编辑工具"观感)
  - toolset 暂挂 "actions",模型根据场景调用(skill prompt 内嵌推荐调用顺序)
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from interfaces.tools.registry import registry

logger = logging.getLogger("interfaces.tools.memory_cognition")


def _j(obj: Any) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)


# ────────────── promote_memory ──────────────

def _handle_promote_memory(args: Dict[str, Any], **_) -> str:
    """把一条经历切片提纯成认知: 写入新 cognition slice + derived_from 反链。"""
    chunk_id = args.get("chunk_id")
    if not isinstance(chunk_id, int):
        try:
            chunk_id = int(chunk_id)
        except Exception:
            return _j({"ok": False, "reason": "chunk_id 必填且必须是 int"})
    summary = str(args.get("summary") or "").strip()
    if not summary:
        return _j({"ok": False, "reason": "summary 必填; 写下你对这段经历的理解/泛化结论"})
    entity_name = args.get("entity_name")
    if entity_name:
        entity_name = str(entity_name).strip() or None
    try:
        from domain.memory.memory.recall.unified.cognition_store import promote_one
        return _j(promote_one(chunk_id, summary=summary, entity_name=entity_name))
    except Exception as e:
        logger.exception("promote_memory failed")
        return _j({"ok": False, "reason": f"{type(e).__name__}: {e}"})


registry.register(
    name="promote_memory",
    toolset="actions",
    schema={
        "name": "promote_memory",
        "description": (
            "把一条经历切片提纯成认知。会创建新的 cognition slice (phase=cognition), "
            "derived_from 反向指向原经历,新认知 cognition_state=nascent。"
            "适用场景:反复验证过的经历、对某实体的总结性理解、应固化为规律的洞见。"
            "用 recall_entity / sense_entity 看到 chunk_id 后用本工具晋升。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "chunk_id": {"type": "integer", "description": "被提纯的经历切片 id"},
                "summary": {"type": "string", "description": "对经历的理解/总结/泛化结论。要清晰可被联想直接命中。"},
                "entity_name": {"type": "string", "description": "关联的实体名(可选)。同步到 entity_index 让 sense_entity 能看到。"},
            },
            "required": ["chunk_id", "summary"],
        },
    },
    handler=_handle_promote_memory,
    check_fn=lambda: True,
    emoji="📚",
)


# ────────────── supersede_memory ──────────────

def _handle_supersede_memory(args: Dict[str, Any], **_) -> str:
    """用新 body 取代某认知: 老 cognition 转 replaced + 双向链 + 新认知接管。"""
    old_chunk_id = args.get("old_chunk_id")
    if not isinstance(old_chunk_id, int):
        try:
            old_chunk_id = int(old_chunk_id)
        except Exception:
            return _j({"ok": False, "reason": "old_chunk_id 必填且必须是 int"})
    new_body = str(args.get("new_body") or "").strip()
    if not new_body:
        return _j({"ok": False, "reason": "new_body 必填; 写下取代旧认知的新版本"})
    new_authority = float(args.get("new_authority") or 0.8)
    entity_name = args.get("entity_name")
    if entity_name:
        entity_name = str(entity_name).strip() or None
    try:
        from domain.memory.memory.recall.unified.cognition_store import supersede_one
        return _j(supersede_one(
            old_chunk_id, new_body, new_authority=new_authority, entity_name=entity_name,
        ))
    except Exception as e:
        logger.exception("supersede_memory failed")
        return _j({"ok": False, "reason": f"{type(e).__name__}: {e}"})


registry.register(
    name="supersede_memory",
    toolset="actions",
    schema={
        "name": "supersede_memory",
        "description": (
            "用新 body 取代某认知。老认知被标记为 replaced + supersede_by 指向新认知; "
            "新认知 derived_from 包含老 id;双方 row 都保留 (永不硬删,可溯源)。"
            "适用场景:旧规则被新规则替代、教训被修正、profile 信息过时更新。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "old_chunk_id": {"type": "integer", "description": "要被取代的老认知切片 id"},
                "new_body": {"type": "string", "description": "新认知的 body 文本"},
                "new_authority": {"type": "number", "description": "新认知权威度 0-1, 默认 0.8"},
                "entity_name": {"type": "string", "description": "关联的实体名(可选)"},
            },
            "required": ["old_chunk_id", "new_body"],
        },
    },
    handler=_handle_supersede_memory,
    check_fn=lambda: True,
    emoji="🔄",
)


# ────────────── revise_memory ─────────────

def _handle_revise_memory(args: Dict[str, Any], **_) -> str:
    """原地修订某认知的 body, cognition_state 转 revising。"""
    chunk_id = args.get("chunk_id")
    if not isinstance(chunk_id, int):
        try:
            chunk_id = int(chunk_id)
        except Exception:
            return _j({"ok": False, "reason": "chunk_id 必填且必须是 int"})
    new_body = str(args.get("new_body") or "").strip()
    if not new_body:
        return _j({"ok": False, "reason": "new_body 必填"})
    version_log = str(args.get("version_log") or "").strip()
    try:
        from domain.memory.memory.recall.unified.cognition_store import revise_one
        return _j(revise_one(chunk_id, new_body=new_body, version_log=version_log))
    except Exception as e:
        logger.exception("revise_memory failed")
        return _j({"ok": False, "reason": f"{type(e).__name__}: {e}"})


registry.register(
    name="revise_memory",
    toolset="actions",
    schema={
        "name": "revise_memory",
        "description": (
            "原地修订某认知的 body (不动 derived_from / entity_links; "
            " cognition_state 转 revising;旧 body 进 provenance)。"
            "适用场景:措辞微调、补充细节、同向修订。"
            "若是新旧差异大,应该用 supersede_memory 而非本工具。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "chunk_id": {"type": "integer", "description": "要修订的认知切片 id"},
                "new_body": {"type": "string", "description": "修订后的 body 文本"},
                "version_log": {"type": "string", "description": "简短修订说明(进 provenance)"},
            },
            "required": ["chunk_id", "new_body"],
        },
    },
    handler=_handle_revise_memory,
    check_fn=lambda: True,
    emoji="✏️",
)


# ───────────── cluster_born_memory ─────────────

def _handle_cluster_born_memory(args: Dict[str, Any], **_) -> str:
    """从多认知聚出更高阶的认知 (B 路径)。"""
    member_ids = args.get("member_chunk_ids") or []
    if not isinstance(member_ids, list) or len(member_ids) < 2:
        return _j({"ok": False, "reason": "member_chunk_ids 必填且至少 2 条"})
    try:
        member_ids = [int(i) for i in member_ids]
    except Exception:
        return _j({"ok": False, "reason": "member_chunk_ids 各项必须是 int"})
    summary = str(args.get("summary") or "").strip()
    if not summary:
        return _j({"ok": False, "reason": "summary 必填; 写下从这些认知抽象出的元认知"})
    entity_name = args.get("entity_name")
    if entity_name:
        entity_name = str(entity_name).strip() or None
    try:
        from domain.memory.memory.recall.unified.cognition_store import cluster_born_persist
        return _j(cluster_born_persist(
            member_ids, summary=summary, entity_name=entity_name,
        ))
    except Exception as e:
        logger.exception("cluster_born_memory failed")
        return _j({"ok": False, "reason": f"{type(e).__name__}: {e}"})


registry.register(
    name="cluster_born_memory",
    toolset="actions",
    schema={
        "name": "cluster_born_memory",
        "description": (
            "从一组近义的认知切片抽象出更高阶的元认知 (cognition_state=higher)。"
            "新元认知 derived_from 包含所有成员 id。适用场景:多教训总结出共同规律、"
            "多规则提炼出原则。\"思考出主题\"的过程。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "member_chunk_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "参与聚合的认知切片 id 列表(至少 2 条)",
                },
                "summary": {"type": "string", "description": "新元认知的总结文本"},
                "entity_name": {"type": "string", "description": "关联的实体名(可选)"},
            },
            "required": ["member_chunk_ids", "summary"],
        },
    },
    handler=_handle_cluster_born_memory,
    check_fn=lambda: True,
    emoji="💡",
)


# ───────────── signal_memory ─────────────

def _handle_signal_memory(args: Dict[str, Any], **_) -> str:
    """给一条切片打信号(access/reference/verified/falsified)。"""
    chunk_id = args.get("chunk_id")
    if not isinstance(chunk_id, int):
        try:
            chunk_id = int(chunk_id)
        except Exception:
            return _j({"ok": False, "reason": "chunk_id 必填且必须是 int"})
    signal = str(args.get("signal") or "").strip().lower()
    if signal not in ("access", "reference", "verified", "falsified"):
        return _j({
            "ok": False,
            "reason": "signal 必须是 access / reference / verified / falsified 之一",
        })
    reason = str(args.get("reason") or "").strip()
    try:
        from domain.memory.memory.recall.unified.cognition_store import apply_signal
        return _j(apply_signal(chunk_id, signal, reason=reason))
    except Exception as e:
        logger.exception("signal_memory failed")
        return _j({"ok": False, "reason": f"{type(e).__name__}: {e}"})


registry.register(
    name="signal_memory",
    toolset="actions",
    schema={
        "name": "signal_memory",
        "description": (
            "给一条切片打信号,驱动其参数演化。注意三大铁律:"
            "  access 命中事件(只动 activation);reference 被复述采纳(累积 verification);"
            "  verified 仅认知类接受结构性强化(+authority);  falsified 仅认知类被认反(-authority)。"
            "适用场景:任务结果验证某规则是否成立、记录某经历被反复复述、对被淘汰认知打 falsified。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "chunk_id": {"type": "integer", "description": "目标切片 id"},
                "signal": {
                    "type": "string",
                    "enum": ["access", "reference", "verified", "falsified"],
                    "description": "信号类型",
                },
                "reason": {"type": "string", "description": "简短原因(可选, falsified 时建议填)"},
            },
            "required": ["chunk_id", "signal"],
        },
    },
    handler=_handle_signal_memory,
    check_fn=lambda: True,
    emoji="📡",
)


__all__ = ["_j"]  # 内部 helper
