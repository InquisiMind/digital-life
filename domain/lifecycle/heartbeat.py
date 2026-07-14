"""L4 唤醒 prompt 构建。

核心职责：
  1. 根据唤醒原因（reason）和待处理事件构建 action_prompt（要立刻做的事）
  2. 构建 ref_context（参考材料：今日目标、记忆、规则、汇报策略等）
  3. 单事件唤醒：内联事件详情到 prompt，模型可直接回复
  4. 多事件唤醒：只显示清单，让模型逐个 sense_event_detail 消费

唤醒 prompt 结构：
  action_prompt = 唤醒原因 + 消息内容 + 执行要求
  ref_context = 今日目标 + 记忆上下文 + 自审提示 + 汇报策略 + 向量召回 + 事件摘要
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("digital_life.lifecycle.heartbeat")

_repo_root = Path(__file__).resolve().parents[2]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

# 唤醒实体追踪：记录 wake prompt 中 [实体触发记忆] 块已展示的 memory ID
# scheduler 在 prompt 构建后读取这些 ID，传给 AIAgent.mark_memories_presented()
# 防止 mid-session 重复注入相同的记忆
_presented_memory_ids: set[str] = set()

# 唤醒事件追踪：记录 wake prompt 中展示的事件 ID
# cron_lifecycle 在 mid-session 注入时检查这些 ID，避免重复注入已展示的事件
_presented_event_ids: set[int] = set()


def get_presented_memory_ids() -> set[str]:
    """返回并清空本次 wake prompt 中已展示的实体记忆 ID 集合。

    scheduler 在 prompt 构建后调用此方法，将 ID 传给 AIAgent.mark_memories_presented()，
    防止 mid-session 的 _inject_entity_recall 重复注入相同记忆。
    """
    global _presented_memory_ids
    ids = _presented_memory_ids.copy()
    _presented_memory_ids.clear()
    return ids


def get_presented_event_ids() -> set[int]:
    """返回当前 wake prompt 中已展示的事件 ID 集合。

    不清空——同一 session 期间可能多次调用 _inject_events_to_running_session，
    每次都需要检查这批 ID。在下次 build_wake_prompt 时清空并重新记录。
    """
    global _presented_event_ids
    return _presented_event_ids.copy()


from domain.identity import wakeup_prompts  # noqa: E402

def _ctx_policy(reason: str) -> dict:
    """从事件注册表读取 context_policy，未注册事件返回空 dict（全部默认注入）。"""
    defn = _get_event_type(reason)
    return defn.context_policy if defn else {}


def _policy_flag(policy: dict, key: str, default: bool = True) -> bool:
    """读取 context_policy 中的布尔标记，默认 True（向后兼容未配置的事件类型）。"""
    return policy.get(key, default)


def _memory_health_snapshot() -> str:
    """记忆体检面板:扫各文件状态,生成简短摘要 + ⚠️ 异常标记。

    每次 wake 注入到 prompt 顶部,让模型自觉到记忆系统健康度。
    ~150 token 预算内。
    """
    try:
        from pathlib import Path as _P
        from domain.memory.memory.consciousness.runtime import _get_runtime_home
        mem_dir = _get_runtime_home() / "memories"
        if not mem_dir.exists():
            return ""

        import time as _time
        now = _time.time()
        warn_items = []
        scan_files = [
            ("意识流",   "CONSCIOUSNESS.md", 100),
            ("教训",     "LESSONS.md",       80),
            ("规则",     "RULES.md",         40),
            ("草稿",     "SCRATCHPAD.md",    2000),  # chars
            ("洞察",     "INSIGHTS.md",      30),
            ("上下文",   "CONTEXT.md",       2000),  # chars
        ]
        lines = ["## 记忆体检"]
        for label, fname, threshold in scan_files:
            p = mem_dir / fname
            if not p.exists():
                continue
            try:
                # 读基础指标
                text = p.read_text(encoding="utf-8")
                if fname.endswith("SCRATCHPAD.md") or fname.endswith("CONTEXT.md"):
                    # char-based 阈值
                    size = len(text)
                    if size <= threshold:
                        continue  # 不展示
                    extra = f"{size} 字"
                    # SCRATCHPAD 段数(## 标题数,> 2 算多)
                    if fname == "SCRATCHPAD.md":
                        seg_count = sum(1 for L in text.split("\n") if L.startswith("## "))
                        if seg_count > 2:
                            warn_items.append(f"{label} {seg_count} 个并行任务(建议 ≤2)")
                        extra += f" · {seg_count} 段"
                    lines.append(f"  · {label}  {extra}")
                else:
                    # 条目计算 --- count
                    if fname == "CONSCIOUSNESS.md":
                        # status tag 数(应清理)
                        status_count = text.count("[status]") + text.count("[trading_wait]") + \
                                       text.count("[system_wait]") + text.count("[final_status]")
                        entries = sum(1 for L in text.split("\n") if L.startswith("## "))
                        extra = f"{entries} 段"
                        if status_count >= 5:
                            warn_items.append(f"{label} {status_count} 个状态报告(more_hygiene 该清)")
                            extra += f" · {status_count} 状态报告"
                        lines.append(f"  · {label}  {extra}")
                    elif fname == "LESSONS.md":
                        sections = sum(1 for L in text.split("\n") if L.startswith("## "))
                        entries = text.count("---\n[")
                        warn = ""
                        if any(k in text for k in ["交易策略", "代码工程", "工具使用"]):
                            for sec in ["交易策略", "代码工程", "工具使用", "工作方式", "沟通规则", "其他"]:
                                if f"## {sec}" in text:
                                    # 数 section 内条数(粗略,--- + [ts])
                                    sec_text = text.split(f"## {sec}", 1)[1].split("## ", 1)[0] if f"## {sec}" in text else ""
                                    sec_count = sec_text.count("---\n[")
                                    if sec_count > threshold / 4:
                                        warn += f" {sec}={sec_count}"
                        if warn:
                            warn_items.append(f"{label}{warn}(其中某 section 超 {threshold // 4} 条)")
                        lines.append(f"  · {label}  {sections} 节 / {entries} 条{warn}")
                    elif fname == "RULES.md":
                        sections = sum(1 for L in text.split("\n") if L.startswith("## "))
                        if sections > threshold:
                            warn_items.append(f"规则 {sections} 节(建议 ≤ {threshold})")
                        lines.append(f"  · {label}  {sections} 节")
                    elif fname == "INSIGHTS.md":
                        # kind 分布
                        idea_cnt = text.count("[idea]")
                        warn_cnt = text.count("[warning]")
                        block_cnt = text.count("[block]")
                        total = idea_cnt + warn_cnt + block_cnt
                        if total > threshold:
                            warn_items.append(f"洞察 {total} 条(建议 ≤ {threshold}, 跑 memory_hygiene 清)")
                        lines.append(f"  · {label}  idea={idea_cnt}/ warning={warn_cnt}/ block={block_cnt}")

                # 文件超过 7 天没动 → 提示 dead
                try:
                    mtime = p.stat().st_mtime
                    age_days = int((now - mtime) / 86400)
                    if age_days >= 7:
                        warn_items.append(f"{label} {age_days} 天没动(可能 dead)")
                except Exception:
                    pass
            except Exception:
                continue

        # 时间戳末次整理
        try:
            consol_p = mem_dir / "CONSCIOUSNESS.md"
            if consol_p.exists():
                text = consol_p.read_text(encoding="utf-8")
                # 搜 [整理] YYYY-MM-DD HH:MM
                import re as _re
                m = _re.findall(r"\[整理\]\s*(\d{4}-\d{2}-\d{2})", text)
                if m:
                    last = m[0]
                    lines.append(f"  上次 memory_hygiene 跑过: {last}")
                else:
                    warn_items.append("还未跑过 memory_hygiene skill(evening_review)")
        except Exception:
            pass

        if warn_items:
            lines.append("  ⚠ 待清理: " + " · ".join(warn_items))
        else:
            lines.append("  ✓ 记忆状态健康")

        return "\n".join(lines) if len(lines) > 2 else ""
    except Exception:
        return ""


def _build_memory_context(reason: str = "", extra: str = "", events_text: str = "", policy: dict | None = None) -> str:
    """构建记忆上下文 — 记忆体检面板 + 近期教训 + 实体触发记忆。

    RULES / CONTEXT / SCRATCHPAD 不再注入，模型按需通过 sense 工具查询。
    CONSCIOUSNESS 由 scheduler._load_prev_session_summary() 以 conversation_history 承载。
    """
    parts = []
    policy = policy or {}

    # ── 记忆体检面板(每次 wake 都加,~150 token 上限) ──
    try:
        health = _memory_health_snapshot()
        if health:
            parts.append(health)
    except Exception:
        pass

    # LESSONS.md — 最近 3 条教训，始终注入
    try:
        from domain.memory.memory.consciousness.runtime import read_lessons
        lessons = read_lessons(n=3)
        if lessons and lessons.strip():
            parts.append(f"## 近期教训\n\n{lessons.strip()}")
    except Exception:
        pass

    # Entity-triggered recall: string-match known entities from wake context
    if _policy_flag(policy, "include_entity_recall"):
        context_for_entities = f"{reason} {extra} {events_text}".strip()
        if context_for_entities:
            try:
                from domain.memory.memory.consciousness.entity_index import (
                    extract_entities_from_context,
                    query_entities_ranked,
                )
                entities = extract_entities_from_context(context_for_entities)
                if entities:
                    memories = query_entities_ranked(entities, current_context=context_for_entities, limit=5)
                    if memories:
                        lines = ["## 实体触发记忆\n"]
                        for m in memories:
                            mtype = str(m.get("memory_type", "")).upper()
                            snippet = str(m.get("snippet", ""))[:150]
                            entity = str(m.get("_matched_entity", ""))
                            tag = f" [{entity}]" if entity else ""
                            if mtype == "PROFILE":
                                # profile 卡 = 对该实体的提炼理解,优先于碎片展示
                                lines.insert(1, f"- [{entity} · 概念] {snippet}")
                            else:
                                lines.append(f"- [{mtype}]{tag} {snippet}")
                        parts.append("\n".join(lines))
                        for m in memories:
                            mid = str(m.get("memory_id", ""))
                            if mid:
                                _presented_memory_ids.add(mid)
            except Exception:
                pass

    return "\n\n---\n\n".join(parts) if parts else ""
from domain.lifecycle.event_registry import get_event_type as _get_event_type  # noqa: E402

wakeup_prompts.configure_runtime_hooks(
    get_event_type=_get_event_type,
)


def _resolve_event_prompt(reason: str, pending_events: list | None = None) -> str:
    """从事件注册表解析 prompt 模板，兜底用通用唤醒 prompt。

    模板占位符（如 {prompt}, {action_label}, {energy_added}）从第一个匹配
    事件的 payload 中通过 str.format() 解析。

    缺失字段会用空字符串代替（不会 fallback 到原文带 {xxx} 的样子），
    保证 LLM 看到的 prompt 总是清理过的文本。
    """
    import string

    from .event_registry import get_event_type

    definition = get_event_type(reason)
    if definition and definition.prompt_template:
        template = definition.prompt_template
        if pending_events and "{" in template:
            for ev in pending_events:
                if ev.get("kind") == reason:
                    payload = ev.get("payload", {}) if isinstance(ev.get("payload"), dict) else {}
                    try:
                        # 用 SafeFormatter：缺失字段返回空字符串而不是抛 KeyError
                        # 特殊处理 _merged_texts_block:从 accumulate 合并的 batch
                        # 渲染成" sender:text "可读块,让模型在一次 batched wake 里
                        # 看到这 30s 窗口内的多条消息
                        payload = dict(payload)  # copy 避免污染原 payload
                        mc = payload.get("_merged_count", 1)
                        mt = payload.get("_merged_texts", [])
                        if isinstance(mc, int) and mc > 1 and isinstance(mt, list) and mt:
                            lines = [
                                f"  之前合并的 {mc-1} 条消息（30 秒窗口内累积到达）:"
                            ]
                            for item in mt:
                                if isinstance(item, dict):
                                    lines.append(f"    {item.get('sender','?')}：{item.get('text','')[:200]}")
                                elif isinstance(item, str):
                                    lines.append(f"    {item[:200]}")
                            payload["_merged_texts_block"] = "\n".join(lines)
                        # 多模态附件：attachments list 转成"附件清单"段提示模型用 sense_image
                        #    形如 [图片 feishu:img_v3_xxx (image/png)] / [文件 feishu:file_v3_yyy]
                        atts = payload.get("attachments") or []
                        if isinstance(atts, list) and atts:
                            kind_emoji = {"image": "🖼️", "file": "📄", "audio": "🎙️", "video": "🎬"}
                            att_lines = [
                                "  附件（调 sense_image(attachment_id) 或按需处理）:"
                            ]
                            for a in atts:
                                if not isinstance(a, dict):
                                    continue
                                aid = a.get("attachment_id", "?")
                                kind = a.get("kind", "file")
                                mime = a.get("mime", "")
                                emoji = kind_emoji.get(kind, "📎")
                                att_lines.append(f"    {emoji} [{kind} {aid} ({mime})]")
                            payload["attachments_block"] = "\n".join(att_lines)
                        else:
                            payload["attachments_block"] = ""
                        # 用 SafeFormatter：缺失字段返回空字符串而不是抛 KeyError
                        fmt = string.Formatter()
                        out_parts = []
                        for literal_text, field_name, format_spec, conversion in fmt.parse(template):
                            out_parts.append(literal_text or "")
                            if field_name is None:
                                continue
                            if not field_name:
                                continue
                            # 取值，缺失返回 ""
                            val = payload.get(field_name, "")
                            if val is None:
                                val = ""
                            if isinstance(val, bool):
                                val = "是" if val else "否"
                            out_parts.append(str(val))
                        return "".join(out_parts)
                    except (KeyError, ValueError, IndexError):
                        pass
        # 无 payload 或异常：仍需清理掉未替换的 {xxx}，避免 LLM 看到裸模板
        import re
        return re.sub(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}", "", template)

    # Fallback: use birth event prompt as generic awake
    birth_def = get_event_type("birth")
    if birth_def and birth_def.prompt_template:
        return birth_def.prompt_template

    return "你醒了。先 sense_time 看时间，sense_vitals 看精力，然后继续推进。"


_MULTI_EVENT_HINT = (
    "\n\n> `sense_event_detail(event_id)` 可查看其他未自动展开的事件详情，"
    "查看后该事件标记已消费。可自行决定优先级与是否处理。"
)


def _event_priority(ev: Dict[str, Any]) -> int:
    """从注册表获取事件的优先级，默认 5。"""
    kind = ev.get("kind", "")
    try:
        type_def = _get_event_type(kind)
        return type_def.priority if type_def else 5
    except Exception:
        return 5


def _build_multi_event_base(
    pending_events: List[Dict[str, Any]],
) -> tuple[str, List[Dict[str, Any]]]:
    """返回 (清单文本, 剩余事件) — 多事件清单只列标题，不内联详情。

    产品语义(与用户对齐过):
    - 清单 = 仅"通知模型这一醒有哪些事在排队",**不展示任何详情、mental_context、prompt_template**
    - 清单单条格式: `[eid] [类型名] - [标题]`(标题来自 name/reason/text 一句话)
    - 清单本身的展示**不消费**任何事件 → covered_event_ids = []
    - 模型按优先级调 sense_event_detail(event_id) → 这一刻才消费 + 才看到完整 payload
      (routine 的 1273 字 prompt_template、timer 的 reason+mental_context、message
       的 sender+text 等)
    - 一个一个做:"取一件 → 看详情 → 处理 → 下一件"的节奏,不要批量拉详情再处理
    - 如果中途 rest 了(忘了处理),未消费的事件保持 due,下次醒来自然进入新一轮清单

    历史 BUG: 旧版清单会把每条事件的 summary 截到 120 字内联,prompt 长度被噪音占满
    之外,还把"清单已展示"误等价于"事件已处理",触发 batch auto-consume——
    结果 routine 的完整流程指引(prompt_template)被吞了,模型既看不到也调不出详情。
    """
    sorted_events = sorted(pending_events, key=_event_priority, reverse=True)
    if not sorted_events:
        # ⚠ BUG D 修复：0 事件被误当多事件处理时，绝不能输出"多个事件同时触发了"
        # —— 那是对模型的纯误导（#1220 整条链根因之一）。空队列意味着这轮 wake
        # 没有任何事要做，给模型诚实信号："没事，可以 rest / 或感知下自己状态"。
        # 注意：理论上不该走到这里——wake_digital_life 在 BUG B 修复后已经
        # event_id<=0 时不再启动 wake。这里只是兜底防御性输出。
        return (
            "事件队列是空的，本轮没有需要你主动处理的事件。"
            "如果刚发完消息发现什么都没发生，那是 wake 误触——你不需要处理任何事。"
            "可以 `sense_vitals`/`sense_time` 看看状态，或直接 `rest`。",
            [],
        )

    lines: list[str] = ["有多个事件同时触发了，按优先级排列（仅标题，查看详情用 sense_event_detail）："]
    for ev in sorted_events:
        eid = ev.get("event_id", "?")
        kind = ev.get("kind", "")
        type_def = _get_event_type(kind)
        display = type_def.display_name if type_def else kind
        priority = type_def.priority if type_def else 5
        title = _event_title(ev)
        line = f"- [#{eid}] **{display}** (priority={priority}) — {title}" if title \
               else f"- [#{eid}] **{display}** (priority={priority})"
        lines.append(line)

    lines.append(
        "\n> **一个一个做**：按优先级顺序调 `sense_event_detail(event_id)`——调用会消费该事件并展示完整 payload"
        "（含作息类的 skill 引导文本、闹钟的 mental_context、消息的发送人/正文等）。"
        "**处理完一件接着调下一件，不要批量拉取详情**。精力够就处理完清单，不需要每件结束 rest。"
        "如果中途因 rest 中断，未消费的事件会自动在下次醒来时重新进入清单。"
    )

    return "\n".join(lines), sorted_events


def _event_title(ev: Dict[str, Any]) -> str:
    """提炼一句话标题(≤40 字)——只用于清单一行展示，不是完整 payload。

    不同 kind 的关键字段不同：routine→name, timer→reason/mental/manage_daily 文案,
    message→sender_name, task_todo_due→todo_id, task_reminder→content, initiative→urgency。
    """
    payload = ev.get("payload") or {}
    if not isinstance(payload, dict):
        return ""
    kind = ev.get("kind", "")

    if kind == "routine":
        return str(payload.get("name") or payload.get("description") or "").strip()[:40]
    if kind in ("timer", "awaiting_reply"):
        # timer 含三种来源(rest / UNTIL / manage_daily 计划项),都用 reason 作为标题
        return str(payload.get("reason") or payload.get("mental_context") or "").strip()[:40]
    if kind in ("group_message", "message"):
        sender = str(payload.get("sender_name") or "").strip()
        text_head = str(payload.get("text") or "").strip()[:30]
        return f"from {sender}: {text_head}" if sender else text_head
    if kind == "task_todo_due":
        return f"todo #{payload.get('todo_id', '?')}"
    if kind == "task_reminder":
        return str(payload.get("content") or "").strip()[:40]
    if kind == "initiative":
        eh = payload.get("elapsed_hours", 0)
        en = payload.get("energy", "?")
        ur = payload.get("urgency", "?")
        return f"空闲 {eh:.1f}h | energy {en} | urgency {ur}"
    return str(payload.get("reason") or payload.get("text") or payload.get("name") or "").strip()[:40]


def build_wake_prompt(
    reason: str,
    extra: str = "",
    pending_events: Optional[List[Dict[str, Any]]] = None,
    sleep_minutes: float = 0,
    status: str = "BLOCKED",
) -> tuple[str, str, list, str]:
    """构建唤醒 prompt，返回 (action_prompt, ref_context, covered_event_ids, task_prompt) 四元组。

    action_prompt — 要立刻处理的事：
      唤醒原因（从 event registry 模板解析）

    ref_context — 参考材料：
      今日目标 → 近期教训 → 实体触发记忆 → 活跃任务
      其余上下文（规则/交接/草稿本/发送记录）模型按需通过 sense 工具查询。

    task_prompt — 活跃任务上下文，作为独立 user message 注入（不走 _convert_user_to_tool）。

    covered_event_ids — prompt 内联了完整内容的事件 ID 列表。
      调度层对此列表做 auto-consume，LLM 失败时回退。
      单事件/短休息 → 事件 ID；多事件（仅清单展示）→ 空列表。
    """
    pending_events = pending_events or []

    # Re-query the DB only when no explicit events were passed (cron path).
    # When events are explicitly passed (message handler path), trust them —
    # re-querying would catch stale unconsumed events from past sessions.
    if not pending_events:
        try:
            from domain.lifecycle.events import pop_due_events
            pending_events = pop_due_events(limit=20)
        except Exception:
            pending_events = []

    is_single_event = len(pending_events) == 1
    _multi_remaining: List[Dict[str, Any]] = []  # events not auto-consumed in multi-event mode

    # 记录展示的事件 ID，防止 mid-session 重复注入
    global _presented_event_ids
    _presented_event_ids.clear()
    for ev in pending_events:
        eid = ev.get("event_id")
        if eid is not None:
            _presented_event_ids.add(eid)

    if status == "BLOCKED" and not is_single_event:
        base, _multi_remaining = _build_multi_event_base(pending_events)
    else:
        base = _resolve_event_prompt(reason, pending_events)

    action_parts: list[str] = []
    ref_parts: list[str] = []

    # 从事件注册表读取 context_policy，控制哪些上下文段落被注入
    policy = _ctx_policy(reason)

    # ── Build action_parts: what to do NOW ──────────────────────

    action_parts.append("## \u2500\u2500 \u2193 \u5f53\u4e0b\u4e8b\u4ef6 \u2193 \u2500\u2500")

    action_parts.append(f"\n### \u5524\u9192\u539f\u56e0\n\n{base}")

    # ── Build ref_parts: reference materials ──────────────────

    def _ref(title: str, body: str) -> str:
        return f"## {title}\n\n{body}"

    # 今日目标
    if _policy_flag(policy, "include_daily"):
        try:
            from domain.memory.memory.consciousness.runtime import read_daily
            daily_text = read_daily()
            if daily_text and "还没有" not in daily_text:
                ref_parts.append(_ref("\u4eca\u65e5\u76ee\u6807", daily_text))
        except Exception:
            pass

    # 自我迭代档案
    if _policy_flag(policy, "include_self_review") and pending_events:
        should_inject = reason == "initiative" or any(
            (ev.get("payload") or {}).get("task_id", "") == "self_iteration"
            for ev in pending_events
        )
        if should_inject:
            try:
                from domain.memory.memory.consciousness.runtime import read_self_knowledge
                sk = read_self_knowledge()
                if sk and sk.strip():
                    ref_parts.append(_ref("\u4f60\u7684\u7cfb\u7edf\u8ba4\u77e5\u6863\u6848", sk))
            except Exception:
                pass

    # 记忆上下文（近期教训始终注入，实体触发记忆按 policy 控制）
    try:
        events_text_parts = []
        for ev in (pending_events or [])[:5]:
            p = ev.get("payload", {}) if isinstance(ev.get("payload"), dict) else {}
            t = (p.get("text") or p.get("description") or "").strip()
            if t:
                events_text_parts.append(t[:200])
        events_text = " ".join(events_text_parts)
        ctx = _build_memory_context(reason=reason, extra=extra, events_text=events_text, policy=policy)
        if ctx:
            ref_parts.append(ctx)
    except Exception:
        pass

    if extra:
        ref_parts.append(f"\n\n{extra}")

    # 任务面板注入：历史上这里调 get_wake_context() 注入一份 "task_board"，
    # scheduler.py 的 my_context 注入点也会调 render_my_board()——同一个内容
    # 重复注入 2 次（wake 853 实测 my_context 675 字 + task_board 675 字完全一致）。
    # 现在删掉这条注入，让 scheduler 的 my_context 是唯一来源。
    task_prompt = ""

    action_parts.append("\n\u2500\u2500 /\u5f53\u4e0b\u4e8b\u4ef6 \u2500\u2500")

    # 计算 prompt 内联覆盖了哪些事件：
    # - 真正的单事件 wake → 完整内容已 inline,直接消费
    # - 短休息承接(<15min)的 timer,且只有这一件(#1367 等) → 同上
    # - 多事件 wake → **清单只列标题**,事件保持待消费,模型按优先级自己调
    #   sense_event_detail 去消费,查看一个处理一个
    #
    # 历史 BUG: 短休息 + timer 条件与多事件叠加,导致"短 timer + 多事件同时到"也
    # 全量 batch consume。06-12 wake 251 就是这样:routine 完整 prompt_template
    # 还没被模型调 sense_event_detail 看过就已经被消费,然后下一轮再也拉不到。
    if is_single_event or (0 < sleep_minutes < 15 and reason == "timer" and len(pending_events) == 1):
        covered_event_ids = [ev.get("event_id") for ev in pending_events if ev.get("event_id")]
    else:
        covered_event_ids = []

    return "\n\n".join(action_parts), "\n\n".join(ref_parts), covered_event_ids, task_prompt


__all__ = ["build_wake_prompt", "get_presented_memory_ids", "get_presented_event_ids"]
