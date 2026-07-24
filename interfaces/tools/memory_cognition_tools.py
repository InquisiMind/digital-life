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
    # V3: 可选 payload (cog_key + value + premise + rationale)
    payload = args.get("payload")
    if payload is not None and not isinstance(payload, dict):
        try:
            import json as _json
            payload = _json.loads(payload)
        except Exception:
            payload = None
    if isinstance(payload, dict) and not payload.get("key"):
        payload = None
    try:
        from domain.memory.memory.recall.unified.cognition_store import promote_one
        return _j(promote_one(chunk_id, summary=summary, entity_name=entity_name, payload=payload))
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
            "derived_from 反向指向原经历,新认知 cognition_state=nascent。\n"
            "适用场景:反复验证过的经历、对某实体的总结性理解、应固化为规律的洞见。\n"
            "用 recall_entity / sense_entity 看到 chunk_id 后用本工具晋升。\n\n"
            "(V3) 提供 payload.cog_key 时走精确去重路径(不被 cos 误拦). "
            "premise/rationale 字段保留推论背景."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "chunk_id": {"type": "integer", "description": "被提纯的经历切片 id"},
                "summary": {"type": "string", "description": "对经历的理解/总结/泛化结论。要清晰可被联想直接命中。"},
                "entity_name": {"type": "string", "description": "关联的实体名(可选)。同步到 entity_index 让 sense_entity 能看到。"},
                "payload": {
                    "type": "object",
                    "description": (
                        "(V3 可选) 结构化主键. 提供 cog_key 则走精确查重不被 cos 误拦, "
                        "提供的 premise(看到什么事实)/ rationale(为什么这么判断) 会随认知一起保存, "
                        "下次联想命中时把推论链路也注入给模型."
                    ),
                    "properties": {
                        "key": {"type": "string", "description": "subject:predicate"},
                        "value": {"description": "任意 JSON"},
                        "premise": {"type": "string", "description": "前提: 你判断时针对于什么事实/状态"},
                        "rationale": {"type": "string", "description": "推理依据: 为什么从 premise 推到这个结论"},
                    },
                },
            },
            "required": ["chunk_id", "summary"],
        },
    },
    handler=_handle_promote_memory,
    check_fn=lambda: True,
    emoji="📚",
    schema_visible=False,  # V6 工具精简: 降级
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
    # V3 #2: 支持 new_payload — 若是参数类认知覆盖, 新 value 同步 profile.derived_fields
    new_payload = args.get("new_payload")
    if new_payload is not None and not isinstance(new_payload, dict):
        try:
            import json as _json_mod  # ⚠ 不能 import as _j, 会覆盖模块级 _j helper
            new_payload = _json_mod.loads(new_payload)
        except Exception:
            new_payload = None
    try:
        from domain.memory.memory.recall.unified.cognition_store import supersede_one
        return _j(supersede_one(
            old_chunk_id, new_body, new_authority=new_authority, entity_name=entity_name,
            new_payload=new_payload,
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
            "新认知 derived_from 包含老 id;双方 row 都保留 (永不硬删,可溯源)。\n"
            "适用场景:旧规则被新规则替代、教训被修正、profile 信息过时更新。\n\n"
            "(V3) 参数类认知覆盖时建议提供 new_payload 新 value — 系统"
            "会自动同步该实体 profile 的 derived_fields, 让 sense_entity"
            "看到的概念卡始终反映最新认知."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "old_chunk_id": {"type": "integer", "description": "要被取代的老认知切片 id"},
                "new_body": {"type": "string", "description": "新认知的 body 文本"},
                "new_authority": {"type": "number", "description": "新认知权威度 0-1, 默认 0.8"},
                "entity_name": {"type": "string", "description": "关联的实体名(可选)"},
                "new_payload": {
                    "type": "object",
                    "description": "(V3 可选) 新版本的结构化 value, 覆盖老 payload. 形如 {\"value\": -0.08}",
                    "properties": {
                        "value": {"description": "新 value (覆盖老 payload.value)"},
                    },
                },
            },
            "required": ["old_chunk_id", "new_body"],
        },
    },
    handler=_handle_supersede_memory,
    check_fn=lambda: True,
    emoji="🔄",
    schema_visible=False,  # V6: 合并到 update_cognition
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
    schema_visible=False,  # V6 工具精简: 降级
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
    schema_visible=False,  # V6 工具精简: 降级
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


