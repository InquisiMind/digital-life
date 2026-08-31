"""分层记忆巩固 — 自动从 session 中提取摘要，分层压缩，建立联想链接。

记忆分层：
  Layer 0  当前session  → _load_prev_session_summary（已有，scheduler.py）
  Layer 1  session digest → 每个 L4 wake transaction 结束自动生成
  Layer 2  日摘要       → 当天所有 session digest 合并
  Layer 3  周摘要       → 7天日摘要压缩

所有层存 SQLite (memory_layers 表)，向量索引存 memory_vectors.db，
联想链接存 associations 表。支持 recall_memory 工具按需检索。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from domain.lifecycle.clock import now_dt as _default_now_dt
from infrastructure.config import get_runtime_memories_dir, get_runtime_state_db_path
from infrastructure.persistence import sqlite
from domain.memory.memory.summaries import (  # noqa: E402
    IGNORE_TOOLS,
    dedup_tool_summaries,
    extract_energy,
    extract_topics,
    format_session_digest,
    summarize_tool_call,
)

logger = logging.getLogger("domain.memory.summaries.consolidation")


# B 治源 (用户 2026-07-17 指引): 前置过滤 conversation 索引阶段的垃圾内容。
# 与 scripts/memory_audit.py 的规则同源, 在源头拦截, 比事后清效率高得多。
# 命中 → return True → 调用者跳过该条消息, 不进 chunks 表。
_WAKE_TEMPLATE_FEATURES = [
    re.compile(p) for p in [
        r"── ↓ 当下事件 ↓ ──",
        r"### 唤醒原因",
        r"## 记忆体检",
        r"⏰ 当前时间：\d",
        r"按优先级排列（仅标题",
        r"⏰ 你设的闹钟响了",
        r"上一轮留下的备注：",
        r"^\[user\] ## ── ↓ 当下事件 ↓ ──",
    ]
]


def _is_noise_content(text: str) -> bool:
    """检 conversation content 是否是已知垃圾模式。
    返回 True 表示该条不应进检索池。
   保守起见: 只过滤有把握的 wake_template (≥2 特征同时命中) 和 tool_result。
    过渡句等 client-side 已经够用, 不在这里跳过(避免误伤真实对话)。
    """
    if not text:
        return False
    # wake_prompt 系统注入: ≥2 个特征同命中才算
    if sum(1 for p in _WAKE_TEMPLATE_FEATURES if p.search(text)) >= 2:
        return True
    # 纯 JSON / 工具返回值 (开头是 { 或 [)
    s = text.strip()
    if s.startswith("{") or s.startswith("["):
        try:
            json.loads(s[:200])
            return True
        except Exception:
            pass
    return False

_mem_dir_cache: Path | None = None


def _get_mem_dir() -> Path:
    global _mem_dir_cache
    if _mem_dir_cache is None:
        _mem_dir_cache = get_runtime_memories_dir()
    return _mem_dir_cache


def _get_db_path() -> Path:
    return _get_mem_dir() / "memory_layers.db"


# Backward-compatible: use _get_mem_dir() / _get_db_path() internally.
_MEM_DIR: Path = property(lambda: _get_mem_dir())  # type: ignore[assignment, has-type]
_DB_PATH: Path = property(lambda: _get_db_path())  # type: ignore[assignment, has-type]
_now_dt_hook: Callable[[], datetime] = _default_now_dt
_on_session_end_hook: Callable[[str, str], Any] = lambda session_id, digest: None
_session_db_factory_hook: Callable[[], Any] | None = None
_llm_call_hook: Callable[..., Any] | None = None


def _resolve_llm_call():
    global _llm_call_hook
    if _llm_call_hook is None:
        try:
            from infrastructure.ai.llm import call_llm
            _llm_call_hook = call_llm
        except Exception:
            pass
    return _llm_call_hook


def _resolve_session_db_factory():
    global _session_db_factory_hook
    if _session_db_factory_hook is None:
        try:
            from infrastructure.ai import SessionDB
            _session_db_factory_hook = SessionDB
        except Exception:
            pass
    return _session_db_factory_hook


def _resolve_on_session_end():
    global _on_session_end_hook
    if _on_session_end_hook.__name__ == "<lambda>":
        try:
            from domain.todos import on_session_end
            _on_session_end_hook = on_session_end
        except Exception:
            pass
    return _on_session_end_hook


def configure_runtime_hooks(
    *,
    now_dt: Callable[[], datetime] | None = None,
    on_session_end: Callable[[str, str], Any] | None = None,
    session_db_factory: Callable[[], Any] | None = None,
    llm_call: Callable[..., Any] | None = None,
) -> None:
    """Inject runtime services supplied by adapters/Hermes."""
    global _now_dt_hook, _on_session_end_hook, _session_db_factory_hook, _llm_call_hook
    if now_dt is not None:
        _now_dt_hook = now_dt
    if on_session_end is not None:
        _on_session_end_hook = on_session_end
    if session_db_factory is not None:
        _session_db_factory_hook = session_db_factory
    if llm_call is not None:
        _llm_call_hook = llm_call

# 不记录的工具——sense 类只是读取状态，不代表"做了什么"
_IGNORE_TOOLS = IGNORE_TOOLS

# 工具摘要映射
_TOOL_SUMMARIZERS = {}


def _summarize_tool_call(name: str, args: Dict[str, Any]) -> str:
    """将 tool call 压缩为一行摘要。"""
    return summarize_tool_call(name, args)


def _extract_energy(text: str) -> Optional[float]:
    """从 assistant 消息中提取精力数值。"""
    return extract_energy(text)


# ──────────────────── SQLite ────────────────────

def _get_db() -> sqlite.Connection:
    db = sqlite.connect(str(_get_db_path()))
    db.row_factory = sqlite.Row
    # durability: WAL + FULL synchronous 防 WAL 半写损坏。
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("PRAGMA busy_timeout=5000")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS memory_layers (
            id INTEGER PRIMARY KEY,
            layer TEXT NOT NULL,
            period TEXT NOT NULL,
            digest TEXT NOT NULL,
            llm_summary TEXT,
            tool_summary TEXT,
            start_time REAL,
            end_time REAL,
            parent_ids TEXT,
            created_at REAL NOT NULL,
            fallback INTEGER DEFAULT 0,
            UNIQUE(layer, period)
        );
        CREATE INDEX IF NOT EXISTS idx_ml_layer ON memory_layers(layer);
        CREATE INDEX IF NOT EXISTS idx_ml_period ON memory_layers(period);
        CREATE INDEX IF NOT EXISTS idx_ml_start ON memory_layers(start_time);
    """)
    # 兼容旧库：llm_summary 列可能不存在
    try:
        db.execute("ALTER TABLE memory_layers ADD COLUMN llm_summary TEXT")
    except Exception:
        pass
    # 兼容旧库：fallback 列可能不存在
    try:
        db.execute("ALTER TABLE memory_layers ADD COLUMN fallback INTEGER DEFAULT 0")
    except Exception:
        pass
    return db


# ──────────────────── Session Digest ────────────────────

def _generate_session_digest(
    session_db: Any,
    session_id: str,
) -> Optional[Dict[str, Any]]:
    """从 messages 表提取 session digest。纯文本分析，不调 LLM。"""
    try:
        rows = session_db._conn.execute(
            "SELECT role, content, tool_calls, timestamp "
            "FROM messages WHERE session_id=? ORDER BY timestamp",
            (session_id,),
        ).fetchall()
    except Exception as e:
        logger.warning("Failed to query messages for %s: %s", session_id, e)
        return None

    if not rows:
        return None

    # 提取唤醒原因（从 session_id 中提取，更可靠）
    wake_reason = "unknown"
    tx_match = re.search(r"^tx_([a-z0-9_]+)_\d{4}_\d{4}_[0-9a-f]{6}$", session_id)
    legacy_match = re.search(r"^l4_wake_(\w+)", session_id)
    if tx_match:
        wake_reason = tx_match.group(1)
    elif legacy_match:
        wake_reason = legacy_match.group(1)

    # 提取 tool calls
    tool_summaries: List[str] = []
    tool_counts: Dict[str, int] = {}
    for m in rows:
        if m["tool_calls"]:
            try:
                calls = json.loads(m["tool_calls"])
                for call in calls:
                    func = call.get("function", {})
                    name = func.get("name", "")
                    args_str = func.get("arguments", "{}")
                    try:
                        args = json.loads(args_str) if isinstance(args_str, str) else args_str
                    except Exception:
                        args = {}
                    summary = _summarize_tool_call(name, args)
                    if summary:
                        tool_summaries.append(summary)
                        tool_counts[name] = tool_counts.get(name, 0) + 1
            except Exception:
                pass

    # 提取精力轨迹
    energies: List[float] = []
    for m in rows:
        if m["role"] == "assistant" and m["content"]:
            e = _extract_energy(m["content"])
            if e is not None:
                energies.append(e)

    # 提取 assistant 关键文本（精力描述、决策性语句）
    assistant_texts: List[str] = []
    for m in rows:
        if m["role"] == "assistant" and m["content"] and m["content"].strip():
            assistant_texts.append(m["content"].strip()[:100])

    # 时间范围
    start_time = rows[0]["timestamp"] if rows else 0
    end_time = rows[-1]["timestamp"] if rows else 0
    duration = end_time - start_time if start_time and end_time else 0

    # 结束方式
    end_reason = "unknown"
    last_tools = []
    for m in rows:
        if m["tool_calls"]:
            try:
                calls = json.loads(m["tool_calls"])
                for call in calls:
                    fname = call.get("function", {}).get("name", "")
                    last_tools.append(fname)
            except Exception:
                pass
    if "rest" in last_tools:
        end_reason = "主动休息"
    else:
        end_reason = "session结束"

    # 精力范围
    energy_str = ""
    if energies:
        e_start = energies[0]
        e_end = energies[-1]
        energy_str = f"精力{e_start:.0f}→{e_end:.0f}"

    # 去重 tool summaries（连续相同的合并计数）
    deduped = _dedup_tool_summaries(tool_summaries)

    # 构建叙事摘要
    tool_counts_str = ", ".join(
        f"{k}×{v}" for k, v in sorted(tool_counts.items(), key=lambda x: -x[1])
        if k not in _IGNORE_TOOLS
    )
    narrative_parts = []
    if wake_reason != "unknown":
        narrative_parts.append(f"唤醒:{wake_reason}")
    if tool_counts_str:
        narrative_parts.append(f"动作:{tool_counts_str}")
    if energy_str:
        narrative_parts.append(energy_str)
    narrative_parts.append(f"结束:{end_reason}")

    # 时间戳格式化
    start_dt = datetime.fromtimestamp(start_time, tz=timezone.utc).astimezone()
    time_str = start_dt.strftime("%m/%d %H:%M")

    digest = {
        "session_id": session_id,
        "time": time_str,
        "wake_reason": wake_reason,
        "duration_sec": round(duration),
        "tool_summary": deduped,
        "tool_counts": tool_counts,
        "energy_range": (energies[0], energies[-1]) if energies else None,
        "end_reason": end_reason,
        "narrative": " | ".join(narrative_parts),
        "message_count": len(rows),
    }

    # Meta-review: 评估本轮被联想命中的认知是否起到了价值
    # 检查 add_cognition 工具调用 → 新写入的认知 计入"沉淀"
    # 检查 supersede_memory → 认知被覆盖 计入"更新"
    # 检查 end_reason 含 error → 可能认知有问题 计入"质疑"
    cognition_assessment = None
    try:
        cognition_assessment = _assess_session_cognition(rows, session_id)
    except Exception:
        pass
    if cognition_assessment:
        digest["cognition_assessment"] = cognition_assessment

    return digest