# V6: signal_memory 废弃 (强化质疑在 session digest 自动跑). handler 保留.


__all__ = ["_j"]  # 内部 helper


# ────────────── add_cognition (新统一入口) ──────────────

def _handle_add_cognition(args: Dict[str, Any], **_) -> str:
    """直接写入一条新认知(不需要从经历 chunk promote)。

    统一入口: 之前的 add_lesson / update_rules / set_entity_profile 的认知写入
    全部走这个。内部:
      1. 写 chunk(phase=cognition, source=source_category or "knowledge")
      2. 自动搜索相似认知 → cos>0.92 返回重复警告, 0.55-0.92 填 derived_from
      3. entity_links 存入 chunk → 后续召回桥接
      4. **V2**: 如果提供 payload(含 key), 走精确查重/查冲突(优先级高于 cos 路径)
    """
    text = str(args.get("text") or "").strip()
    if not text:
        return _j({"ok": False, "reason": "text 必填; 写下要沉淀的判断/事实/规则"})
    entity_links = args.get("entity_links") or []
    if isinstance(entity_links, str):
        entity_links = [s.strip() for s in entity_links.split(",") if s.strip()]
    if not isinstance(entity_links, list):
        entity_links = []
    entity_links = [str(s).strip() for s in entity_links if str(s).strip()]
    source_category = str(args.get("source_category") or "").strip().lower()
    if source_category not in ("", "lesson", "rule", "insight", "fact", "knowledge"):
        source_category = ""
    source = source_category or "knowledge"

    # V2: 可选 payload
    payload = args.get("payload")
    if payload is not None and not isinstance(payload, dict):
        # 容忍 string 形式(模型偶尔会回 JSON 字符串)
        try:
            import json as _json
            payload = _json.loads(payload)
        except Exception:
            payload = None
    # V3 (2026-07-23 #3): payload 含 premise/rationale 但无 key → 仍保留 (作为背景信息)
    # 但 cog_key 精确路径只在有 key 时跑(逻辑上 key 必须有)
    if isinstance(payload, dict) and not payload.get("key"):
        # 没有 key, 看是否含 premise/rationale; 否则丢弃
        if not (payload.get("premise") or payload.get("rationale")):
            payload = None

    # V6 #2: trigger_scenarios 作为召回锚点 — 追加到 attention_tokens
    # (不污染 entity_links 的语义, 但参与 _boost_attention 匹配)
    trigger_scenarios = []
    if isinstance(payload, dict):
        trigger_scenarios = payload.get("trigger_scenarios") or []
    if not trigger_scenarios:
        trigger_scenarios = args.get("trigger_scenarios") or []
    if isinstance(trigger_scenarios, str):
        trigger_scenarios = [trigger_scenarios]

    try:
        from domain.memory.memory.recall.unified.cognition_store import add_cognition_direct
        # V6 #2: trigger_scenarios 合并进 entity_links 作为召回锚点
        # (语义上 trigger_scenarios 就是"什么上下文中该想起这条认知"= 扩展 entity_links)
        # 单独存 payload 里保留语义, 但参与召回时和 entity_links 一样做 boost
        merged_links = list(entity_links)
        for ts in trigger_scenarios:
            ts = str(ts).strip()
            if ts and ts not in merged_links and len(ts) >= 2:
                merged_links.append(ts)
        return _j(add_cognition_direct(
            text=text,
            entity_links=merged_links,
            source=source,
            payload=payload,
        ))
    except Exception as e:
        logger.exception("add_cognition failed")
        return _j({"ok": False, "reason": f"{type(e).__name__}: {e}"})