def _dedup_tool_summaries(summaries: List[str]) -> List[str]:
    """去重连续相同的摘要，合并计数。"""
    return dedup_tool_summaries(summaries)


_COGNITION_ASSESSMENT_PROMPT = """你刚刚结束了一段工作。现在请审视本轮被注入到你思维中的联想认知，判断它们是否起到了作用。

## 本轮对话摘要

{conversation}

## 本轮被注入的联想认知

{recalled_cognitions}

## 评估要求

对每条认知，判断本轮中它的实际作用：
- **positive**: 你据此认知做了决策/行动，且结果正确（没有被打脸、没有被纠正）
- **negative**: 你据此认知行动了，但结果错误/被用户纠正/导致了问题
- **neutral**: 只是看到了，没有据此行动；或无法判断

严格判断 — 只标真正起到作用的。拿不准的标 neutral。

## 输出格式 (JSON)

```json
[
  {{"id": 12345, "verdict": "positive", "reason": "据此判断了SL阈值"}},
  {{"id": 12346, "verdict": "negative", "reason": "用了但被Alpha指出数据过期"}},
  {{"id": 12347, "verdict": "neutral", "reason": "只是看到了，没据此行动"}}
]
```

只输出 JSON 数组，不要其它文字。"""


def _assess_session_cognition(rows: Any, session_id: str) -> Optional[Dict[str, Any]]:
    """Meta-review: wake 结束自动跑, 用 LLM 判断本轮召回的认知是否起到了价值。

    设计原则:
      - 输入: 本轮对话 + 被注入的联想认知 (面包屑里的 #id + text)
      - LLM 判断: 每条认知 → positive/negative/neutral
      - 输出: positive → apply_signal verified, negative → apply_signal falsified
      - neutral → 不动

    纯规则的部分只保留 supersede 的自动质疑 (这个不需要 LLM 判断):
      - supersede_memory 老认知 → 自动 falsified (被覆盖 = 被质疑)
    """
    # === 1. 纯规则: supersede → 老认知自动 falsified ===
    try:
        from domain.memory.memory.recall.unified.cognition_store import apply_signal
    except Exception as e:
        logger.warning("_assess_session_cognition: cannot import apply_signal: %s", e)
        return None

    positive_signals: list[int] = []
    negative_signals: list[int] = []

    for m in rows:
        if not m["tool_calls"]:
            continue
        try:
            calls = json.loads(m["tool_calls"])
        except Exception:
            continue
        for call in calls:
            fname = call.get("function", {}).get("name", "")
            if fname in ("update_cognition", "supersede_memory"):
                args_str = call.get("function", {}).get("arguments", "{}")
                try:
                    args = json.loads(args_str) if isinstance(args_str, str) else args_str
                except Exception:
                    args = {}
                action = args.get("action", "")
                old_id = args.get("old_chunk_id") or args.get("chunk_id")
                if action == "supersede" and isinstance(old_id, int):
                    try:
                        r = apply_signal(old_id, "falsified", reason=f"session {session_id[:30]}: 被新认知覆盖")
                        if r.get("ok"):
                            negative_signals.append(old_id)
                    except Exception:
                        pass

    # === 2. 收集本轮被注入的联想认知 ===
    recalled = []
    for m in rows:
        content = m.get("content") or ""
        if m.get("role") == "tool" and "联想命中" in content:
            # 从面包屑里提取 #id 和 text
            import re
            for match in re.finditer(r'#(\d+)\s.*?[\]】]\s*(.*?)(?:\n|$)', content):
                cid = int(match.group(1))
                text = match.group(2).strip()[:100]
                if cid > 0 and text:
                    recalled.append({"id": cid, "text": text})
    # 去重
    seen_ids = set()
    recalled = [r for r in recalled if r["id"] not in seen_ids and not seen_ids.add(r["id"])]

    if not recalled:
        # 没有召回认知 → 只返回 supersede 质疑结果
        if positive_signals or negative_signals:
            return {
                "auto_verified": 0,
                "auto_falsified": len(negative_signals),
                "llm_assessed": 0,
                "recalled_count": 0,
                "summary": f"LLM评估0条(无召回), 自动质疑{len(negative_signals)}条",
            }
        return None

    # === 3. LLM 判断 ===
    llm_call = _resolve_llm_call()
    if llm_call is None:
        logger.warning("_assess_session_cognition: LLM 不可用, 跳过认知评估")
        return None

    # 构建对话摘要 (压缩, 避免太长)
    conversation_parts = []
    for m in rows:
        role = m.get("role", "")
        content = (m.get("content") or "").strip()
        if role in ("user", "assistant") and content and len(content) > 5:
            conversation_parts.append(f"[{role}] {content[:300]}")
    conversation = "\n".join(conversation_parts[-20:])[:4000]  # 最近 20 条, 最多 4000 字符

    recalled_text = "\n".join(
        f"- #{r['id']}: {r['text']}" for r in recalled[:15]  # 最多 15 条
    )

    prompt = _COGNITION_ASSESSMENT_PROMPT.format(
        conversation=conversation,
        recalled_cognitions=recalled_text,
    )

    try:
        resp = llm_call(
            messages=[{"role": "user", "content": prompt}],
            model=None,  # 用默认模型
            temperature=0.1,
            max_tokens=1000,
        )
        # 解析 LLM 返回的 JSON
        import re as _re
        json_match = _re.search(r'\[.*\]', resp, _re.DOTALL)
        if not json_match:
            logger.debug("_assess_session_cognition: LLM 没返回 JSON, resp=%r", resp[:200])
            return None

        assessments = json.loads(json_match.group(0))
        assessed_count = 0

        for a in assessments:
            cid = a.get("id")
            verdict = str(a.get("verdict", "")).lower()
            reason = a.get("reason", "")
            if not isinstance(cid, int) or cid <= 0:
                continue

            if verdict == "positive":
                try:
                    r = apply_signal(cid, "verified", reason=f"LLM评估: {reason[:50]}")
                    if r.get("ok"):
                        positive_signals.append(cid)
                        assessed_count += 1
                except Exception:
                    pass
            elif verdict == "negative":
                try:
                    r = apply_signal(cid, "falsified", reason=f"LLM评估: {reason[:50]}")
                    if r.get("ok"):
                        negative_signals.append(cid)
                        assessed_count += 1
                except Exception:
                    pass

        logger.info("[cognition_audit] LLM assessed %d cognitions: %d positive, %d negative, %d neutral",
                    len(assessments), len(positive_signals), len(negative_signals),
                    len(assessments) - assessed_count)

        return {
            "auto_verified": len(positive_signals),
            "auto_falsified": len(negative_signals),
            "llm_assessed": assessed_count,
            "recalled_count": len(recalled),
            "summary": f"LLM评估{len(recalled)}条召回: 强化{len(positive_signals)} 质疑{len(negative_signals)} 中性{len(assessments) - assessed_count}",
        }

    except Exception as e:
        logger.warning("_assess_session_cognition: LLM 调用失败: %s", e)
        return None

    # 0 信号 → return None (不影响 digest, 只是不增加字段)
    if not positive_signals and not negative_signals and not added_chunk_ids and not superseded_old_ids and not deleted_ids and not has_error:
        return None

    return {
        "added": len(added_chunk_ids),
        "superseded": len(superseded_old_ids),
        "deleted": len(deleted_ids),
        "auto_verified": len(positive_signals),
        "auto_falsified": len(negative_signals),
        "has_error": has_error,
        "summary": (
            f"沉淀{len(added_chunk_ids)} 覆盖{len(superseded_old_ids)} 删除{len(deleted_ids)} "
            f"自动强化{len(positive_signals)} 自动质疑{len(negative_signals)} "
            f"{'error信号' if has_error else 'ok'}"
        ).strip(),
    }


def _format_session_digest(d: Dict[str, Any]) -> str:
    """格式化 digest 为可读文本。"""
    return format_session_digest(d)


# ──────────────────── Segment Narrative ────────────────────

# 极短 session 跳过阈值
_SHORT_SESSION_DURATION_S = 30
_SHORT_SESSION_TOOL_CALLS = 3


def _should_generate_segment(session_db: Any, session_id: str) -> bool:
    """判断是否应该生成段叙事（避免极短 session）。"""
    try:
        rows = session_db._conn.execute(
            "SELECT MIN(timestamp) as start, MAX(timestamp) as end FROM messages WHERE session_id=?",
            (session_id,),
        ).fetchone()
        if not rows or not rows["start"] or not rows["end"]:
            return False

        duration = rows["end"] - rows["start"]
        if duration < _SHORT_SESSION_DURATION_S:
            logger.debug("Skipping segment narrative: session %s too short (%ds)", session_id[:20], duration)
            return False

        tool_call_count = session_db.get_tool_call_count(session_id)
        if tool_call_count < _SHORT_SESSION_TOOL_CALLS:
            logger.debug("Skipping segment narrative: session %s too few tool calls (%d)", session_id[:20], tool_call_count)
            return False
    except Exception as e:
        logger.debug("Failed to check session length for %s: %s", session_id[:20], e)
        return False
    return True


_SEGMENT_NARRATIVE_PROMPT = """回顾你这一段经历，写下结构化记忆。

## 要求
- 第一人称"我"
- 抓主干：使命、决策、转折、产出、线头
- 不写流水账，要写意图与结果
- 决策写"为什么这么选"和"放弃了什么"
- 提到具体工具调用、文件时带 ID 引用
- 用 YAML 输出（schema 见下）

## 输出 schema
session_id: {session_id}
segment_index: {segment_index}
mission: | 这一段的使命是什么（不是"做了什么"，是"为什么"）
decisions:
  - choice: "做了什么选择"
    reason: "为什么这么选"
    rejected: ["放弃的替代方案"]
trajectory:
  - 阶段描述: 做了什么
outputs:
  files:
    - path: "文件路径"
      tool_call_id: 调用ID
  memory_writes:
    - target: "写入目标（LESSONS.md等）"
      tool_call_id: 调用ID
surprises:
  - "意外情况及应对"
open_threads:
  - "留给下段的线头"
entities:
  - name: "实体名"
    type: "实体类型"

## 核心提问
- 这一段的使命是什么（不是"做了什么"，是"为什么"）
- 关键抉择在哪？为什么这么选？拒绝了什么？
- 有什么意外？怎么应对？
- 沉淀了什么到永久记忆？
- 留给下一段的线头是什么？

对话记录：
{conversation}
""".strip()


def _build_segment_conversation_text(messages: list) -> str:
    """把段 messages 构建成 LLM 可读的对话文本。"""
    parts = []
    for m in messages:
        role = "用户" if m["role"] == "user" else "我"
        content = (m["content"] or "").strip()
        if not content and m.get("tool_calls"):
            try:
                calls = m["tool_calls"] if isinstance(m["tool_calls"], list) else json.loads(m["tool_calls"] or "[]")
                lines = []
                for call in calls:
                    fn = call.get("function", {})
                    name = fn.get("name", "")
                    args_str = fn.get("arguments", "{}")
                    try:
                        args = json.loads(args_str) if isinstance(args_str, str) else args_str
                    except Exception:
                        args = {}
                    if name == "express_to_human":
                        lines.append(f"[发消息]: {args.get('text', '')[:80]}")
                    elif name == "write_diary":
                        lines.append(f"[写日记]: {args.get('text', '')[:80]}")
                    elif name == "record_thought":
                        lines.append(f"[思绪]: {args.get('text', '')[:80]}")
                    elif name == "update_scratchpad":
                        lines.append(f"[更新笔记]")
                    elif name == "terminal":
                        lines.append(f"[命令]: {args.get('command', '')[:80]}")
                    elif name == "execute_code":
                        code = args.get('code', '')[:80]
                        lines.append(f"[执行代码]: {code}")
                    else:
                        lines.append(f"[{name}]")
                if lines:
                    content = " | ".join(lines)
            except Exception:
                pass
        if content:
            ts = datetime.fromtimestamp(m["timestamp"], tz=timezone.utc).astimezone().strftime("%H:%M")
            if m["role"] == "user":
                content = content[:120]
            parts.append(f"[{ts}] {role}: {content}")
    return "\n".join(parts)


def _generate_segment_digest(session_db: Any, session_id: str, segment_index: int) -> Optional[Dict[str, Any]]:
    """从 messages 表提取 segment digest。纯文本分析，不调 LLM。"""
    try:
        rows = session_db._conn.execute(
            "SELECT role, content, tool_calls, tool_name, timestamp "
            "FROM messages WHERE session_id=? AND segment_index=? ORDER BY timestamp",
            (session_id, segment_index),
        ).fetchall()
    except Exception as e:
        logger.warning("Failed to query messages for segment %s[%d]: %s", session_id[:20], segment_index, e)
        return None

    if not rows:
        return None

    # 提取唤醒原因
    wake_reason = "unknown"
    tx_match = re.search(r"^tx_([a-z0-9_]+)_\d{4}_\d{4}_[0-9a-f]{6}$", session_id)
    if tx_match:
        wake_reason = tx_match.group(1)

    # 提取 tool calls
    tool_summaries: List[str] = []
    tool_counts: Dict[str, int] = {}
    for m in rows:
        if m["tool_calls"]:
            try:
                calls = json.loads(m["tool_calls"])
                for call in calls:
                    func = call.get("function", {})
                    name = func.get("name", "")
                    args_str = func.get("arguments", "{}")
                    try:
                        args = json.loads(args_str) if isinstance(args_str, str) else args_str
                    except Exception:
                        args = {}
                    summary = _summarize_tool_call(name, args)
                    if summary:
                        tool_summaries.append(summary)
                        tool_counts[name] = tool_counts.get(name, 0) + 1
            except Exception:
                pass

    # 时间范围
    start_time = rows[0]["timestamp"] if rows else 0
    end_time = rows[-1]["timestamp"] if rows else 0
    duration = end_time - start_time if start_time and end_time else 0

    # 结束方式
    end_reason = "unknown"
    last_tools = []
    for m in rows:
        if m["tool_calls"]:
            try:
                calls = json.loads(m["tool_calls"])
                for call in calls:
                    fname = call.get("function", {}).get("name", "")
                    last_tools.append(fname)
            except Exception:
                pass
    if "rest" in last_tools:
        end_reason = "主动休息"
    else:
        end_reason = "session结束"

    # 去重 tool summaries
    deduped = _dedup_tool_summaries(tool_summaries)

    start_dt = datetime.fromtimestamp(start_time, tz=timezone.utc).astimezone()
    time_str = start_dt.strftime("%Y-%m-%d %H:%M")

    digest = {
        "session_id": session_id,
        "segment_index": segment_index,
        "time": time_str,
        "wake_reason": wake_reason,
        "duration_sec": round(duration),
        "tool_summary": deduped,
        "tool_counts": tool_counts,
        "end_reason": end_reason,
        "message_count": len(rows),
        "start_msg_id": rows[0]["id"] if rows else None,
        "end_msg_id": rows[-1]["id"] if rows else None,
    }

    return digest


def _generate_segment_narrative_llm(
    session_db: Any,
    session_id: str,
    segment_index: int,
    db_path: str,
) -> Optional[str]:
    """调 LLM 生成段叙事（第一人称结构化叙事）。"""
    try:
        llm_call = _resolve_llm_call()
        if llm_call is None:
            return None

        rows = session_db._conn.execute(
            "SELECT role, content, tool_calls, timestamp "
            "FROM messages WHERE session_id=? AND segment_index=? ORDER BY timestamp",
            (session_id, segment_index),
        ).fetchall()

        messages = [dict(row) for row in rows]
        for m in messages:
            if m.get("tool_calls"):
                try:
                    m["tool_calls"] = json.loads(m["tool_calls"])
                except Exception:
                    m["tool_calls"] = []

        conversation = _build_segment_conversation_text(messages)
        if not conversation:
            return None

        prompt = _SEGMENT_NARRATIVE_PROMPT.format(
            session_id=session_id,
            segment_index=segment_index,
            conversation=conversation[:6000],
        )

        summary = llm_call(prompt=prompt, timeout=30.0).strip()
        return summary if summary else None

    except Exception as e:
        logger.warning("LLM segment narrative failed for %s[%d]: %s", session_id[:20], segment_index, e)
        return None


def _generate_segment_narrative_fallback(digest_data: Dict[str, Any]) -> str:
    """生成规则兜底的段叙事（LLM 失败时使用）。"""
    tool_counts_str = ", ".join(
        f"{k}×{v}" for k, v in sorted(digest_data.get("tool_counts", {}).items(), key=lambda x: -x[1])
        if k not in _IGNORE_TOOLS
    )
    parts = [
        f"session_id: {digest_data.get('session_id', '')}",
        f"segment_index: {digest_data.get('segment_index', 0)}",
        f"mission: |",
        f"  持续{digest_data.get('duration_sec', 0)}秒，结束原因：{digest_data.get('end_reason', '')}",
        "decisions: []",
        "trajectory: []",
        "outputs:",
        f"  tool_summary: {tool_counts_str}",
        "surprises: []",
        "open_threads: []",
        "entities: []",
    ]
    return "\n".join(parts)