registry.register(
    name="add_cognition",
    toolset="actions",
    schema={
        "name": "add_cognition",
        "description": (
            "写入一条新认知(高价值的判断/事实/规则/观点)。这是知识沉淀的统一入口——\n"
            "对话中产生的新见解、复盘后的总结、经验教训,都通过这个工具写进认知库。\n\n"
            "写入后系统会自动:\n"
            "  · 搜索相似已有认知 → 高度重复的提示用 supersede_memory 覆盖\n"
            "  · 关联已有的相似认知 → 形成关联网络(后续联想能从一条跳到相关的)\n"
            "  · (V2) 如果提供 payload 含 key, 走精确主键查重/查冲突(高于 cos 路径)\n\n"
            "entity_links 是关键词索引: 将来提到这些词时该认知更容易被联想命中。\n"
            "比如 ['金开新能', '止损线'] → 下次问止损线时这条优先浮出。\n\n"
            "**payload (可选, V2)**: 适合\"实体+具体判断\"的认知(规则/事实/参数)。\n"
            "  · 例: 金开新能的止损线 → {\"key\": \"金开新能:stop_loss_line\", \"value\": -0.07}\n"
            "  · key 格式: `subject:predicate` (你自己拼, 简明即可)\n"
            "  · value: 任意结构(数字/字符串/对象), 用于精确去重\n"
            "  · **polarity** (强烈推荐用于偏好/判断类): positive/negative/中性\n"
            "     这样系统才能区分\"Alpha 喜欢 review\"和\"Alpha 不喜欢 review\" (cos 视角两者 0.93 几乎一样).\n"
            "  · 时间敏感的事实可加 ttl_h(小时), 自动过期\n"
            "  纯主观抽象认知(如\"做事要专注\")不需要 payload, 直接给 text 即可。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "认知内容。一句话判断/事实/规则。清晰可被联想命中。",
                },
                "entity_links": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "关键词索引, 如 ['金开新能', '止损线', '风控']。将来提到这些词时该认知优先召回。",
                },
                "source_category": {
                    "type": "string",
                    "enum": ["", "lesson", "rule", "insight", "fact"],
                    "description": "语义分类(不影响逻辑,纯标注), 默认 knowledge",
                },
                "payload": {
                    "type": "object",
                    "description": (
                        "(V2 可选) 结构化主键, 用于精确去重/查询/冲突检测。"
                        "适合规则/参数/事实类认知。格式: "
                        "{\"key\": \"主体:谓词\", \"value\": ..., \"polarity\": \"...\", \"ttl_h\": 24?}。"
                        "同 key 同 value 自动跳过, 同 key 不同 value 标记为冲突候选。"
                    ),
                    "properties": {
                        "key": {"type": "string", "description": "subject:predicate 形式, 自己拼"},
                        "value": {"description": "任意 JSON (数字/字符串/对象)"},
                        "value_type": {
                            "type": "string",
                            "enum": ["number", "percentage", "text", "boolean", "enum", "any"],
                            "description": "value 的类型标注, 默认 any",
                        },
                        "ttl_h": {
                            "type": ["number", "null"],
                            "description": "可选 TTL(小时), 超时自动 archived。fact 默认 24, 其它默认永久",
                        },
                        "scope": {
                            "type": "string",
                            "description": "适用范围 (一般规律 vs 特定账户/场景), 可省略",
                        },
                        "condition": {
                            "type": "object",
                            "description": "触发条件对象 (可选), e.g. {\"market_open\": true}",
                        },
                        "polarity": {
                            "type": "string",
                            "enum": ["positive", "negative", "neutral"],
                            "description": (
                                "V3 #1: 判断的极性. **强烈推荐用于偏好/判断类认知**。"
                                "embedding 对 '喜欢' vs '不喜欢' 区分度极低(cos≈0.93), "
                                "靠 polarity 显式标注才能区分. "
                                "例: 'Alpha 喜欢 review' → polarity=positive; "
                                "'Alpha 讨厌 review' → polarity=negative (同 predicate 不同 polarity 系统标冲突)."
                            ),
                        },
                        "trigger_scenarios": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "V6 #2: 触发情景。**推荐 2-3 条**。\n"
                                "写下'下次我处于什么**具体情境**时,这条认知对我有用'。\n"
                                "**注意: 必须和源认知是同一场景, 不是关联主题。**\n"
                                "✅ 正确例 (认知'小明讨厌雨天'):\n"
                                "  trigger_scenarios=['小明出门没带伞','看到下雨想到小明']\n"
                                "❌ 错误例:\n"
                                "  trigger_scenarios=['下雨天计划户外'] ← 这是关联主题, 和小明无关\n"
                                "本质: 你在给未来的自己留条线索 —— '遇到这种场景, 翻出这条结论'."
                            ),
                        },
                    },
                    "required": ["key"],
                },
            },
            "required": ["text"],
        },
    },
    handler=_handle_add_cognition,
    check_fn=lambda: True,
    emoji="🧠",
)