def _save_segment_narrative(
    db: sqlite.Connection,
    session_id: str,
    segment_index: int,
    narrative: str,
    digest_data: Dict[str, Any],
    fallback: bool = False,
) -> None:
    """保存段叙事到 memory_layers 表。"""
    period = f"{session_id}#{segment_index}"
    start_time = digest_data.get("start_time", 0) or 0
    end_time = digest_data.get("end_time", 0) or 0
    tool_summary_json = json.dumps(digest_data.get("tool_counts", {}))

    db.execute(
        "INSERT OR REPLACE INTO memory_layers "
        "(layer, period, digest, llm_summary, tool_summary, start_time, end_time, parent_ids, created_at, fallback) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "segment",
            period,
            narrative,
            narrative,
            tool_summary_json,
            start_time,
            end_time,
            json.dumps([session_id]),
            time.time(),
            1 if fallback else 0,
        ),
    )
    db.commit()


def _generate_all_segment_narratives(session_db: Any, session_id: str, db: sqlite.Connection) -> int:
    """为 session 的所有段生成叙事。返回生成数量。"""
    if not _should_generate_segment(session_db, session_id):
        return 0

    try:
        segment_count = session_db.get_segment_count(session_id)
    except Exception:
        return 0

    generated = 0
    for seg_idx in range(segment_count):
        period = f"{session_id}#{seg_idx}"
        existing = db.execute(
            "SELECT 1 FROM memory_layers WHERE layer='segment' AND period=?",
            (period,),
        ).fetchone()
        if existing:
            continue

        digest_data = _generate_segment_digest(session_db, session_id, seg_idx)
        if not digest_data:
            continue

        narrative = _generate_segment_narrative_llm(session_db, session_id, seg_idx, str(_get_db_path()))
        fallback = False
        if not narrative:
            narrative = _generate_segment_narrative_fallback(digest_data)
            fallback = True

        _save_segment_narrative(db, session_id, seg_idx, narrative, digest_data, fallback)
        generated += 1

        logger.info("Segment narrative generated: %s[%d] fallback=%s", session_id[:20], seg_idx, fallback)

    return generated


def _lazy_generate_segment_narrative(
    session_db: Any,
    session_id: str,
    segment_index: int,
) -> Optional[str]:
    """惰性补生成：压缩时发现某段无叙事，临时同步生成。"""
    db = _get_db()
    try:
        period = f"{session_id}#{segment_index}"
        existing = db.execute(
            "SELECT llm_summary FROM memory_layers WHERE layer='segment' AND period=?",
            (period,),
        ).fetchone()
        if existing and existing["llm_summary"]:
            return existing["llm_summary"]

        digest_data = _generate_segment_digest(session_db, session_id, segment_index)
        if not digest_data:
            return None

        narrative = _generate_segment_narrative_llm(session_db, session_id, segment_index, str(_get_db_path()))
        fallback = False
        if not narrative:
            narrative = _generate_segment_narrative_fallback(digest_data)
            fallback = True

        _save_segment_narrative(db, session_id, segment_index, narrative, digest_data, fallback)
        return narrative
    finally:
        db.close()


def load_segment_narrative(session_id: str, segment_index: int) -> Optional[str]:
    """从 DB 加载段叙事。"""
    db = _get_db()
    try:
        period = f"{session_id}#{segment_index}"
        row = db.execute(
            "SELECT llm_summary FROM memory_layers WHERE layer='segment' AND period=?",
            (period,),
        ).fetchone()
        return row["llm_summary"] if row and row["llm_summary"] else None
    finally:
        db.close()


def update_entity_index_from_narrative(narrative: str) -> None:
    """从叙事中提取实体并同步进 entity_index。

    P1 T017 修复(recon 确认的两个 bug):
      1. 旧代码 import `add_entity`(entity_index 不存在该函数) → ImportError
         被外层 try/except 吞 → 整个函数静默 no-op。
      2. 旧代码把 entity 当 dict 调 .get("name"), 但 extract_entities_from_context
         返回 list[str] → AttributeError(双重 bug)。

    本重写按 spec Clarifications Q1 + design doc §1 "4 源自动实体":
    extract 后返回字符串列表,每个 name 经 sync_entity_from_source 写入。
    失败容忍(单个实体出错不影响其它),但仍会在控制台暴露。
    """
    try:
        from domain.memory.memory.consciousness.entity_index import (
            extract_entities_from_context,
            sync_entity_from_source,
        )
    except ImportError as ie:
        logger.warning("entity_index import failed (cannot sync narrative entities): %s", ie)
        return

    entities: list[str] = extract_entities_from_context(narrative)
    if not entities:
        return

    summary_preview = narrative.strip().replace("\n", " ")[:200]
    for entity_name in entities:
        if not entity_name or not entity_name.strip():
            continue
        try:
            # 与 contacts/store.create_contact 的调用方式一致:position name, kw 其他。
            # 来源 narrative 不会强加 entity_type(模型可在后续 memory_hygiene
            # 周期里给它准确类型);sync 是 create-or-update 幂等。
            sync_entity_from_source(
                entity_name.strip(),
                entity_type="concept",
                summary=summary_preview,
            )
        except Exception as per_entity_err:
            # 窄 try/except:一个实体的失败不应阻塞其它实体,
            # 但要可见(避免回归"完全静默")。
            logger.debug(
                "sync_entity_from_source failed for %r: %s",
                entity_name, per_entity_err,
            )


_SESSION_SUMMARY_PROMPT = """你是一个数字生命的记忆总结助手。请根据以下对话记录，生成一段简洁的记忆摘要。

要求：
- 2-5句话，提取关键信息（聊了什么、做了什么决定、用户表达了什么偏好或情绪、数字生命主动做了什么）
- 忽略例行性的感知类动作（如查看状态、获取时间等）
- 关注结论性、有意义的内容
- 使用中文
- 只输出摘要，不要前缀

对话记录：
{conversation}
""".strip()


def _build_conversation_text(rows: list, session_id: str) -> str:
    """把 session messages 构建成 LLM 可读的对话文本。"""
    parts = []
    for m in rows:
        if m["role"] == "system":
            continue  # skip persona/lifecycle prompt — not conversation content
        role = "用户" if m["role"] == "user" else "数字生命"
        content = (m["content"] or "").strip()
        if not content:
            # tool_calls 摘要
            try:
                calls = json.loads(m["tool_calls"] or "[]")
                lines = []
                for call in calls:
                    fn = call.get("function", {})
                    name = fn.get("name", "")
                    args_str = fn.get("arguments", "{}")
                    try:
                        args = json.loads(args_str)
                    except Exception:
                        args = {}
                    if name == "express_to_human":
                        lines.append(f"[发消息]: {args.get('text', '')[:80]}")
                    elif name == "write_diary":
                        lines.append(f"[写日记]: {args.get('text', '')[:80]}")
                    elif name == "record_thought":
                        lines.append(f"[思绪]: {args.get('text', '')[:80]}")
                    elif name == "update_scratchpad":
                        lines.append(f"[更新笔记]")
                    elif name == "terminal":
                        lines.append(f"[命令]: {args.get('command', '')[:80]}")
                    elif name == "execute_code":
                        lines.append(f"[执行代码]: {args.get('code', '')[:80]}")
                    else:
                        lines.append(f"[{name}]")
                if lines:
                    content = " | ".join(lines)
            except Exception:
                pass
        if content:
            ts = datetime.fromtimestamp(m["timestamp"], tz=timezone.utc).astimezone().strftime("%H:%M")
            # Cap user messages at 120 chars — long ref_context blobs are noise for summaries
            if m["role"] == "user":
                content = content[:120]
            parts.append(f"[{ts}] {role}: {content}")
    return "\n".join(parts)


_SEGMENT_NARRATIVE_RETENTION_HOURS = 168  # 7天


def _generate_segment_narratives_async(session_db: Any, session_id: str) -> None:
    """后台线程：生成所有段的叙事，不阻塞主流程。"""
    t = threading.Thread(
        target=_generate_segment_narratives_worker,
        args=(session_db, session_id, str(_get_db_path())),
        daemon=True,
    )
    t.start()


def _generate_segment_narratives_worker(session_db: Any, session_id: str, db_path: str) -> None:
    """后台线程 worker：生成段叙事并更新 entity_index。"""
    try:
        db = sqlite.connect(db_path)
        db.row_factory = sqlite.Row
        generated = _generate_all_segment_narratives(session_db, session_id, db)

        # 从已生成的叙事中提取实体并更新 index
        if generated > 0:
            rows = db.execute(
                "SELECT llm_summary, period FROM memory_layers WHERE layer='segment' AND period LIKE ?",
                (f"{session_id}#%",),
            ).fetchall()
            for row in rows:
                if row["llm_summary"]:
                    # P1 T015: segment narrative 必须进向量索引。
                    # recon 确认:旧实现写进 memory_layers 后从不切片,造成
                    # "最该读到的第一人称经历在检索里蒸发"。修复:与 session 摘要
                    # 一样走 _index_digest_to_vectors(layer="segment", period=row["period"])。
                    # period 形如 f"{session_id}#{seg_idx}",作为 chunk_hash 用。
                    try:
                        _index_digest_to_vectors(
                            row["llm_summary"], "segment", row["period"]
                        )
                    except Exception as idx_err:
                        logger.warning(
                            "Failed to index segment vector (session=%s period=%s): %s",
                            session_id[:20], row["period"], idx_err,
                        )
                    update_entity_index_from_narrative(row["llm_summary"])

        db.close()
        logger.info("Segment narratives generated for %s: %d segments", session_id[:20], generated)
    except Exception as e:
        logger.warning("Segment narrative generation failed for %s: %s", session_id[:20], e)