# ────────────── delete_cognition ──────────────

def _handle_delete_cognition(args: Dict[str, Any], **_) -> str:
    """删除一条认知。删除前检查是否被其它认知 derived_from 引用。"""
    chunk_id = args.get("chunk_id")
    if not isinstance(chunk_id, int):
        try:
            chunk_id = int(chunk_id)
        except Exception:
            return _j({"ok": False, "reason": "chunk_id 必填且必须是 int"})
    try:
        from domain.memory.memory.recall.unified.cognition_store import delete_cognition
        return _j(delete_cognition(chunk_id))
    except Exception as e:
        logger.exception("delete_cognition failed")
        return _j({"ok": False, "reason": f"{type(e).__name__}: {e}"})


registry.register(
    name="delete_cognition",
    toolset="actions",
    schema={
        "name": "delete_cognition",
        "description": (
            "删除一条认知(永久移除)。如果被其它认知的 derived_from 引用,"
            "会返回警告并建议用 supersede_memory 替代删除。"
            "适用场景: 确认完全无用的认知、重复写入后的清理、事实严重错误的认知。"
            "\n\n注: 想保留溯源但让联想不再命中这条过时认知, 用 mark_obsolete 更安全 (软标 archived)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "chunk_id": {"type": "integer", "description": "要删除的认知切片 id"},
            },
            "required": ["chunk_id"],
        },
    },
    handler=_handle_delete_cognition,
    check_fn=lambda: True,
    emoji="🗑️",
    schema_visible=False,  # V6: 合并到 update_cognition
)


# ────────────── V6 (2026-07-24): mark_obsolete 软标过时 ──────────────


def _handle_mark_obsolete(args: Dict[str, Any], **_) -> str:
    """手动软标某认知 archived。不硬删, 但默认召回全部过滤, 不再注入 prompt。

    Zero/Alpha 7/24 反馈: "lesson 标签拦不住旧记忆残留被联想激活"。
    supersede 适合新认知接管老的; 但 pure 过时 (没接班人) 用 mark_obsolete 更直接。
    """
    chunk_id = args.get("chunk_id")
    if not isinstance(chunk_id, int):
        try:
            chunk_id = int(chunk_id)
        except Exception:
            return _j({"ok": False, "reason": "chunk_id 必填且必须是 int"})
    reason = str(args.get("reason") or "").strip()
    try:
        from domain.memory.memory.recall.unified.cognition_store import mark_obsolete
        return _j(mark_obsolete(chunk_id, reason=reason))
    except Exception as e:
        logger.exception("mark_obsolete failed")
        return _j({"ok": False, "reason": f"{type(e).__name__}: {e}"})


registry.register(
    name="mark_obsolete",
    toolset="actions",
    schema={
        "name": "mark_obsolete",
        "description": (
            "把一条认知软标为'archived 已过时' — 默认召回全部过滤, 不再注入 prompt。"
            "记录仍保留(可被 recall_cognition_by_key(include_history=True) 查), 溯源链不断."
            "\n\n与 delete_cognition 区别: mark_obsolete 软标安全, 适合要不要彻底删还在犹豫的认知。"
            "与 supersede_memory 区别: supersede 需要新认知接班, mark_obsolete 不需要接班, "
            "适合 pure 过时(没后继认知)或 confirmed 错误认知."
            "\n\n案例(7/24 反馈): '金开新能 SL -7%' 那种混淆产物, 确认是 bug 后直接 mark_obsolete, "
            "下次联想再不会命中这个错值."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "chunk_id": {"type": "integer", "description": "要标过时的认知 id"},
                "reason": {"type": "string", "description": "(可选)为什么标过时, 写一句话便于日后回看"},
            },
            "required": ["chunk_id"],
        },
    },
    handler=_handle_mark_obsolete,
    check_fn=lambda: True,
    emoji="🚫",
    schema_visible=False,  # V6: 合并到 update_cognition
)


# ────────────── V2 (2026-07-23): 结构化精确查询 ──────────────