def _llm_summary_worker(
    session_db: Any,
    session_id: str,
    db_path: str,
) -> None:
    """后台线程：调 LLM 生成 session 摘要并写回 DB。"""
    try:
        llm_call = _resolve_llm_call()
        if llm_call is None:
            return

        rows = session_db._conn.execute(
            "SELECT role, content, tool_calls, timestamp "
            "FROM messages WHERE session_id=? ORDER BY timestamp",
            (session_id,),
        ).fetchall()

        conversation = _build_conversation_text(rows, session_id)
        if not conversation:
            return

        prompt = _SESSION_SUMMARY_PROMPT.format(conversation=conversation[:4000])

        summary = llm_call(prompt=prompt, timeout=30.0).strip()
        if not summary:
            return

        # 写回 memory_layers 表
        db = sqlite.connect(db_path)
        db.row_factory = sqlite.Row
        db.execute(
            "UPDATE memory_layers SET llm_summary=? WHERE layer='session' AND period=?",
            (summary, session_id),
        )
        db.commit()
        db.close()

        logger.info("LLM session summary generated for %s: %s", session_id[:20], summary[:60])

        # 更新向量索引（用 llm_summary 替代旧 digest）
        try:
            from domain.memory.memory.recall.vector import _embed_single, _embedding_to_blob
            emb = _embed_single(summary)
            if emb:
                vec_db_path = str(_get_mem_dir() / "memory_vectors.db")
                vdb = sqlite.connect(vec_db_path)
                vdb.row_factory = sqlite.Row
                blob = _embedding_to_blob(emb)
                chunk_hash = f"session:{session_id}"
                vdb.execute(
                    "INSERT OR REPLACE INTO chunks "
                    "(source, chunk_hash, text, embedding, file_mtime, created_at, "
                    " phase, source_kind, session_id, provenance, cognition_state) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (f"digest_session", chunk_hash, summary, blob, time.time(), time.time(),
                     "experience", "digest", session_id,
                     f"digest_session:{session_id}", "active"),
                )
                vdb.commit()
                vdb.close()
        except Exception as e:
            logger.debug("Failed to update vector index with LLM summary: %s", e)

    except Exception as e:
        logger.warning("LLM session summary failed for %s: %s", session_id[:20], e)


def dispatch_llm_summary(session_db: Any, session_id: str) -> None:
    """后台线程 dispatch LLM 总结，不阻塞主流程。"""
    t = threading.Thread(
        target=_llm_summary_worker,
        args=(session_db, session_id, str(_get_db_path())),
        daemon=True,
    )
    t.start()


# ──────────────────── Day Digest ────────────────────

def _generate_day_digest(
    db: sqlite.Connection,
    date_str: str,
) -> Optional[str]:
    """合并当天所有 session digest 成日摘要。"""
    # session_id 格式如 tx_initiative_0715_1914_xxx，含 MMDD 而非 YYYY-MM-DD。
    # 用 start_time 时间戳（UTC > UTC 当天的 start/end）精确过滤，
    # 而非依赖 period 字符串 LIKE。
    day_start_ts = 0
    day_end_ts = 0
    try:
        from datetime import datetime as _dt, timezone as _tz
        day_dt = _dt.strptime(date_str, "%Y-%m-%d").replace(tzinfo=_tz.utc)
        day_start_ts = day_dt.timestamp()
        day_end_ts = day_start_ts + 86400
    except Exception:
        pass

    if day_start_ts > 0:
        rows = db.execute(
            "SELECT * FROM memory_layers WHERE layer='session' "
            "AND start_time >= ? AND start_time < ? ORDER BY start_time",
            (day_start_ts, day_end_ts),
        ).fetchall()
    else:
        # fallback: 旧逻辑
        rows = db.execute(
            "SELECT * FROM memory_layers WHERE layer='session' "
            "AND period LIKE ? ORDER BY start_time",
            (f"%{date_str[5:]}%",),  # 取 MM-DD 部分
        ).fetchall()

    if not rows:
        return None

    # 收集当天所有工具统计
    all_tool_counts: Dict[str, int] = {}
    session_ids: List[str] = []
    energy_values: List[float] = []
    express_count = 0

    for row in rows:
        session_ids.append(row["period"])
        ts = json.loads(row["tool_summary"] or "[]")
        for entry in ts:
            if entry.startswith("发消息:"):
                express_count += 1
        if row["tool_summary"]:
            try:
                tc = json.loads(row.get("tool_summary") or "{}")
            except Exception:
                tc = {}
            for k, v in tc.items():
                all_tool_counts[k] = all_tool_counts.get(k, 0) + v

    # 从 session digest 提取精力
    for row in rows:
        digest = row["digest"]
        energies = re.findall(r"精力([\d.]+)→", digest)
        energies.extend(re.findall(r"→([\d.]+)", digest))
        for e_str in energies:
            try:
                energy_values.append(float(e_str))
            except ValueError:
                pass

    # 聚合主题
    topics = []
    tool_words = {
        "write_diary": "写日记",
        "express_to_human": "发消息",
        "record_thought": "记录思绪",
        "update_scratchpad": "更新笔记",
        "manage_goals": "管理目标",
        "manage_daily": "管理计划",
    }
    for tool, word in tool_words.items():
        cnt = all_tool_counts.get(tool, 0)
        if cnt > 0:
            topics.append(f"{word}{cnt}次")

    # 优先用 llm_summary，没生成的话 fallback 到 digest
    session_summaries = []
    for row in rows:
        s = row["llm_summary"] or row["digest"]
        if s:
            session_summaries.append(s)
    digest_text = " ".join(session_summaries)
    key_topics = _extract_topics(digest_text)

    # 精力范围
    energy_str = ""
    if energy_values:
        energy_str = f"精力{min(energy_values):.0f}→{max(energy_values):.0f}"

    parts = [f"{date_str}: {', '.join(topics)}"]
    if key_topics:
        parts.append(", ".join(key_topics))
    if express_count:
        parts.append(f"给哥发了{express_count}条消息")
    if energy_str:
        parts.append(energy_str)

    day_digest = " | ".join(parts)

    # 如果有 LLM 生成的 session 摘要，拼接成日级 llm_summary
    llm_parts = []
    for row in rows:
        sid = row["period"]
        llm = row["llm_summary"]
        if llm:
            llm_parts.append(f"[{sid}] {llm}")
    day_llm_summary = "\n".join(llm_parts) if llm_parts else None

    # 存储日摘要
    db.execute(
        "INSERT OR REPLACE INTO memory_layers "
        "(layer, period, digest, llm_summary, tool_summary, start_time, end_time, parent_ids, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "day",
            date_str,
            day_digest,
            day_llm_summary,
            json.dumps(all_tool_counts),
            rows[0]["start_time"] if rows else 0,
            rows[-1]["end_time"] if rows else 0,
            json.dumps(session_ids),
            time.time(),
        ),
    )
    db.commit()

    # 建立层间联想链接
    _build_cross_layer_links(db, "day", date_str, session_ids)

    return day_digest


def _extract_topics(text: str) -> List[str]:
    """从 digest 文本中提取高频有意义的关键词。"""
    return extract_topics(text)


# ──────────────────── Week Digest ────────────────────

def _maybe_generate_week_digest(db: sqlite.Connection) -> Optional[str]:
    """如果距上次周摘要 >= 7 天且日摘要 >= 7，生成周摘要。"""
    # 检查上次周摘要
    last_week = db.execute(
        "SELECT * FROM memory_layers WHERE layer='week' ORDER BY start_time DESC LIMIT 1"
    ).fetchone()

    # 获取最近7天的日摘要
    recent_days = db.execute(
        "SELECT * FROM memory_layers WHERE layer='day' "
        "ORDER BY start_time DESC LIMIT 7"
    ).fetchall()

    if len(recent_days) < 3:
        return None  # 日摘要太少，不值得生成周摘要

    # 检查是否需要生成（上次周摘要覆盖的日摘要不在最近7天中）
    if last_week:
        parent_ids = json.loads(last_week["parent_ids"] or "[]")
        # 如果最近7天都已经被上次周摘要覆盖了，就不需要重新生成
        covered = set(parent_ids)
        recent_day_ids = [r["period"] for r in recent_days]
        if all(d in covered for d in recent_day_ids):
            return None

    # 优先用 llm_summary（包含所有 session 的 LLM 摘要），fallback 到 digest
    all_summaries = []
    for r in recent_days:
        s = r.get("llm_summary") or r["digest"]
        if s:
            all_summaries.append(s)
    all_text = "\n".join(all_summaries)

    topics = _extract_topics(all_text)

    if not topics:
        return None

    # 提取日期范围
    dates = [r["period"] for r in recent_days]
    week_label = f"{dates[-1]}~{dates[0]}"  # 最早~最近

    week_digest = f"本周主线: {', '.join(topics[:5])}"

    # 计算周数
    if recent_days:
        start_dt = datetime.fromtimestamp(recent_days[-1]["start_time"])
        week_num = start_dt.isocalendar()[1]
        period = f"W{week_num}"

    # 周级 llm_summary = 所有日摘要的 llm_summary 拼接
    week_llm_summary = "\n".join(
        f"[{r['period']}] {r.get('llm_summary') or r['digest']}"
        for r in recent_days
        if r.get("llm_summary") or r["digest"]
    )

    db.execute(
        "INSERT OR REPLACE INTO memory_layers "
        "(layer, period, digest, llm_summary, tool_summary, start_time, end_time, parent_ids, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "week",
            period,
            week_digest,
            week_llm_summary,
            "{}",
            recent_days[-1]["start_time"] if recent_days else 0,
            recent_days[0]["end_time"] if recent_days else 0,
            json.dumps(dates),
            time.time(),
        ),
    )
    db.commit()

    # 建立层间链接
    _build_cross_layer_links(db, "week", period, dates)

    logger.info("Generated week digest: %s", week_digest[:80])
    return week_digest


# ──────────────────── Cross-layer Links ────────────────────

def _build_cross_layer_links(
    db: sqlite.Connection,
    parent_layer: str,
    parent_period: str,
    child_periods: List[str],
) -> None:
    """建立父层 digest 与子层 digest 之间的联想链接。"""
    # 获取父层 chunk id
    parent_hash = f"{parent_layer}:{parent_period}"
    parent_row = db.execute(
        "SELECT id FROM chunks WHERE chunk_hash=?", (parent_hash,)
    ).fetchone()

    if not parent_row:
        return

    parent_id = parent_row["id"]
    now = time.time()

    for child_period in child_periods:
        child_layer = "session" if child_period.startswith("l4_") else "day"
        child_hash = f"{child_layer}:{child_period}"
        child_row = db.execute(
            "SELECT id FROM chunks WHERE chunk_hash=?", (child_hash,)
        ).fetchone()

        if not child_row:
            continue

        child_id = child_row["id"]
        a, b = min(parent_id, child_id), max(parent_id, child_id)

        db.execute(
            "INSERT OR REPLACE INTO associations (chunk_a, chunk_b, weight, last_activated) "
            "VALUES (?, ?, 3.0, ?)",
            (a, b, now),
        )
    db.commit()


# ──────────────────── Vector Index ────────────────────

def _index_digest_to_vectors(
    digest_text: str,
    layer: str,
    period: str,
) -> None:
    """将 digest 向量化并存入 memory_vectors.db。"""
    try:
        from domain.memory.memory.recall.vector import _embed_single, _get_db as _get_vec_db
        from domain.memory.memory.recall.vector import _embedding_to_blob
    except ImportError:
        return

    embedding = _embed_single(digest_text)
    if not embedding:
        return

    import hashlib
    chunk_hash = f"{layer}:{period}"

    try:
        vec_db = _get_vec_db()
        blob = _embedding_to_blob(embedding)
        # P1 T016:顺便写入 phase 字段(P1 不消费,但 schema 已就位 — 见 spec Clarifications Q1)。
        # 准入门槛: 骨架格式 digest (无 LLM 总结, 只是时间戳+工具列表) 不索引
        stripped_digest = digest_text.strip()
        is_skeleton = (
            (len(stripped_digest) < 150)
            and ("工具:" in stripped_digest or "结束:" in stripped_digest or stripped_digest.startswith("["))
        )
        if is_skeleton:
            return  # 骨架摘要无知识价值, 跳过索引

        # 所有 digest_* 都是 experience(经历摘要),按 data-model.md 相位映射表。
        # P3 T040: session_id + segment_index 填入(用于时序邻居连边 §4 三类连边②)。
        # segment 的 period 形如 "{session_id}#{seg_idx}";session/day 的 period 直接是 session_id/日期。
        seg_id = ""
        seg_idx = None
        if layer == "segment" and "#" in period:
            parts = period.split("#", 1)
            seg_id, rest = parts[0], parts[1]
            try:
                seg_idx = int(rest)
            except Exception:
                pass
        elif layer in ("session", "day", "week"):
            seg_id = period  # 完整 period 作为 session identifier
        vec_db.execute(
            "INSERT OR REPLACE INTO chunks "
            "(source, chunk_hash, text, embedding, file_mtime, created_at, phase, session_id, segment_index, provenance, cognition_state) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"digest_{layer}", chunk_hash, digest_text, blob, time.time(), time.time(),
             "experience", seg_id, seg_idx,
             f"digest_{layer}:{period}:{seg_idx or 0}", "active"),
        )
        vec_db.commit()
        vec_db.close()
    except Exception as e:
        # P1 SC-003: 不再静默 debug;向量索引失败也可能影响召回质量,用 warning 暴露。
        logger.warning("Failed to index digest vector (layer=%s period=%s): %s", layer, period, e)


# ──────────────────── Main Entry Point ────────────────────

def consolidate_after_session(session_db: Any, session_id: str) -> None:
    """Session 结束后调用：生成 digest，尝试生成日摘要/周摘要，建立向量索引。"""
    if not session_db:
        return

    db = _get_db()
    try:
        # 1. 生成 session digest
        digest_data = _generate_session_digest(session_db, session_id)
        if not digest_data:
            return

        digest_text = _format_session_digest(digest_data)
        tool_summary_json = json.dumps(digest_data.get("tool_counts", {}))

        # 时间范围
        start_time = 0
        end_time = 0
        try:
            rows = session_db._conn.execute(
                "SELECT MIN(timestamp) as mn, MAX(timestamp) as mx FROM messages WHERE session_id=?",
                (session_id,),
            ).fetchone()
            start_time = rows["mn"] or 0
            end_time = rows["mx"] or 0
        except Exception:
            pass

        db.execute(
            "INSERT OR REPLACE INTO memory_layers "
            "(layer, period, digest, tool_summary, start_time, end_time, parent_ids, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("session", session_id, digest_text, tool_summary_json,
             start_time, end_time, "[]", time.time()),
        )
        db.commit()

        logger.info("Session digest generated: %s → %s", session_id[:40], digest_data["narrative"][:60])

        # 1.5 生成段叙事（异步，不阻塞主流程）
        # 在 LLM session 总结之后执行，共享上下文
        _generate_segment_narratives_async(session_db, session_id)

        # 1.6 异步 LLM 总结（不阻塞主流程）
        dispatch_llm_summary(session_db, session_id)

        # 2. 向量索引 session digest
        _index_digest_to_vectors(digest_text, "session", session_id)

        # 3. 尝试生成日摘要（当天可能还有其他 session）
        try:
            now = _now_dt_hook()
            date_str = now.strftime("%Y-%m-%d")
            _generate_day_digest(db, date_str)
        except Exception as e:
            logger.debug("Day digest skipped: %s", e)

        # 4. 尝试生成周摘要
        try:
            _maybe_generate_week_digest(db)
        except Exception as e:
            logger.debug("Week digest skipped: %s", e)

        # 5. 向量索引日摘要和周摘要
        try:
            for row in db.execute("SELECT layer, period, digest FROM memory_layers WHERE layer IN ('day','week') ORDER BY created_at DESC LIMIT 2"):
                _index_digest_to_vectors(row["digest"], row["layer"], row["period"])
        except Exception:
            pass

    except Exception as e:
        logger.error("Consolidation failed for %s: %s", session_id, e)
    finally:
        db.close()

    # 5.5. 索引当前session的对话消息（动态源）
    try:
        index_conversations(session_db, max_age_hours=24.0)
    except Exception as e:
        logger.debug("Conversation indexing skipped: %s", e)

    # 6. 定期清理旧数据（每10次session清理一次）
    try:
        if hash(session_id) % 10 == 0:
            _cleanup_old_digests(db)
    except Exception as e:
        logger.debug("Cleanup skipped: %s", e)

    # 7. 关联到活跃任务
    try:
        _resolve_on_session_end()(session_id, digest_text if digest_data else "")
    except Exception:
        pass