def _handle_recall_cognition_by_key(args: Dict[str, Any], **_) -> str:
    """V2: 按 (subject, predicate) 精确召回认知。
    比 recall_entity / unified_recall 准 — 后者是模糊匹配, 本函数是 1:1 主键查询。
    适合"我对 X 的 Y 的认知/规则现在是什么" 这种明确意图的检索。
    """
    subject = str(args.get("subject") or "").strip()
    if not subject:
        return _j({"ok": False, "reason": "subject 必填, 如 '金开新能'"})
    predicate = str(args.get("predicate") or "").strip() or None
    include_history = bool(args.get("include_history"))
    try:
        from domain.memory.memory.recall.unified.cognition_store import recall_cognition_by_key
        return _j(recall_cognition_by_key(subject, predicate, include_history=include_history))
    except Exception as e:
        logger.exception("recall_cognition_by_key failed")
        return _j({"ok": False, "reason": f"{type(e).__name__}: {e}"})


registry.register(
    name="recall_cognition_by_key",
    toolset="memory",
    schema={
        "name": "recall_cognition_by_key",
        "description": (
            "V2: 按 (subject, predicate) 主键精确召回认知。"
            "比 recall_entity 准 — 后者是模糊联想, 本函数是 1:1 主键查询。\n\n"
            "适合场景: \"我对 X 的某项规则现在是什么?\" / \"调出该 key 的所有版本\"。"
            "调用前可先调 list_cognition_keys 看看有哪些 key。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "subject": {"type": "string", "description": "主体名, 如 '金开新能' / '苹果' / '夏令作息'"},
                "predicate": {
                    "type": "string",
                    "description": "谓词, 如 'stop_loss_line' / 'position_size' / 'daily_start_time'。省略 → 返回该 subject 所有谓词",
                },
                "include_history": {
                    "type": "boolean",
                    "description": "是否返回已被 supersede_memory / archived 的旧版本(看演化链), 默认 false",
                },
            },
            "required": ["subject"],
        },
    },
    handler=_handle_recall_cognition_by_key,
    check_fn=lambda: True,
    emoji="🔑",
)


def _handle_list_cognition_keys(args: Dict[str, Any], **_) -> str:
    """V2: 列出所有 cog_key (可选过滤某 subject), 让你知道\"自己知道什么\"。"""
    subject = str(args.get("subject") or "").strip() or None
    try:
        from domain.memory.memory.recall.unified.cognition_store import list_cognition_keys
        return _j(list_cognition_keys(subject))
    except Exception as e:
        logger.exception("list_cognition_keys failed")
        return _j({"ok": False, "reason": f"{type(e).__name__}: {e}"})


# V6: list_cognition_keys 废弃. handler 保留供 console API.


# ────────────── V3 (2026-07-23) #4: dream 阶段冲突桶查询 ──────────────


def _handle_find_conflict_buckets(args: Dict[str, Any], **_) -> str:
    """V3: 列出认知库中需要 dream 检测的冲突候选桶。返回:
      - 精确 key 桶 (find_conflicting_keys): 同 cog_key 不同 chunk
      - 叙述类桶 (find_narrative_conflict_buckets): 共享 entity_links ≥2 的认知分组
    """
    try:
        from domain.memory.memory.recall.unified.cognition_store import (
            find_conflicting_keys, find_narrative_conflict_buckets,
        )
        precise = find_conflicting_keys()
        narrative = find_narrative_conflict_buckets()
        return _j({
            "ok": True,
            "precise_key_buckets": precise,
            "narrative_link_buckets": narrative,
            "summary": f"{len(precise)} 精确 key 桶 + {len(narrative)} 叙述桶",
            "note": (
                "对每个桶里的认知, 调 recall_cognition_by_key / recall_memory 拉回原文, "
                "语义判断: subsume(归一)/supersede(覆盖)/split(分拆)/keep(保留). "
                "0 个桶说明认知库干净."
            ),
        })
    except Exception as e:
        logger.exception("find_conflict_buckets failed")
        return _j({"ok": False, "reason": f"{type(e).__name__}: {e}"})