def backfill_existing_sessions(session_db: Any, limit: int = 50) -> int:
    """回填已有的 session digest（用于冷启动）。"""
    if not session_db:
        return 0

    try:
        rows = session_db._conn.execute(
            "SELECT id FROM sessions "
            "WHERE id LIKE 'tx_%' OR id LIKE 'l4_wake_%' "
            "ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except Exception:
        return 0

    count = 0
    for row in rows:
        sid = row["id"]
        db = _get_db()
        try:
            existing = db.execute(
                "SELECT 1 FROM memory_layers WHERE layer='session' AND period=?", (sid,)
            ).fetchone()
            if existing:
                continue
            consolidate_after_session(session_db, sid)
            count += 1
        finally:
            db.close()

    logger.info("Backfilled %d session digests", count)
    return count


# ──────────────────── Recall (供工具使用) ────────────────────

def recall_memories(
    query: str,
    depth: str = "digest",
    limit: int = 5,
) -> str:
    """检索记忆，按 depth 返回不同粒度的结果。

    depth:
      'digest'    → session + day + week digest
      'original'  → 原始 messages 中的相关片段
    """
    from domain.memory.memory.recall.vector import _embed_single, _cosine_sim, ensure_indexed, _get_db as _get_vec_db
    from domain.memory.memory.recall.vector import _blob_to_embedding

    ensure_indexed(max_age_hours=6.0)

    query_emb = _embed_single(query)
    if not query_emb:
        return ""

    results: List[Tuple[float, str, str]] = []

    if depth in ("digest", "session"):
        # 检索 digest 向量
        vec_db = _get_vec_db()
        try:
            rows = vec_db.execute(
                "SELECT source, text, embedding FROM chunks WHERE source LIKE 'digest_%' AND embedding IS NOT NULL"
            ).fetchall()

            for row in rows:
                emb = _blob_to_embedding(row["embedding"])
                sim = _cosine_sim(query_emb, emb)
                if sim >= 0.2:
                    results.append((sim, row["source"], row["text"]))
        finally:
            vec_db.close()

    if depth == "original":
        # 检索原始 messages
        try:
            sdb_factory = _resolve_session_db_factory()
            if sdb_factory is None:
                return ""
            sdb = sdb_factory()
            rows = sdb._conn.execute(
                "SELECT session_id, role, content, timestamp FROM messages "
                "WHERE role IN ('user', 'assistant') AND content IS NOT NULL "
                "ORDER BY timestamp DESC LIMIT 500"
            ).fetchall()

            for row in rows:
                content = row["content"] or ""
                if len(content) < 10:
                    continue
                content_emb = _embed_single(content[:200])
                if content_emb:
                    sim = _cosine_sim(query_emb, content_emb)
                    if sim >= 0.4:
                        results.append((sim, f"msg:{row['session_id'][:30]}", content[:200]))
        except Exception:
            pass

    if not results:
        return ""

    results.sort(key=lambda x: x[0], reverse=True)
    lines = ["[记忆检索 — depth={depth}]"]
    for i, (score, source, text) in enumerate(results[:limit]):
        lines.append(f"\n[{source} score={score:.2f}] {text[:150]}")

    lines.append("\n[/记忆检索]")
    return "".join(lines)


def recall_session(session_id: str) -> str:
    """检索指定 session 的完整 digest + 工具摘要。"""
    db = _get_db()
    try:
        row = db.execute(
            "SELECT * FROM memory_layers WHERE layer='session' AND period=?", (session_id,)
        ).fetchone()
        if not row:
            return f"(未找到 session {session_id} 的 digest)"
        result = f"[Session Digest: {session_id}]\n\n{row['digest']}"
        if row["tool_summary"]:
            try:
                tc = json.loads(row["tool_summary"])
                if tc:
                    result += f"\n\n工具统计: {json.dumps(tc, ensure_ascii=False)}"
            except Exception:
                pass
        return result
    finally:
        db.close()


# ──────────────────── 飞书对话索引 ────────────────────

def _get_state_db() -> Optional[sqlite.Connection]:
    """获取 state.db 连接。"""
    try:
        state_path = get_runtime_state_db_path()
        if not state_path.exists():
            return None
        db = sqlite.connect(str(state_path))
        db.row_factory = sqlite.Row
        return db
    except Exception:
        return None


def _conversation_role_labels() -> dict[str, str]:
    return {
        "user": os.getenv("DIGITAL_LIFE_HUMAN_DISPLAY_NAME", "user"),
        "assistant": os.getenv("DIGITAL_LIFE_DISPLAY_NAME", "assistant"),
    }


def _index_single_conversation(
    vec_db: sqlite.Connection,
    session_id: str,
    role: str,
    content: str,
    timestamp: float,
) -> bool:
    """将单条对话消息向量化并存入 memory_vectors.db。"""
    if not content or len(content.strip()) < 5:
        return False

    # 跳过纯工具输出（JSON、纯数字等）
    text = content.strip()
    if text.startswith("{") or text.startswith("["):
        return False
    if len(text) < 10:
        return False

    # 截断过长内容，保留核心语义
    text = text[:300]

    # 构建 chunk
    role_label = _conversation_role_labels().get(role, role)
    indexed_text = f"[{role_label}] {text}"

    chunk_hash_val = f"conversation:{session_id}:{timestamp}"

    # 检查是否已索引
    existing = vec_db.execute(
        "SELECT 1 FROM chunks WHERE source='conversation' AND chunk_hash=?",
        (chunk_hash_val,),
    ).fetchone()
    if existing:
        return False

    try:
        from domain.memory.memory.recall.vector import _embed_single, _embedding_to_blob
    except ImportError:
        return False

    embedding = _embed_single(indexed_text)
    if not embedding:
        return False

    blob = _embedding_to_blob(embedding)
    # P1-3 fix: conversation 写入时填 entity_links (之前 1% 填充率 = 导航骨架形同虚设)
    entity_links_json = "[]"
    try:
        from domain.memory.memory.consciousness.entity_index import (
            extract_entities_from_context,
        )
        entities = extract_entities_from_context(content)
        if entities:
            import json as _j
            entity_links_json = _j.dumps(entities, ensure_ascii=False)
    except Exception:
        pass
    vec_db.execute(
        "INSERT OR REPLACE INTO chunks "
        "(source, chunk_hash, text, embedding, file_mtime, created_at, "
        " phase, source_kind, session_id, entity_links, provenance, cognition_state) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("conversation", chunk_hash_val, indexed_text, blob, timestamp, timestamp,
         "experience", "conversation", session_id, entity_links_json,
         f"conversation:{session_id}:{timestamp}", "active"),
    )
    return True


def index_conversations(session_db: Any = None, max_age_hours: float = 1.0) -> int:
    """索引最近的飞书对话消息到向量库。

    从 state.db 读取飞书 session 的 user/assistant 消息，
    向量化后存入 memory_vectors.db 的 chunks 表（source='conversation'）。

    Args:
        session_db: SessionDB 实例（可选，用于兼容）
        max_age_hours: 只索引最近 N 小时内的消息

    Returns:
        新索引的消息数
    """
    state_db = _get_state_db()
    if not state_db:
        logger.warning("state.db not found, skipping conversation indexing")
        return 0

    try:
        from domain.memory.memory.recall.vector import _get_db as _get_vec_db
        from domain.memory.memory.recall.vector import _embed_texts, _embedding_to_blob

        vec_db = _get_vec_db()
        try:
            cutoff = time.time() - max_age_hours * 3600

            # 查询所有 session 中最近的消息（不限定 source='feishu'——session source 实际
            # 值是 'l4_wake' 不是 'feishu'，之前的过滤导致永远查不到行）
            rows = state_db.execute(
                "SELECT m.session_id, m.role, m.content, m.timestamp "
                "FROM messages m "
                "JOIN sessions s ON m.session_id = s.id "
                "WHERE m.role IN ('user', 'assistant') "
                "AND m.content IS NOT NULL "
                "AND m.timestamp > ? "
                "ORDER BY m.timestamp ASC",
                (cutoff,),
            ).fetchall()

            if not rows:
                return 0

            # 批量 embedding（减少 API 调用）
            texts_to_index = []
            for row in rows:
                content = row["content"]
                if not content or len(content.strip()) < 5:
                    continue
                text = content.strip()[:300]
                if text.startswith("{") or text.startswith("[") or len(text) < 80:
                    continue  # <80 字不索引(自言自语/碎片拦截)
                # B 治源(2026-07-17): skip wake_template 等垃圾内容
                if _is_noise_content(text):
                    continue
                # 准入门槛: assistant 自言自语(状态汇报/中间思维)不索引
                # 有价值的 conversation 是: user 的提问/指令 + assistant 的实质结论
                # 特征: role=assistant 且 text 像"思路过程"不是"结论" → 跳过
                role = row["role"]
                if role == "assistant":
                    stripped = text.strip()
                    # 明确的结论类标记 保留
                    has_conclusion = any(stripped.startswith(p) for p in (
                        "✅", "结论", "总结", "验证", "确认", "修复", "部署", "上线",
                    ))
                    # 中间过程短语 → 跳过
                    is_process = any(p in stripped[:20] for p in (
                        "现在", "让我", "让我看", "让我尝试", "让我检查", "先看",
                        "接下来", "再看看", "再试试", "好了，我已经",
                        "修复方案清晰", "Alpha 回复确认", "精力",
                    ))
                    if is_process and not has_conclusion:
                        continue
                # B 治源(2026-07-17): skip wake_template 等垃圾内容 -
                # 否则 system 注入的 wake prompt 会被当 user 对话喂进 chunks,
                # 跑 audit 时再清效率低。前置过滤更优。
                if _is_noise_content(text):
                    continue
                texts_to_index.append((row["session_id"], row["role"], text, row["timestamp"]))

            if not texts_to_index:
                return 0

            # 批量 embedding
            role_labels = _conversation_role_labels()
            embed_texts = []
            chunk_hashes = []
            indexed_texts = []
            # P0-1 fix: 保留 session_id 跟随 INSERT (之前丢了, 导致 session_id 100% 空)
            conv_sids = []
            for sid, role, text, ts in texts_to_index:
                chunk_hash_val = f"conversation:{sid}:{ts}"
                existing = vec_db.execute(
                    "SELECT 1 FROM chunks WHERE source='conversation' AND chunk_hash=?",
                    (chunk_hash_val,),
                ).fetchone()
                if existing:
                    continue
                role_label = role_labels.get(role, role)
                indexed_text = f"[{role_label}] {text}"
                embed_texts.append(indexed_text)
                chunk_hashes.append(chunk_hash_val)
                indexed_texts.append(indexed_text)
                conv_sids.append(sid or "")

            if not embed_texts:
                return 0

            # 分批 embedding（每批最多 20 条）
            count = 0
            batch_size = 20
            all_times = [ts for _, _, _, ts in texts_to_index]
            for i in range(0, len(embed_texts), batch_size):
                batch_texts = embed_texts[i:i + batch_size]
                batch_hashes = chunk_hashes[i:i + batch_size]
                batch_indexed = indexed_texts[i:i + batch_size]
                batch_times = all_times[i:i + batch_size]
                batch_sids = conv_sids[i:i + batch_size]

                # 过滤已存在的 hash
                existing_hashes = set()
                for h in batch_hashes:
                    ex = vec_db.execute(
                        "SELECT 1 FROM chunks WHERE source='conversation' AND chunk_hash=?",
                        (h,),
                    ).fetchone()
                    if ex:
                        existing_hashes.add(h)

                filtered = [
                    (t, h, idx, ts, sid)
                    for t, h, idx, ts, sid in zip(batch_texts, batch_hashes, batch_indexed, batch_times, batch_sids)
                    if h not in existing_hashes
                ]
                if not filtered:
                    continue

                ft, fh, fi, ftimes, fsids = zip(*filtered)
                embeddings = _embed_texts(list(ft))
                if not embeddings:
                    continue

                for emb, h, text, ts, sid in zip(embeddings, fh, fi, ftimes, fsids):
                    if emb is None:
                        continue
                    blob = _embedding_to_blob(emb)
                    # P1-3 fix: conversation INSERT 现在写 entity_links
                    ent_links = "[]"
                    try:
                        from domain.memory.memory.consciousness.entity_index import (
                            extract_entities_from_context,
                        )
                        ents = extract_entities_from_context(text)
                        if ents:
                            import json as _j
                            ent_links = _j.dumps(ents, ensure_ascii=False)
                    except Exception:
                        pass
                    vec_db.execute(
                        "INSERT OR REPLACE INTO chunks "
                        "(source, chunk_hash, text, embedding, file_mtime, created_at, "
                        " phase, source_kind, session_id, entity_links, provenance, cognition_state) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        ("conversation", h, text, blob, ts, ts,
                         "experience", "conversation", sid, ent_links,
                         f"conversation:{sid}:{ts}", "active"),
                    )
                    count += 1

            if count:
                vec_db.commit()
                logger.info("Indexed %d conversation messages to vector store", count)

            return count
        finally:
            vec_db.close()
    except Exception as e:
        logger.error("Conversation indexing failed: %s", e)
        return 0
    finally:
        state_db.close()


def backfill_conversations(limit: int = 500) -> int:
    """回填历史飞书对话到向量库（批量 embedding）。"""
    state_db = _get_state_db()
    if not state_db:
        return 0

    try:
        rows = state_db.execute(
            "SELECT m.session_id, m.role, m.content, m.timestamp "
            "FROM messages m "
            "JOIN sessions s ON m.session_id = s.id "
            "WHERE s.source = 'feishu' "
            "AND m.role IN ('user', 'assistant') "
            "AND m.content IS NOT NULL "
            "ORDER BY m.timestamp ASC "
            "LIMIT ?",
            (limit,),
        ).fetchall()

        if not rows:
            return 0

        role_labels = _conversation_role_labels()

        # 过滤有效消息
        valid = []
        seen = set()
        for row in rows:
            content = (row["content"] or "").strip()
            if len(content) < 10:
                continue
            if content.startswith("{") or content.startswith("["):
                continue
            key = f"{row['session_id']}:{row['timestamp']}"
            if key in seen:
                continue
            seen.add(key)
            role_label = role_labels.get(row["role"], row["role"])
            text = content[:300]
            valid.append((key, f"[{role_label}] {text}", row["timestamp"]))

        if not valid:
            return 0

        from domain.memory.memory.recall.vector import _get_db as _get_vec_db
        from domain.memory.memory.recall.vector import _embed_texts, _embedding_to_blob

        vec_db = _get_vec_db()
        try:
            count = 0
            batch_size = 20
            for i in range(0, len(valid), batch_size):
                batch = valid[i:i + batch_size]
                keys, texts, times = zip(*batch)

                to_embed = []
                to_embed_keys = []
                to_embed_times = []
                for k, t, ts in zip(keys, texts, times):
                    existing = vec_db.execute(
                        "SELECT 1 FROM chunks WHERE source='conversation' AND chunk_hash=?",
                        (f"conversation:{k}",),
                    ).fetchone()
                    if not existing:
                        to_embed.append(t)
                        to_embed_keys.append(f"conversation:{k}")
                        to_embed_times.append(ts)

                if not to_embed:
                    continue

                embeddings = _embed_texts(to_embed)
                if not embeddings:
                    logger.warning("Backfill embedding batch failed at offset %d", i)
                    continue

                for emb, chunk_hash, text, ts in zip(embeddings, to_embed_keys, to_embed, to_embed_times):
                    if emb is None:
                        continue
                    blob = _embedding_to_blob(emb)
                    # P0-1 + P1-3 fix: backfill_conversations 写 phase/source_kind/session_id + entity_links
                    hash_parts = chunk_hash.split(":", 2)
                    bf_sid = hash_parts[1] if len(hash_parts) >= 3 else ""
                    ent_links = "[]"
                    try:
                        from domain.memory.memory.consciousness.entity_index import (
                            extract_entities_from_context,
                        )
                        ents = extract_entities_from_context(text)
                        if ents:
                            import json as _j
                            ent_links = _j.dumps(ents, ensure_ascii=False)
                    except Exception:
                        pass
                    vec_db.execute(
                        "INSERT OR REPLACE INTO chunks "
                        "(source, chunk_hash, text, embedding, file_mtime, created_at, "
                        " phase, source_kind, session_id, entity_links, cognition_state) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        ("conversation", chunk_hash, text, blob, ts, ts,
                         "experience", "conversation", bf_sid, ent_links, "active"),
                    )
                    count += 1

                vec_db.commit()
                logger.info("Backfill progress: %d/%d messages indexed", count, len(valid))

            logger.info("Backfill complete: %d conversation messages indexed", count)
            return count
        finally:
            vec_db.close()
    except Exception as e:
        logger.error("Backfill failed: %s", e)
        return 0
    finally:
        state_db.close()




# ──────────────────── Retention / Cleanup ────────────────────

_DIGEST_SESSION_RETENTION_HOURS = 720  # 30天
_DIGEST_DAY_RETENTION_HOURS = 2160    # 90天
_DIGEST_WEEK_RETENTION_HOURS = 4320   # 180天
_DIGEST_SEGMENT_RETENTION_HOURS = 168 # 7天
_CONVERSATION_RETENTION_HOURS = 168   # 7天


def _cleanup_old_digests(db) -> dict:
    """清理过期的 digest 记录。"""
    now = time.time()
    stats = {}

    cutoff = now - _DIGEST_SESSION_RETENTION_HOURS * 3600
    result = db.execute("DELETE FROM memory_layers WHERE layer=? AND created_at < ?", ("session", cutoff))
    stats["session"] = result.rowcount

    cutoff = now - _DIGEST_DAY_RETENTION_HOURS * 3600
    result = db.execute("DELETE FROM memory_layers WHERE layer=? AND created_at < ?", ("day", cutoff))
    stats["day"] = result.rowcount

    cutoff = now - _DIGEST_WEEK_RETENTION_HOURS * 3600
    result = db.execute("DELETE FROM memory_layers WHERE layer=? AND created_at < ?", ("week", cutoff))
    stats["week"] = result.rowcount

    cutoff = now - _DIGEST_SEGMENT_RETENTION_HOURS * 3600
    result = db.execute("DELETE FROM memory_layers WHERE layer=? AND created_at < ?", ("segment", cutoff))
    stats["segment"] = result.rowcount

    if any(v > 0 for v in stats.values()):
        db.commit()
        logger.info("Cleaned up digests: %s", stats)

    _cleanup_vector_index()
    return stats


def _cleanup_vector_index() -> dict:
    """清理向量索引中的过期数据。"""
    try:
        from domain.memory.memory.recall.vector import _get_db as _get_vec_db
    except ImportError:
        return {}
    
    # 先获取memory_layers中所有有效的period
    layers_db = _get_db()
    valid_periods = {}
    for layer in ["session", "day", "week"]:
        rows = layers_db.execute("SELECT period FROM memory_layers WHERE layer=?", (layer,)).fetchall()
        valid_periods[layer] = set(r[0] for r in rows)
    layers_db.close()
    
    vec_db = _get_vec_db()
    stats = {}
    
    try:
        # 清理conversation按时间
        cutoff = time.time() - _CONVERSATION_RETENTION_HOURS * 3600
        result = vec_db.execute("DELETE FROM chunks WHERE source=? AND created_at < ?", ("conversation", cutoff))
        stats["conversation"] = result.rowcount
        
        # 清理digest的orphan记录（不在memory_layers中的）
        for layer in ["session", "day", "week"]:
            source = "digest_" + layer
            rows = vec_db.execute("SELECT chunk_hash FROM chunks WHERE source=?", (source,)).fetchall()
            deleted = 0
            for row in rows:
                parts = row[0].split(":", 1)
                if len(parts) == 2:
                    period = parts[1]
                    if period not in valid_periods[layer]:
                        vec_db.execute("DELETE FROM chunks WHERE chunk_hash=?", (row[0],))
                        deleted += 1
            if deleted > 0:
                stats[source] = deleted
        
        if any(v > 0 for v in stats.values()):
            vec_db.commit()
            logger.info("Cleaned up vector index: %s", stats)
    finally:
        vec_db.close()
    
    return stats


def cleanup_all() -> dict:
    """手动触发完整清理。"""
    db = _get_db()
    try:
        stats = _cleanup_old_digests(db)
        stats.update(_cleanup_vector_index())
        return stats
    finally:
        db.close()


__all__ = [
    "cleanup_all",
    "consolidate_after_session",
    "backfill_existing_sessions",
    "recall_memories",
    "recall_session",
    "_get_db",
    "index_conversations",
    "backfill_conversations",
    # Segment narrative APIs
    "load_segment_narrative",
    "_lazy_generate_segment_narrative",
    "update_entity_index_from_narrative",
]