registry.register(
    name="find_conflict_buckets",
    toolset="memory",
    schema={
        "name": "find_conflict_buckets",
        "description": (
            "V3 dream 工具: 列出需要冲突检测的候选桶, 含精确 key 桶(find_conflicting_keys) "
            "和叙述类桶(find_narrative_conflict_buckets). 一晚通常 0-5 桶, 集中处理."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    handler=_handle_find_conflict_buckets,
    check_fn=lambda: True,
    emoji="🔍",
)


# ────────────── V3 (2026-07-24) #6: 认知状态自检 ──────────────


def _handle_cognition_health(args: Dict[str, Any], **_) -> str:
    """认知库健康快照 — 一眼看 evidence/challenge/supersede 全过程数据.
    可以让模型在 dream 用, 也可以让用户直接调, 看"强化/质疑有没有在跑".
    """
    try:
        from domain.memory.memory.recall.vector import _get_db
        db = _get_db()
        try:
            # 总体分布
            state_counts = dict(
                (r[0] or "∅", r[1])
                for r in db.execute(
                    "SELECT cognition_state, count(*) FROM chunks "
                    "WHERE phase='cognition' GROUP BY cognition_state"
                ).fetchall()
            )
            evidence_dist = dict(
                (r[0], r[1])
                for r in db.execute(
                    "SELECT evidence_count, count(*) FROM chunks "
                    "WHERE phase='cognition' GROUP BY evidence_count ORDER BY evidence_count"
                ).fetchall()
            )
            challenge_dist = dict(
                (r[0], r[1])
                for r in db.execute(
                    "SELECT challenge_count, count(*) FROM chunks "
                    "WHERE phase='cognition' GROUP BY challenge_count ORDER BY challenge_count"
                ).fetchall()
            )
            # 无 cog_key 的比例 (predicate 还没被模型写的覆盖)
            no_key = db.execute(
                "SELECT count(*) FROM chunks WHERE phase='cognition' "
                "AND (cog_key IS NULL OR cog_key='') "
                "AND cognition_state NOT IN ('replaced','archived')"
            ).fetchone()[0]
            total_active = db.execute(
                "SELECT count(*) FROM chunks WHERE phase='cognition' "
                "AND cognition_state NOT IN ('replaced','archived')"
            ).fetchone()[0]
            superseded_count = db.execute(
                "SELECT count(*) FROM chunks WHERE phase='cognition' "
                "AND supersede_by IS NOT NULL"
            ).fetchone()[0]
            return _j({
                "ok": True,
                "state": state_counts,
                "evidence_distribution": evidence_dist,
                "challenge_distribution": challenge_dist,
                "summary": {
                    "total_active": total_active,
                    "with_evidence_ge_1": sum(v for k, v in evidence_dist.items() if k >= 1),
                    "with_challenge_ge_1": sum(v for k, v in challenge_dist.items() if k >= 1),
                    "no_cog_key": no_key,
                    "superseded_total": superseded_count,
                    "predicate_coverage_pct": round(
                        (total_active - no_key) / max(total_active, 1) * 100, 1
                    ),
                },
                "interpretation": (
                    "evidence/challenge = 强化/质疑被真正调用过的次数; "
                    "高 = 演化活跃; 0 = 静止死知识. "
                    "no_cog_key = 待 dream 回填 predicate 的认知数. "
                    "superseded_total = 历史被覆盖(已沉淀过质疑决策)的总数."
                ),
            })
        finally:
            db.close()
    except Exception as e:
        logger.exception("cognition_health failed")
        return _j({"ok": False, "reason": f"{type(e).__name__}: {e}"})


# V6: cognition_health 废弃 (运维工具). handler 保留.


# ════════════════════════════════════════════════════════════════
# V6: update_cognition — 合并 supersede/obsolete/delete 三合一
# ════════════════════════════════════════════════════════════════


def _handle_update_cognition(args: Dict[str, Any], **_) -> str:
    """认知生命周期操作四合一: preview / supersede / obsolete / delete.

    preview 模式 (交互式):
      返回该认知的全文 + 状态 + 被谁引用 + 同 entity_links 邻居.
      模型看完后决定要不要真删/覆盖, 再带 action=obsolete/delete/supersede 调一次.
      类似 rest 工具的提示卡模式 — 不可逆操作前先看影响范围.
    """
    action = str(args.get("action") or "").strip().lower()
    chunk_id = args.get("chunk_id")
    if not isinstance(chunk_id, int):
        try:
            chunk_id = int(chunk_id)
        except Exception:
            return _j({"ok": False, "reason": "chunk_id 必填且必须是 int"})

    # ── preview: 交互式预览 ──
    if action == "preview":
        try:
            from domain.memory.memory.recall.unified.cognition_store import (
                load_slice_by_id, check_supersede_neighbors_for_stale_links,
            )
            sl = load_slice_by_id(chunk_id)
            if sl is None or sl.phase != "cognition":
                return _j({"ok": False, "reason": f"#{chunk_id} 不存在或不是认知"})
            info = {
                "ok": True,
                "preview": True,
                "chunk_id": chunk_id,
                "text": sl.body[:300],
                "source": sl.source,
                "cognition_state": sl.cognition_state,
                "evidence_count": sl.evidence_count,
                "challenge_count": sl.challenge_count,
                "authority": round(sl.authority, 2),
                "entity_links": sl.entity_links,
                "cog_key": sl.cog_key,
                "derived_from": sl.derived_from,
            }
            # 邻居: 同 entity_links 的认知 (影响范围)
            try:
                nbrs = check_supersede_neighbors_for_stale_links(chunk_id, chunk_id)
                if nbrs:
                    info["neighbors"] = [
                        {"chunk_id": n["chunk_id"], "text": (n.get("text") or "")[:60]}
                        for n in nbrs[:5]
                    ]
                    info["hint"] = (
                        f"⚠️ 这条认知有 {len(nbrs)} 条同 entity_links 的邻居. "
                        f"覆盖/删除它可能影响这些认知的语义完整性. "
                        f"确认后用 action=obsolete/delete/supersede 再调一次."
                    )
                else:
                    info["hint"] = "无邻居影响. 确认后用 action=obsolete/delete/supersede 执行."
            except Exception:
                pass
            return _j(info)
        except Exception as e:
            return _j({"ok": False, "reason": f"{type(e).__name__}: {e}"})

    # ── supersede ──
    elif action == "supersede":
        new_body = str(args.get("new_body") or "").strip()
        if not new_body:
            return _j({"ok": False, "reason": "supersede 需要 new_body"})
        new_payload = args.get("new_payload")
        entity_name = args.get("entity_name")
        if entity_name:
            entity_name = str(entity_name).strip() or None
        from domain.memory.memory.recall.unified.cognition_store import supersede_one
        return _j(supersede_one(chunk_id, new_body,
                                 new_authority=float(args.get("new_authority") or 0.8),
                                 entity_name=entity_name,
                                 new_payload=new_payload))

    # ── obsolete ──
    elif action == "obsolete":
        reason = str(args.get("reason") or "").strip()
        from domain.memory.memory.recall.unified.cognition_store import mark_obsolete
        return _j(mark_obsolete(chunk_id, reason=reason))

    # ── delete ──
    elif action == "delete":
        from domain.memory.memory.recall.unified.cognition_store import delete_cognition
        return _j(delete_cognition(chunk_id))

    else:
        return _j({"ok": False, "reason": f"action 必须是 preview/supersede/obsolete/delete, 收到: {action}"})


registry.register(
    name="update_cognition",
    toolset="actions",
    schema={
        "name": "update_cognition",
        "description": (
            "调整已有认知。四种操作:\n"
            "  · preview: 查看该认知全文+状态+邻居 (交互式, 删/覆盖前先看)\n"
            "  · supersede: 用新内容覆盖 (老的标 replaced, 新的接管, 溯源链保留)\n"
            "  · obsolete: 软标过时 (召回不再命中, 但记录保留)\n"
            "  · delete: 硬删 (有引用时拒绝)\n"
            "建议: 先 preview 看影响范围 → 再 supersede/obsolete/delete.\n"
            "选择: 有新认知接班 → supersede; 确认过时没接班 → obsolete; 完全垃圾 → delete."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "chunk_id": {"type": "integer", "description": "目标认知 id"},
                "action": {
                    "type": "string",
                    "enum": ["preview", "supersede", "obsolete", "delete"],
                    "description": "操作类型",
                },
                "new_body": {"type": "string", "description": "(supersede 必填) 新认知内容"},
                "new_payload": {"type": "object", "description": "(supersede 可选) 新结构化值"},
                "reason": {"type": "string", "description": "(obsolete 可选) 为什么标过时"},
            },
            "required": ["chunk_id", "action"],
        },
    },
    handler=_handle_update_cognition,
    check_fn=lambda: True,
    emoji="🔧",
)
