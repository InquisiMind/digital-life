"""L4 调度器 — 事件驱动唤醒入口。

被 cron/scheduler.py 的 tick() 调用：
  cron tick → 检查唤醒事件 → wake_digital_life(affair_id, reason) → 启动 agent

不再有"心跳"概念。唤醒完全由事件驱动。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from typing import Any


# NOTE: 原本从 wakeup_policy 引入 next_retry_delay_minutes / retry_intent_meta，
# 用于 _rollback_to_blocked 里基于 WaitIntent 的「失败指数退避」。
# 该机制已废弃——失败退避改为事件自驱（events.delay_pending_events），
# 闹钟不再为 failure recovery 服务（设计文档 5.5 / 22.1）。
import importlib
from domain.lifecycle.heartbeat import build_wake_prompt
from domain.lifecycle.runtime_context import set_current_affair
from domain.lifecycle.affairs.runtime import (
    init_db, clear_wait_intent, update_affair, record_heartbeat,
    get_affair,
)
from domain.lifecycle.state_machine import AffairStatus
from domain.lifecycle.wake import (
    L4_TASK_TOOLSETS,
    L4_TOOLSETS,
    enabled_toolsets_for_reason,
    make_wake_session_id,
    make_wake_session_log_filename,
)

logger = logging.getLogger("digital_life.lifecycle.scheduler")

# Per-instance concurrency guards — each instance can wake independently.
_wake_locks: dict[str, threading.Lock] = {}
_wake_in_progress: dict[str, float] = {}  # instance_id -> wake 开始时间(unix epoch)
# 僵尸 wake 自动释放阈值。历史 BUG: alpha wake_seq=223 卡 8 小时 几十个 LLM 调用都
# 超时 + 退避,lock 一直没释放,后续 8 小时所有 wake 全部 skipped_concurrent。
# 超过此阈值的 wake 视为僵尸,允许新 wake 抢占 lock。
WAKE_ZOMBIE_SECONDS = float(os.environ.get("DIGITAL_LIFE_WAKE_ZOMBIE_SECONDS", "600"))  # 10 分钟

# 短间隔唤醒延续：记录每个实例上一轮 session 的结束时间。
# 上轮结束 <15min 内的下一次唤醒复用同一 session（接上对话，而非新建）。
_last_session_end: dict[str, dict] = {}  # instance_id → {"session_id": str, "at": datetime}
CONTINUATION_WINDOW_S = 15 * 60

# 接续时 prev_history 的时间折叠阈值：同 session 的历史 segment（= 历史 wake）
# 若其最后一条消息距「本次 wake 开始」超过此 gap，原文折叠为摘要后回灌，
# 不再全量回放——避免长期接续的 session 把 6 小时前的原文一遍遍塞进 LLM。
SEGMENT_GAP_COMPRESS_S = 30 * 60

# 实例级连续 wake 失败计数 —— 偶发超时是常态（GLM 偶尔抽风），不报健康异常；
# 持续失败到阈值才写 flow_event error 事件，让健康检查（system_routes 的
# health_state）亮 error 并显示原因。成功一次即清零（健康自动恢复）。
WAKE_FAILURE_HEALTH_THRESHOLD = 3
_consecutive_wake_failures: dict[str, int] = {}  # instance_id → 连续失败次数


def _record_wake_health_failure(instance_id: str, error_text: str, *, is_429: bool) -> None:
    """连续 wake 失败到阈值 → 写一条 flow_event error，触发实例健康变 error。

    设计要点：
    - GLM 偶发超时/429 是常态，单次失败不当健康异常兇（避免频繁误报）；
    - 达阈值才报，让健康检查能看到"持续故障"；
    - 429（配额耗尽）单独标注，因为这才是真正需要人介入的；
    - 写完即重置计数，避免每轮重复写多条（最新一条反映最新错误即可）。
    """
    key = instance_id or "_global"
    n = _consecutive_wake_failures.get(key, 0) + 1
    _consecutive_wake_failures[key] = n
    if n < WAKE_FAILURE_HEALTH_THRESHOLD:
        return
    _consecutive_wake_failures[key] = 0  # 重置，避免重复刷 error 事件

    reason = "429_token_quota_exhausted" if is_429 else "agent_failed"
    summary = (
        f"GLM API 持续失败（连续 {n} 次，最近: {reason}）：{error_text[:160]}"
        if is_429 or "timeout" in str(error_text).lower() or "timed out" in str(error_text).lower()
        else f"实例连续 {n} 次 wake 失败（{reason}）：{error_text[:160]}"
    )
    try:
        from domain.flow_event_log.core.events import FlowEvent, new_flow_event_id
        from infrastructure.config import get_runtime_state_db_path
        from infrastructure.persistence.repositories import SQLiteFlowEventLogRepository
        repo = SQLiteFlowEventLogRepository(get_runtime_state_db_path())
        repo.append_event(FlowEvent(
            run_id=f"health_{key}",
            id=new_flow_event_id(),
            type="WakeHealthFailure",
            layer="orchestration",
            source="scheduler.wake",
            employee_id=instance_id,
            summary=summary,
            severity="error",
            payload={"reason": reason, "consecutive": n, "error": error_text[:500]},
        ))
        logger.warning("Health alert written: %s", summary)
    except Exception as exc:
        logger.debug("Failed to record wake health failure: %s", exc)


def _clear_wake_health_failure(instance_id: str) -> None:
    """成功 wake 一次即清零连续失败计数 —— 复用 system_routes 第 350 行的
    "最近一次成功 turn = 自动恢复" 机制，无需额外处理 error 事件清除。"""
    _consecutive_wake_failures.pop(instance_id or "_global", None)



def _get_instance_lock(instance_id: str) -> threading.Lock:
    if instance_id not in _wake_locks:
        _wake_locks[instance_id] = threading.Lock()
    return _wake_locks[instance_id]


def _is_wake_in_progress(instance_id: str) -> bool:
    return bool(_wake_in_progress.get(instance_id, 0))


def _check_continuation(instance_id: str):
    """上轮 session 结束 <15min → 返回可复用的 session_id，否则 None。

    先查内存 _last_session_end，丢失时（进程重启）回退到 DB 查询 sessions 表。
    """
    from domain.lifecycle.clock import now_dt
    prev = _last_session_end.get(instance_id or "")
    if prev:
        try:
            if (now_dt() - prev["at"]).total_seconds() <= CONTINUATION_WINDOW_S:
                return prev["session_id"]
        except Exception:
            pass

    # 内存状态丢失（如进程重启），回退到 DB 查询最近结束的 session
    try:
        from infrastructure.ai.session_db import SessionDB
        db = SessionDB()
        row = db._conn.execute(
            "SELECT id, ended_at FROM sessions WHERE ended_at IS NOT NULL ORDER BY ended_at DESC LIMIT 1"
        ).fetchone()
        if row:
            from datetime import datetime, timezone
            ended_dt = datetime.fromtimestamp(row["ended_at"], tz=timezone.utc)
            if (now_dt() - ended_dt).total_seconds() <= CONTINUATION_WINDOW_S:
                logger.debug("Continuation session recovered from DB: %s", row["id"])
                _last_session_end[instance_id or ""] = {"session_id": row["id"], "at": ended_dt}
                return row["id"]
    except Exception:
        pass
    return None


def _enabled_toolsets_for_reason(reason: str) -> list[str]:
    return enabled_toolsets_for_reason(reason)


def _load_prior_messages(session_db, session_id: str) -> list:
    """把上轮 session 的 DB 消息还原成 API message 格式。"""
    out: list[dict] = []
    for m in session_db.get_messages(session_id):
        role = m["role"]
        if role == "tool":
            out.append({"role": "tool", "tool_call_id": m.get("tool_call_id"),
                        "name": m.get("tool_name"), "content": m.get("content") or ""})
        elif role == "assistant":
            item = {"role": "assistant", "content": m.get("content") or ""}
            if m.get("tool_calls"):
                item["tool_calls"] = m["tool_calls"]
            out.append(item)
        elif role == "user":
            out.append({"role": "user", "content": m.get("content") or ""})
        else:
            out.append({"role": role, "content": m.get("content") or ""})
    return out


def _segment_gap_end_timestamp(messages: list[dict]) -> float:
    """段内最后一条带 timestamp 的消息时间，用于 gap 判定。"""
    for m in reversed(messages):
        ts = m.get("timestamp")
        if ts is not None:
            try:
                return float(ts)
            except (TypeError, ValueError):
                continue
    return 0.0


def _segment_gap_start_timestamp(messages: list[dict]) -> float:
    """段内最早的消息时间戳，用于段间排序。

    读取侧防御：即便写盘侧 segment_index 出错（曾经有过"读后写整体落到 -1 段"的
    bug），段间排序也按真实时间正确，而不是按 segment_index 数字序。0 表示无时间戳。
    """
    for m in messages:
        ts = m.get("timestamp")
        if ts is not None:
            try:
                return float(ts)
            except (TypeError, ValueError):
                continue
    return 0.0


def _summarize_segment(segment: list[dict], session_db, session_id: str, seg_idx: int) -> list[dict]:
    """把一个段的原文折叠成一条摘要消息（三级降级，保证不丢信息也不抛异常）。

    1) segment narrative（LLM/规则化叙事）—— 优先，最贴合「段」语义
    2) session 级 digest 头（规则化、必出）—— 兜底最近经历
    3) 段首尾消息（_shrink_segment 风格）—— 最差也不丢全部

    返回的 list 可直接 extend 进 prev_history。
    """
    try:
        body = ""
        # 1) segment narrative
        try:
            from domain.memory.memory.summaries.consolidation_runtime import (
                load_segment_narrative,
                _lazy_generate_segment_narrative,
            )
            narrative = load_segment_narrative(session_id, seg_idx)
            if not narrative and session_db:
                narrative = _lazy_generate_segment_narrative(session_db, session_id, seg_idx)
            if narrative:
                body = narrative
        except Exception as e:
            logger.debug("segment narrative unavailable for %s#%d: %s", session_id[:20], seg_idx, e)

        # 提取段时间范围做标题
        time_range = ""
        ts_end = _segment_gap_end_timestamp(segment)
        ts_start = _segment_gap_start_timestamp(segment)
        if ts_start and ts_end:
            import time as _time
            time_range = "{0}-{1}".format(
                _time.strftime("%m-%d %H:%M", _time.localtime(ts_start)),
                _time.strftime("%H:%M", _time.localtime(ts_end)),
            )

        if body:
            return [{
                "role": "user",
                "content": f"[历史回顾 · 非新事件 · {time_range}]\n{body}\n[/历史回顾]",
                "_sys_tool": "segment_recap",
            }]

        # 2) 无 narrative → 段首尾兜底（不丢全部，但显著瘦身）
        if len(segment) <= 4:
            return _load_prior_messages_segment(segment)
        head = _load_prior_messages_segment(segment[:2])
        tail = _load_prior_messages_segment(segment[-2:])
        return head + [{
            "role": "user",
            "content": "[…此段更早内容已折叠…]",
            "_sys_tool": "segment_recap",
        }] + tail
    except Exception as e:
        # 绝不抛 —— 降级为段首尾
        logger.debug("segment summarization failed for %s#%d, keeping verbatim: %s",
                     session_id[:20], seg_idx, e)
        return _load_prior_messages_segment(segment)


def _segment_gap_start_timestamp(messages: list[dict]) -> float:
    """段内第一条带 timestamp 的消息时间。"""
    for m in messages:
        ts = m.get("timestamp")
        if ts is not None:
            try:
                return float(ts)
            except (TypeError, ValueError):
                continue
    return 0.0


def _load_prior_messages_segment(segment: list[dict]) -> list[dict]:
    """把单个段的 DB message 还原成 API message 格式（_load_prior_messages 的 per-segment 版）。"""
    out: list[dict] = []
    for m in segment:
        role = m.get("role")
        if role == "tool":
            out.append({"role": "tool", "tool_call_id": m.get("tool_call_id"),
                        "name": m.get("tool_name"), "content": m.get("content") or ""})
        elif role == "assistant":
            item = {"role": "assistant", "content": m.get("content") or ""}
            if m.get("tool_calls"):
                item["tool_calls"] = m["tool_calls"]
            out.append(item)
        elif role == "user":
            out.append({"role": "user", "content": m.get("content") or ""})
        else:
            out.append({"role": role, "content": m.get("content") or ""})
    return out


def _load_prior_messages_with_compression(session_db, session_id: str, now_ts: float) -> list:
    """接续时拉 prev_history，并按「段(wake) gap」做时间折叠。

    segment（= wake）的最后一条消息若距 now_ts 超过 SEGMENT_GAP_COMPRESS_S，
    原文折叠为摘要；否则保留原文。当前正在跑的 wake（最后一段）永不折叠。

    任何环节失败 → 退回 _load_prior_messages 全量原文，绝不抛异常（热路径）。
    """
    try:
        all_messages = session_db.get_messages(session_id)
        if not all_messages:
            return []

        # 按 segment_index 分组（保留时间序）。message dict 自带 segment_index。
        segments_map: dict[int, list[dict]] = {}
        for m in all_messages:
            try:
                idx = int(m.get("segment_index") or 0)
            except (TypeError, ValueError):
                idx = 0
            segments_map.setdefault(idx, []).append(m)

        if len(segments_map) <= 1:
            # 只有一段（首次接续），无需折叠
            return _load_prior_messages(session_db, session_id)

        # 段间按"每段最早消息时间戳"排序——而不是按 segment_index 数字序。
        # 这是读取侧防御：即便写盘将来再出 bug 让 segment_index 错乱，
        # 段之间依然按真实时间因果序排列。（旧代码 sorted(seg_indices)
        # 99% 情况下正确，但因为段号乱给出的 -1 段被排到 0 段之前的 case
        # 直接破坏了因果序。）
        seg_indices = sorted(
            segments_map.keys(),
            key=lambda idx: _segment_gap_start_timestamp(segments_map[idx]),
        )

        out: list[dict] = []
        folded = 0
        for idx in seg_indices:
            seg = segments_map[idx]
            # get_messages 返回的全是已落库的历史段（本轮 wake 即将产生的新
            # message 尚未 append，不在列表里），所以无需 skip 当前段。
            seg_end_ts = _segment_gap_end_timestamp(seg)
            gap = now_ts - seg_end_ts if seg_end_ts else 0.0
            if gap > SEGMENT_GAP_COMPRESS_S:
                out.extend(_summarize_segment(seg, session_db, session_id, idx))
                folded += 1
            else:
                out.extend(_load_prior_messages_segment(seg))

        if folded:
            logger.info(
                "SEGMENT_GAP_FOLD session=%s folded=%d/%d (gap>%ds)",
                session_id[:20], folded, len(seg_indices), SEGMENT_GAP_COMPRESS_S,
            )
        return out
    except Exception as e:
        logger.debug("compressed prev_history failed, fallback to verbatim: %s", e)
        try:
            return _load_prior_messages(session_db, session_id)
        except Exception:
            return []


def _load_prev_session_summary(session_db, current_session_id: str) -> list:
    """加载上下文记忆层：最近经历 + 上次休息前留给自己的思绪。

    不直接塞入上一轮完整原始对话，避免 token 膨胀和低价值重复。
    """
    result = []

    # Layer 1: 你的最近经历 — LLM 叙事摘要 + digest 头部信息
    try:
        from domain.memory.memory.summaries.consolidation_runtime import _get_db
        db = _get_db()
        rows = db.execute(
            "SELECT llm_summary, digest FROM memory_layers WHERE layer='session' "
            "ORDER BY start_time DESC LIMIT 3"
        ).fetchall()
        db.close()
        if rows:
            lines = ["[你的最近经历]\n"]
            for r in rows:
                digest = (r["digest"] or "").strip()
                summary = (r["llm_summary"] or "").strip()

                # Extract header from digest: [MM/DD HH:MM] kind, Xs, Ymsgs
                header = _parse_digest_header(digest)

                # Body: prefer narrative llm_summary; fall back to digest
                body = summary or digest or ""
                if not body:
                    continue

                lines.append(f"── {header} ──\n{body}")

            if len(lines) > 1:
                lines.append("[/你的最近经历]")
                result.append({
                    "role": "user",
                    "content": "\n\n".join(lines),
                    "_sys_tool": "session_digest",
                })
    except Exception as e:
        logger.debug("Recent experience fallback: %s", e)

    # Layer 2: 上次休息前留给自己的思绪 — CONSCIOUSNESS 最后 1 条，主观"写给自己的"
    try:
        from domain.memory.memory.consciousness.runtime import read_last_thought
        last = read_last_thought()
        if last:
            _STATUS_TAGS = ("trading_wait", "system_wait", "final_status")
            if not any(f"[{tag}]" in last for tag in _STATUS_TAGS):
                result.append({
                    "role": "user",
                    "content": f"[上次休息前留给自己的思绪]\n{last.strip()}\n[/上次休息前留给自己的思绪]",
                    "_sys_tool": "consciousness",
                })
    except Exception as e:
        logger.debug("Self thought fallback: %s", e)

    return result


def _parse_digest_header(digest: str) -> str:
    """Extract a human-readable header from digest first line.

    Digest format: [MM/DD HH:MM] event_kind, Xs, Ymsgs
    Returns: "MM/DD HH:MM · event_kind · Xs"

    Falls back to a truncated digest prefix if parsing fails.
    """
    import re
    if not digest:
        return ""
    first_line = digest.split("\n")[0].strip()
    m = re.match(r"\[([^\]]+)\]\s*(\w+),\s*(\d+s)", first_line)
    if m:
        ts = m.group(1)
        kind = m.group(2)
        duration = m.group(3)
        # Humanize kind names
        kind_cn = {
            "message": "人类消息",
            "group_message": "群聊消息",
            "timer": "闹钟唤醒",
            "routine": "作息事件",
            "initiative": "主动探索",
            "awaiting_reply": "等待回复",
            "task_reminder": "任务提醒",
            "task_momentum": "任务惯性",
            "birth": "初次醒来",
            "vital_threshold": "精力预警",
            "nurture_energy": "加鸡腿",
            "self_iteration": "自我迭代",
        }.get(kind, kind)
        # Humanize duration
        try:
            secs = int(duration.rstrip("s"))
            if secs < 60:
                dur_str = f"{secs}s"
            elif secs < 3600:
                dur_str = f"{secs // 60}m{secs % 60}s"
            else:
                h = secs // 3600
                m = (secs % 3600) // 60
                dur_str = f"{h}h{m}m"
        except ValueError:
            dur_str = duration
        return f"{ts} · {kind_cn} · {dur_str}"
    # Fallback: first 60 chars of digest
    return first_line[:60]


def wake_digital_life(
    affair_id: str,
    reason: str,
    extra: str = "",
    pending_events: Optional[List[Dict[str, Any]]] = None,
) -> dict:
    """唤醒 digital life：设置状态 + 构建 prompt + 启动 agent。

    Args:
        affair_id: 生命 affair ID
        reason: 唤醒原因 (timer/natural/message/vital_threshold/boredom)
        extra: 额外提示信息
        pending_events: 当前所有待处理事件列表
    """
    global _wake_in_progress

    # 优先取 ContextVar（fan_out 跨实例生成事件时已 set_current_instance_id）；
    # fallback 到 env。历史上只用 os.environ，导致 fan_out 调 wake_digital_life 时
    # 持有的是 sender 进程的 env（而非 target），inst_lock 锁错对象、信号也写错队列。
    try:
        from infrastructure.config import get_app_instance_id
        instance_id = get_app_instance_id() or ""
    except Exception:
        instance_id = os.environ.get("DIGITAL_LIFE_INSTANCE_ID", "")
    inst_lock = _get_instance_lock(instance_id)

    if not inst_lock.acquire(blocking=False):
        # 僵尸 wake 检测:已有 wake 跑了 WAKE_ZOMBIE_SECONDS 还没释放 → 抢占 lock。
        # 防止一次 GLM 异常拖住整实例 N 小时(历史 alpha 8 小时卡死案例)。
        started_at = _wake_in_progress.get(instance_id, 0)
        now = time.time()
        if started_at and (now - started_at) > WAKE_ZOMBIE_SECONDS:
            logger.warning(
                "wake_digital_life: existing wake for %s appears stuck (%.0fs > %ds) — hijacking lock",
                instance_id, now - started_at, int(WAKE_ZOMBIE_SECONDS),
            )
            try:
                inst_lock.release()
            except RuntimeError:
                pass  # 已被其他持有者释放,忽略
        else:
            logger.warning("wake_digital_life skipped — another wake is in progress for %s", instance_id)
            return {"woke": False, "reason": "skipped_concurrent"}

    _wake_in_progress[instance_id] = time.time()
    logger.info(
        "WAKE_LOCK_ACQUIRED instance=%s affair=%s reason=%s zombie_seconds=%d",
        instance_id, affair_id[:8], reason, int(WAKE_ZOMBIE_SECONDS),
    )
    _wake_result_ok: bool | None = None
    try:
        result = _wake_digital_life_inner(affair_id, reason, extra, instance_id=instance_id, pending_events=pending_events)
        _wake_result_ok = not (isinstance(result, dict) and result.get("error"))
        return result
    except Exception as exc:
        _wake_result_ok = False
        raise
    finally:
        _wake_in_progress.pop(instance_id, None)
        inst_lock.release()
        logger.info(
            "WAKE_LOCK_RELEASED instance=%s affair=%s result_ok=%s",
            instance_id, affair_id[:8], _wake_result_ok,
        )
        # Fast-path: wake 刚释放 lock,可能有被 skipped_concurrent 的 immediate 消息
        # 留在 due 队列里。立刻扫一次 due 队列把延迟从"60s cron tick"降到"几秒"。
        # 软消息已被 debounce 累积成 30s batch event,这里只会 pick up 立刻到期的。
        # 防止 recursive wake 无限循环:hover guard 一次扫完就清,不再触发新 fast-path。
        #
        # ⚠️ 如果本轮 wake 失败（agent crash / 429），不走 fast-path。
        # 失败意味着 _rollback_to_blocked 已设了退避 timer，fast-path 只会
        # 立即重新唤醒 → 再次失败 → 无限循环（历史 6/14 zero 疯狂重试事件）。
        if _wake_result_ok is not True:
            logger.debug("wake fast-path skipped (wake failed) for %s", instance_id)
        else:
            try:
                from domain.lifecycle.events import pop_due_events
                due_after = pop_due_events(limit=3)
                if due_after:
                    logger.info("wake fast-path: %d due events after release, re-waking for %s",
                                len(due_after), instance_id)
                    # instance_id 通过 ContextVar(已 set) 传递,无需显式 kwarg
                    wake_digital_life(affair_id, reason=due_after[0].get("kind", "timer"),
                                      pending_events=due_after)
            except Exception as exc:
                logger.debug("wake fast-path scan failed: %s", exc)


def _render_workspace_intro(instance_id: str, my_projects: list | None = None) -> str:
    """Render the "your workspace" prompt section.

    精简版：只给根路径 + 三层区分级 + 实际项目列表。详细子目录模型用 terminal
    ls 自查（实测模型已熟练使用 projects/<pid>/... 路径）。每个项目的代码/文档/
    记忆子目录结构（code/tools/, docs/, memory/ 等）历史版本给过描述，但模型日常
    写文件靠 ls，无需每次重打。
    """
    try:
        from domain.project.loader import load_all_projects
        from infrastructure.config import get_project_root
        all_projects = load_all_projects()
        _repo_root = str(get_project_root())
        mine = []
        if not my_projects:
            for pid, cfg in all_projects.items():
                if cfg.status != "active":
                    continue
                if cfg.get_position_for_instance(instance_id):
                    mine.append((pid, cfg))
        else:
            mine = my_projects if my_projects and isinstance(my_projects[0], tuple) else []

        proj_lines = "\n".join(f"- projects/{pid}/（{cfg.name}）" for pid, cfg in mine)
        if not proj_lines:
            proj_lines = "（你还没参与任何 active 项目）"

        return (
            "### 你的工作空间\n\n"
            f"**项目根**：`{_repo_root}`\n"
            f"**默认工作目录**：`apps/{instance_id[:8]}…/workspace/`"
            "（terminal/execute_code 默认 cwd；写文件、跑脚本请基于此，"
            "你的产出物应在这里）\n\n"
            "**三层空间**（动手前想「该放哪一层」）：\n"
            f"- 个人：`apps/{instance_id[:8]}…/` (workspace/tools/skills/)\n"
            "- 项目：写到这里对应的 `projects/<pid>/`（拆成 code/docs/memory/skills）\n"
            "- 共享：`shared/` (capabilities/tools/skills 跨项目通用)\n\n"
            f"**你参与的项目**：\n{proj_lines}\n\n"
            "**约束**：写出每个产出物前先问「它在 apps/<iid>/workspace 里吗？」"
            "——你的产出物应留在 sandbox 内。"
            "注册新工具/技能走 `register_tool`/`register_skill`；"
            "不要新建顶层目录、不要写 domain/infrastructure/config/ 源码。"
            "只读参考：`docs/architecture/`、`CLAUDE.md`。"
        )
    except Exception:
        return ""


def _wake_digital_life_inner(
    affair_id: str,
    reason: str,
    extra: str = "",
    instance_id: str = "",
    pending_events: Optional[List[Dict[str, Any]]] = None,
) -> dict:
    # 确保环境变量已加载（后台线程可能晚于主进程加载）
    try:
        from infrastructure.config import get_runtime_env_path, get_runtime_home
        from infrastructure.ai import load_runtime_dotenv

        load_runtime_dotenv(runtime_home=get_runtime_home(), project_env=get_runtime_env_path())
    except Exception:
        pass

    # 设置线程级实例上下文，防止 cron 循环切换 env var 时污染 agent 线程的 DB 路径
    _ctx_token = None
    if instance_id:
        try:
            from infrastructure.config import set_current_instance_id, reset_current_instance_id
            _ctx_token = set_current_instance_id(instance_id)
        except Exception:
            pass

    try:
        return _wake_digital_life_inner_safe(affair_id, reason, extra, pending_events, instance_id=instance_id)
    finally:
        if _ctx_token is not None:
            try:
                from infrastructure.config import reset_current_instance_id
                reset_current_instance_id(_ctx_token)
            except Exception:
                pass


def _wake_digital_life_inner_safe(
    affair_id: str,
    reason: str,
    extra: str = "",
    pending_events: Optional[List[Dict[str, Any]]] = None,
    *,
    instance_id: str = "",
) -> dict:
    # ⚠ 诊断日志[wake入口]：群消息穿透排查用。零行为变更。
    # 这里能看到 wake 收到的 pending_events 长度（实例下 _route_to_life / tick / fast-path 不同源）
    _pct = len(pending_events) if isinstance(pending_events, list) else None
    logger.info(
        "WAKE_BEGIN affair=%r reason=%s instance=%s pending_events=%s "
        "pending_event_ids=%s",
        affair_id, reason, instance_id,
        _pct if _pct is not None else "None",
        [e.get("event_id") for e in pending_events] if isinstance(pending_events, list) else None,
    )

    init_db()

    # 重置 exec 调用计数器
    try:
        from interfaces.tools.code_execution_tool import reset_exec_counter
        reset_exec_counter()
    except Exception:
        pass

    # 设置群聊回复上下文 + conversation_id 上下文（如果是因为消息唤醒）
    try:
        import importlib
        action_tools = importlib.import_module("interfaces.tools.action_tools")
        set_group_reply_context = action_tools.set_group_reply_context
        set_dm_reply_context = action_tools.set_dm_reply_context
        # 清掉 reply context，避免上轮 wake 残留污染本轮
        # set_*_reply_context("") 通过 _REPLY_CONTEXT[iid][*]="" 显式标记"无"
        # 读取层用 .get() or _*_REPLY_CHAT_ID，空串会被 fallback 吃掉，
        # 因此同时把全局变量也清空（per-instance 隔离在另一个 dict key）
        if reason in ("message", "group_message") and pending_events:
            for ev in pending_events:
                if ev.get("kind") in ("message", "group_message"):
                    chat_id = ev.get("payload", {}).get("chat_id", "")
                    if chat_id:
                        if reason == "group_message":
                            set_group_reply_context(chat_id)
                            set_dm_reply_context("")  # 清 DM 污染
                            action_tools._DM_REPLY_CHAT_ID = None
                            logger.info("Set group reply context: chat_id=%s (dm cleared)", chat_id)
                        else:
                            set_dm_reply_context(chat_id)
                            set_group_reply_context("")  # 清 group 污染
                            action_tools._GROUP_REPLY_CHAT_ID = None
                            logger.info("Set DM reply context: chat_id=%s (group cleared)", chat_id)
                        try:
                            from domain.lifecycle.runtime_context import set_current_conversation_id
                            set_current_conversation_id(chat_id)
                            logger.debug("Set conversation_id context: %s", chat_id)
                        except Exception:
                            pass
                    break
    except Exception:
        pass

    # 三态收条-态 2: wake 真正启动 → 撤所有 ✅ 加 🤔
    # 不在 emit_event 后立即调(那时还没真跑 LLM,切换太快),在 wake_inner 入口调。
    if reason in ("message", "group_message"):
        try:
            from application.ingress.reaction_state import mark_all_processing_sync
            mark_all_processing_sync()
        except Exception:
            pass

    # 记录心跳（维持时间戳，供自然醒等逻辑参考）
    try:
        record_heartbeat(
            woke_affair_id=affair_id,
            notes=f"wake_reason={reason}",
        )
    except Exception as e:
        logger.debug("Failed to record heartbeat: %s", e)

    # 计算睡眠时长，传给 prompt 构建器区分"深度睡眠醒来"和"短暂休息继续"
    sleep_minutes = 0
    previous_wait_intent = None
    try:
        from domain.lifecycle.affairs.runtime import get_wait_intent
        _intent = get_wait_intent(affair_id)
        previous_wait_intent = _intent
        if _intent and _intent.blocked_at:
            from domain.lifecycle.clock import now_dt, parse_iso
            elapsed = (now_dt() - parse_iso(_intent.blocked_at)).total_seconds()
            sleep_minutes = max(0, elapsed / 60)
    except Exception:
        pass

    # 构建唤醒 prompt（先构建，不更新状态——agent 失败时不留副作用）
    action_prompt, ref_context, covered_event_ids, task_prompt = build_wake_prompt(reason, extra=extra, pending_events=pending_events,
                                    sleep_minutes=sleep_minutes, status="BLOCKED")
    _pe_n = len(pending_events) if isinstance(pending_events, list) else 0
    _branch = (
        "single_event_inline" if _pe_n == 1
        else ("multi_event_list" if _pe_n >= 2 else "ZERO_EVENTS_pseudo_multi")
    )
    logger.info(
        "WAKE_PROMPT_BUILT reason=%s sleep_minutes=%.1f pending_events=%d "
        "branch=%s covered_event_ids=%s action_prompt_head=%r",
        reason, sleep_minutes, _pe_n, _branch, list(covered_event_ids),
        (action_prompt[:100] + "…") if len(action_prompt) > 100 else action_prompt,
    )
    logger.info("Waking digital life: reason=%s, affair=%s", reason, affair_id)

    # 设置事务上下文
    set_current_affair(affair_id)
    try:
        from domain.lifecycle.runtime_context import set_current_wake_reason
        set_current_wake_reason(reason)
    except Exception:
        pass

    # 设置当前事件来源 chat_id（消息事件 payload 携带；非消息事件为空）
    # agent._append_message 自动读此 ContextVar 并写入 messages.chat_id 列
    # express_to_human 在模型未显式指定 chat_id 时用它做 fallback
    _wake_chat_id = ""
    if pending_events:
        for ev in pending_events:
            payload = ev.get("payload") or {}
            # Primary source: explicit chat_id field (group_message / message).
            _wake_chat_id = payload.get("chat_id") or ""
            # Fallback: awaiting_reply 类无 chat_id 但有 channel
            # 形如 "feishu:dm:oc_xxx" / "lark:group:oc_xxx" → 抽尾部 oc_xxx
            # （同时认现用 feishu: 与历史存量 lark: 前缀）
            if not _wake_chat_id:
                chan = payload.get("channel") or ""
                if chan.startswith(("feishu:", "lark:")):
                    parts = chan.split(":", 2)
                    if len(parts) >= 3 and parts[2].startswith("oc_"):
                        _wake_chat_id = parts[2]
            if _wake_chat_id:
                break
    _chat_token = None
    try:
        from domain.lifecycle.runtime_context import set_current_event_chat_id, reset_current_event_chat_id
        _chat_token = set_current_event_chat_id(_wake_chat_id)
    except Exception:
        pass

    # 启动 AIAgent
    summary = {
        "woke": True,
        "reason": reason,
        "affair_id": affair_id,
    }
    _session_db = None
    session_id = ""
    try:
        from infrastructure.ai import AIAgent, SessionDB, load_runtime_config, parse_reasoning_effort, resolve_runtime_provider

        # load_runtime_config() 内部自动解析到当前实例的 apps/<id>/config/app.yaml
        # 不要传 get_runtime_config_path()——那是已废弃的 data/config.yaml 路径，
        # 实例配置迁移到 config/app.yaml 后该文件不再存在，传进去会让实例层被跳过、model 解析为空。
        try:
            _cfg = load_runtime_config()
        except Exception as e:
            logger.warning("Failed to load runtime config: %s", e)
            _cfg = {}

        model_cfg = _cfg.get("model", {})
        if isinstance(model_cfg, str):
            model = model_cfg
        elif isinstance(model_cfg, dict):
            model = model_cfg.get("name") or model_cfg.get("default") or ""
        else:
            model = ""

        # Provider 解析
        try:
            runtime = resolve_runtime_provider(
                requested=os.getenv("DIGITAL_LIFE_INFERENCE_PROVIDER"),
                config=_cfg,
            )
        except Exception as exc:
            logger.error("Failed to resolve runtime provider: %s", exc)
            summary["error"] = str(exc)
            _rollback_to_blocked(affair_id)
            return summary

        effort = str(_cfg.get("agent", {}).get("reasoning_effort", "")).strip()
        reasoning_config = parse_reasoning_effort(effort)

        # 来自实例 app.yaml 的 agent.max_turns（ConfigCenter「实例配置 → 任务策略」编辑）；
        # 没配置时用 999 给足空间，让长任务不被切断（保持与历史行为一致）。
        max_iterations = int(_cfg.get("agent", {}).get("max_turns") or 90)

        # 醒来即标记 last_activity_at（vital-refactor 后的字段拆分）。
        #
        # 旧版这里是一坨 hack：UPDATE vitals.updated_at + sync engine 内存。原因
        # 是 updated_at 一个字段身兼两职（recovery 锚 + initiative idle 锚）。重构
        # 后两者分开：updated_at 只由 tick/consume_energy/apply_nurture 推进
        # （真实事件）；last_activity_at 才是 wake-time 主动标记"刚活动"的锚。
        # engine 单例内存的 _last_activity_at 必须显式 sync 才能跟 DB 一致。
        try:
            from domain.vital import touch_activity
            touch_activity()
            from domain.vital.simulation import get_engine
            get_engine().sync_last_activity_at()
        except Exception:
            pass

        clear_wait_intent(affair_id)
        update_affair(affair_id, status=AffairStatus.RUNNING)
        logger.info(
            "AFFAIR_TRANSITION affair=%s instance=%s BLOCKED→RUNNING reason=%s",
            affair_id[:8], instance_id, reason,
        )

        # 事务 ID：延续（上轮结束 <15min）则复用上轮 session；否则新建。
        prev_sid = _check_continuation(instance_id)
        is_continuation = bool(prev_sid)
        session_id = prev_sid if is_continuation else make_wake_session_id(reason)
        logger.info(
            "WAKE_SESSION instance=%s session_id=%s is_continuation=%s",
            instance_id, session_id, is_continuation,
        )
        from infrastructure.config import set_current_session_id
        _session_id_token = set_current_session_id(session_id)

        # Auto-consume events that the wake prompt inlines or resolves.
        # heartbeat returns covered_event_ids: the events whose content was inlined
        # into the prompt. Consume them now; roll back on LLM failure.
        auto_consumed_eids: list[int] = []
        for eid in covered_event_ids:
            try:
                from domain.lifecycle.events import consume_event
                consume_event(eid, session_id=session_id)
                auto_consumed_eids.append(eid)
                logger.info(
                    "WAKE_AUTO_CONSUME event_id=%d session_id=%s caller=covered_event_ids",
                    eid, session_id,
                )
            except Exception:
                pass

        # Session DB
        try:
            _session_db = SessionDB()
        except Exception:
            pass

        # 人设：LIFE_PERSONA.md 作为 identity layer
        # 生命周期提示通过 ephemeral_system_prompt 注入（API-call 时追加）
        _l4_lifecycle = ""
        try:
            from domain.identity.system_prompts import L4_LIFECYCLE_PROMPT
            _l4_lifecycle = L4_LIFECYCLE_PROMPT
        except Exception:
            pass
        _persona = ""
        try:
            from domain.memory.context.selectors.persona import load_life_persona, MISSING_LIFE_PERSONA
            raw = load_life_persona(instance_id or None).strip()
            if raw and raw != MISSING_LIFE_PERSONA:
                _persona = raw
        except Exception:
            pass

        # 技能索引：按实例注册动态注入（插在 lifecycle 和 persona 之间）
        _skill_index = ""
        try:
            from interfaces.skills import render_skill_index
            _skill_index = render_skill_index(instance_id)
        except Exception:
            pass

        # 项目岗位上下文：把当前承担的岗位 + 协作伙伴清晰呈现，注入 persona 之后
        # ——「我是谁」必须含「我是某项目的某岗位」，否则人格与职责脱节。
        _role_positioning = ""
        _project_direction = ""  # 目标+论断+KPI 进度 — 让模型每次 wake 都看到自己往哪走
        try:
            from domain.project.loader import load_all_projects
            from infrastructure.config import get_instance_display_name
            my_iid = instance_id or ""
            roles_lines: list[str] = []
            direction_lines: list[str] = []
            for pid, cfg in load_all_projects().items():
                if cfg.status != "active":
                    continue
                my_pos = cfg.get_position_for_instance(my_iid)
                if not my_pos:
                    continue
                # 该项目其他岗位（我的协作者画像）
                partners_lines: list[str] = []
                for other_pos in cfg.positions:
                    if other_pos.id == my_pos.id:
                        continue
                    for asg in other_pos.assignees:
                        # 区分人类 vs 实例
                        if asg.startswith("human:"):
                            partner_label = "zhp"  # 目前只一个真人
                            partner_role = other_pos.name
                        else:
                            partner_label = get_instance_display_name(asg) or asg[:8]
                            partner_role = other_pos.name
                        partners_lines.append(f"  · {partner_label}({partner_role})")
                is_pm = "，**项目经理**" if cfg.manager == my_iid else ""
                resp = "／".join(my_pos.responsibilities) if my_pos.responsibilities else "执行所在岗位职责"
                head = f"💼 **{cfg.name}** 项目 — 担任 **{my_pos.name}**{is_pm}"
                detail = f"  核心职责: {resp}"
                partners_block = ""
                if partners_lines:
                    partners_block = "  协作者:\n" + "\n".join(partners_lines)
                roles_lines.append("\n".join([head, detail, partners_block]))

                # ── 项目方向段（仅 PM 或主担岗位才需要看到完整 thesis）──
                goal = cfg.goal or {}
                if goal:
                    dir_parts: list[str] = [f"### 🎯 {cfg.name} 项目方向"]
                    gs = goal.get("statement", "")
                    if gs:
                        dir_parts.append(f"**目标**：{gs}")
                    sd = goal.get("started_at", "")
                    dd = goal.get("deadline", "")
                    if sd and dd:
                        dir_parts.append(f"**周期**：{sd} → {dd}")
                    sc = goal.get("start_capital", "")
                    tc = goal.get("target_capital", "")
                    if sc and tc:
                        dir_parts.append(f"**起止资金**：{sc} → {tc}")
                    kpis = cfg.kpis or []
                    if kpis:
                        kpi_lines = ["**当前 KPI 进度**："]
                        for kpi in kpis:
                            name = kpi.get("name", "")
                            tgt = kpi.get("target") or kpi.get("limit") or kpi.get("constraint") or ""
                            sv = kpi.get("snapshot_value", "")
                            sv_at = kpi.get("snapshot_at", "")
                            if sv and sv_at:
                                kpi_lines.append(f"  · {name}{('('+tgt+')') if tgt else ''}: {sv} (截至 {sv_at})")
                            elif tgt:
                                kpi_lines.append(f"  · {name}: 目标 {tgt}")
                        dir_parts.append("\n".join(kpi_lines))
                    thesis_list = cfg.thesis or []
                    if thesis_list:
                        t_lines = ["**论断假设**（决定你做什么，可能过期需 review）："]
                        for idx, t in enumerate(thesis_list, 1):
                            stmt = t.get("statement", "")
                            conf = t.get("confidence", "")
                            ev_count = len(t.get("evidence") or [])
                            lr = t.get("last_reviewed", "")
                            t_lines.append(
                                f"{idx}. _{stmt}_ [{conf}信心]"
                                f"{f'，{ev_count} 条证据' if ev_count else ''}"
                                f"{f'，最后review：{lr}' if lr else ''}"
                            )
                        dir_parts.append("\n".join(t_lines))
                    rs = cfg.review_schedule or {}
                    if rs:
                        rs_parts = ["**反思节奏**："]
                        for key in ("daily_review_at", "weekly_review_at", "monthly_milestone_at"):
                            v = rs.get(key, "")
                            if v:
                                label_map = {
                                    "daily_review_at": "每日晚复盘",
                                    "weekly_review_at": "每周策略 review",
                                    "monthly_milestone_at": "每月里程碑",
                                }
                                rs_parts.append(f"  · {label_map.get(key, key)} @ {v}")
                        dir_parts.append("\n".join(rs_parts))
                    direction_lines.append("\n".join(dir_parts))
            if roles_lines:
                _role_positioning = (
                    "### 你承担的职责\n\n"
                    "下面是你目前主担的项目和岗位。每一段都该影响你的判断："
                    "作为策略师优先论断和规划，作为架构师优先抽象和复用，作为交易员优先风控和执行。"
                    "把岗位责任放在心上，与你的协作者分工推进。\n\n"
                    + "\n\n".join(roles_lines)
                )
            if direction_lines:
                _project_direction = "\n\n".join(direction_lines)
        except Exception:
            pass

        # 工作空间介绍（每次 wake 告诉模型它的"家"在哪、能写到哪里、参考源在哪）
        _workspace_intro = _render_workspace_intro(instance_id or "")

        # 5 段拼成有机整体：lifecycle（环境） → persona（自我意识） → 职责（自我定位加项目）
        #                      → 项目方向（目标+论断） → 工作空间（自我迭代环境）
        #                      → skill_index（方法）
        _full_system = "\n\n".join(
            p for p in [_l4_lifecycle, _persona, _role_positioning, _project_direction,
                        _workspace_intro, _skill_index] if p
        )

        # L4 需要精简工具集，避免模型被48个工具淹没而只发文字
        agent = AIAgent(
            model=model,
            api_key=runtime.get("api_key"),
            base_url=runtime.get("base_url"),
            provider=runtime.get("provider"),
            api_mode=runtime.get("api_mode"),
            max_iterations=max_iterations,
            reasoning_config=reasoning_config,
            quiet_mode=True,
            platform="l4_wake",
            session_id=session_id,
            session_db=_session_db,
            enabled_toolsets=_enabled_toolsets_for_reason(reason),
            skip_memory=True,
            instance_id=instance_id,
        )
        try:
            agent.session_log_file = agent.logs_dir / make_wake_session_log_filename(session_id)
        except Exception:
            pass

        # Start a new audit wake (dual-write alongside legacy session_db).
        # 把 _full_system(L4_LIFECYCLE_PROMPT + persona + role_positioning
        # + workspace + skill_index)全文直接放进 audit meta system_prompt_text,
        # 而非依赖 agent.py record_system_prompt(那个只在 system_message 非空时跑,
        # continuation session 走 system_for_agent=None 会跳过,导致前端"完整输入"
        # 只能走 persona_loader fallback,而后者只读 LIFE_PERSONA.md 文件不含 L4
        # 生命周期说明 → 用户看到"system prompt 没拼生命周期说明")。
        # 直接存全文:前端回放永远和 model 实际看到的一致。
        wake_ctx = None
        _wake_id_token = None
        try:
            from infrastructure.persistence.instance import get_audit
            from infrastructure.persistence.instance.wake_context import WakeContext
            audit_meta: dict[str, Any] = {
                "trigger_type": reason,
                "trigger_chat_id": _wake_chat_id or "",
                "model_id": model or "",
                "system_prompt_ref": f"instance:{instance_id}",
                "system_prompt_text": _full_system or "",
            }
            wake_ctx = WakeContext.start(get_audit(instance_id), meta=audit_meta)
            agent.audit_ctx = wake_ctx
            agent.wake_id = wake_ctx.wake_id
            # 把当前 wake_id 挂到 contextvar —— mid-session 注入靠它精确归属到本 wake，
            # 而不是靠 ended_at IS NULL 猜（失败 wake 的 ended_at 曾因异常被吞而留 NULL，
            # 导致下一条消息的注入错挂到上一组失败 wake 上）。
            from domain.lifecycle.runtime_context import set_current_wake_id
            _wake_id_token = set_current_wake_id(str(wake_ctx.wake_id))
            logger.info("WakeContext started: instance=%s wake_id=%d seq=%d",
                        instance_id, wake_ctx.wake_id, wake_ctx.wake_seq)
        except Exception:
            logger.debug("Failed to start WakeContext — audit DB unavailable, continuing without", exc_info=True)

        # Mark wake entity recall memories as presented so mid-session
        # _inject_entity_recall doesn't re-inject them.
        try:
            from domain.lifecycle.heartbeat import get_presented_memory_ids
            wake_recall_ids = get_presented_memory_ids()
            if wake_recall_ids:
                agent.mark_memories_presented(wake_recall_ids)
        except Exception:
            pass

        agent_result = {}
        agent_error = None
        # 仪表:agent.run_conversation 是否真正启动过。一旦启动过,
        # auto_consumed_eids 的事件就已经 inline 进 prompt 被模型看见过了,
        # 失败回退不该 unconsume —— 否则在 "agent 跑了 55 轮 LLM 然后崩溃"
        # 这种场景下,事件被夺回 → 下次 wake 又 pop 出来 → 模型又看到了 → 又不
        # 回应 → 又崩溃 → 又夺回 ... 形成事实上的死循环 (awaiting_reply
        # 事件 1421 在 19:09-19:34 之间被复活 2 次, akka 1107 / 1109 两次 wake
        # 都是它,实测)。
        _agent_started = False
        # prev_history 策略：
        #   - continuation: 续用上轮 messages 表内容（已经包含上次 wake 注入的 slow_ctx），不再重复注入
        #   - 新 session: 拼装 slow_ctx (digest/consciousness/ref_context) + task_board + chat_stream
        # 避免每次 wake 都重新注入整套 slow_ctx，导致 messages 表指数膨胀 + LLM API 超限。
        if is_continuation:
            # prev_history 拉 + 时间折叠：超 30min 的历史段折叠为摘要，避免长期接续
            # 的 session 把几小时前的原文一遍遍全量回放给 LLM。
            import time as _time
            prev_history = list(_load_prior_messages_with_compression(
                _session_db, session_id, _time.time()
            ))
            if not prev_history:
                is_continuation = False
                prev_history = []
                system_for_agent = _full_system or None
            else:
                system_for_agent = None
                logger.info("Continuing session %s (%d prior messages)", session_id, len(prev_history))
        else:
            prev_history = []
            system_for_agent = _full_system or None

        # 每次 wake 都注入 slow_ctx（保证信息最新）。
        # agent._convert_user_to_tool 在持久化时会 DELETE 同类旧条目（slow_ctx dedup），
        # 保证 messages 表里 digestive / consciousness / task_board / system_context
        # 每个 sys_tool 类型只留最新一份；chat_stream 不去重（不同 chat / 时间片段都保留）。
        slow_ctx = _load_prev_session_summary(_session_db, session_id)
        if ref_context:
            slow_ctx.insert(0, {"role": "user", "content": ref_context, "_sys_tool": "system_context"})
        prev_history.extend(slow_ctx)

        # social_context:oplastics contacts + 群 + 岗位，让模型决定回哪发哪
        try:
            from domain.social_context import render_social_context
            sc_body = render_social_context(instance_id).strip()
            if sc_body:
                prev_history.append({
                    "role": "user",
                    "content": sc_body,
                    "_sys_tool": "social_context",
                })
        except Exception as exc:
            logger.debug("social_context inject failed: %s", exc)

        # schedule: 近期日程 + 每日作息 + 精力恢复说明，让模型无需主动调 sense_schedule
        # 就能预判后续安排（每条闹钟带 id，方便 rest 时 reuse）。
        try:
            from domain.lifecycle.alarms import get_schedule_overview, format_schedule_for_human
            _sched_overview = get_schedule_overview(days_ahead=7)
            _sched_body = format_schedule_for_human(_sched_overview).strip()
            if _sched_body:
                prev_history.append({
                    "role": "user",
                    "content": _sched_body,
                    "_sys_tool": "schedule",
                })
        except Exception as exc:
            logger.debug("schedule inject failed: %s", exc)

        # task_skill: 当前 in_progress task 的 type → 对应 skill 注入 prompt
        # P2: task 带 type，type 有对应 skill 文件 → 读取内容注入
        try:
            from domain.todos import resolve_skill_for_current_task
            skill_text = resolve_skill_for_current_task().strip()
            if skill_text:
                prev_history.append({
                    "role": "user",
                    "content": f"## ── 当前任务执行方法论（来自 task.type → skill）──\n{skill_text}\n## ── /方法论 ──",
                    "_sys_tool": "task_skill",
                })
        except Exception as exc:
            logger.debug("task_skill inject failed: %s", exc)

        # my_context: 我的待办面板（单一渲染入口 domain.todos.board）
        try:
            from domain.todos.board import render_my_board
            from infrastructure.config import get_app_instance_id
            from domain.lifecycle import clock
            iid = get_app_instance_id() or ""
            text = render_my_board(iid, clock.now_dt()) if iid else ""
            if text:
                prev_history.append({
                    "role": "user",
                    "content": text,
                    "_sys_tool": "my_context",
                })
        except Exception as exc:
            logger.debug("my_context inject failed: %s", exc)

        # task_board 每次注入最新任务列表（agent 端会 dedup）
        if task_prompt:
            prev_history.append({
                "role": "user",
                "content": task_prompt,
                "_sys_tool": "task_board",
            })

        # chat_stream 每次拉近期对话流水（来自 var/conversations/chats.db 聚合库，
        # 含本实例 + 其他 bot 的发言 + 真人发言）
        #
        # Critical: 模型每次 wake 都应当看到群聊最近对话——不论是 timer /
        # awaiting_reply / initiative 触发。否则模型会"自我上下文脱钩"，发不
        # 相关的话、对已变的事实做出过时反应。
        #
        # Wake chat id 抽取顺序：
        #   1. triggering event payload.chat_id（group_message / message）
        #   2. triggering event payload.channel（awaiting_reply: 「lark:dm:oc_xxx」）
        #   3. fallback: 当前实例参与的项目 group_chat_id（任意 active project 第一个）
        if not _wake_chat_id:
            try:
                from domain.project.loader import load_all_projects
                from infrastructure.config import get_app_instance_id
                _me = get_app_instance_id() or ""
                for _pid, _pcfg in load_all_projects().items():
                    if _pcfg.status != "active":
                        continue
                    # 不接受任意 active project 的 group_chat_id —— 当前实例必须
                    # 是这个项目的成员（在某个 position 的 assignees 中），否则模型
                    # 会被喂一个 bot 进不去的 chat_id，飞书发消息报 invalid receive_id。
                    if _me and _pcfg.get_position_for_instance(_me) is None:
                        continue
                    gcid = _pcfg.group_chat_id
                    if gcid and gcid.startswith("oc_"):
                        _wake_chat_id = gcid
                        break
            except Exception:
                pass
            # 也要同步写回 ctx var，让 chat_id 跟随整个 wake
            if _wake_chat_id:
                try:
                    from domain.lifecycle.runtime_context import set_current_event_chat_id
                    set_current_event_chat_id(_wake_chat_id)
                except Exception:
                    pass

        if _wake_chat_id:
            try:
                from domain.conversations import list_chat_messages
                msgs = list_chat_messages(_wake_chat_id, limit=10)
                if msgs:
                    snippet_lines = [
                        f"## ── 当前对话近期流水 ──"
                    ]
                    # ⚠️ heading 不放 chat_id（避免模型从 heading 抄到截断值），
                    # _wake_chat_id 已由上游 express_to_human 默认回复上下文绑定，
                    # 模型无需显式填。跨通道去重已在发送侧根治，无需二级去重。
                    for m in msgs:
                        text = (m.get("text") or "").strip()
                        if not text:
                            continue

                        sender = (m.get("sender_name") or "").strip()
                        if not sender:
                            sender = m.get("sender_id") or "未知"
                        # 历史流水全量进 prompt（不再截断 [:800]）。
                        # 旧 200→800 的截断会让长消息/代码/markdown 表格丢内容，
                        # 用户反馈 zero"看不全"正是这里栽的——消息里的标的、参数、
                        # 表格关键列被砍掉。去掉截断换完整性，token 成本可接受。
                        snippet_lines.append(f"{sender}：{text}")
                    snippet_lines.append("## ── /当前对话近期流水 ──")
                    prev_history.append({
                        "role": "user",
                        "content": "\n".join(snippet_lines),
                        "_sys_tool": "chat_stream",
                    })
                    logger.info(
                        "Injected chat_stream: %d msgs from chat %s",
                        len(msgs), _wake_chat_id[:16],
                    )
                    # 系统已把 _wake_chat_id 的近期流水展示给模型 → 登记为「本 session 已查看」，
                    # 供 express_to_human 发送前校验「目标通道是否看过」（channel_views 账本）。
                    try:
                        from domain.lifecycle.channel_views import mark_channel_viewed
                        mark_channel_viewed(_wake_chat_id)
                    except Exception:
                        logger.debug("channel_views mark failed for chat %s", _wake_chat_id[:16], exc_info=True)
            except Exception as exc:
                logger.debug("chat_stream injection failed: %s", exc)

        # Persist continuation history to audit (real prior turns, only those
        # not tagged with _sys_tool — those are captured separately as
        # slow_ctx injections via agent._convert_user_to_tool).
        if wake_ctx is not None:
            try:
                real_continuation = []
                for m in prev_history:
                    if m.get("_sys_tool"):
                        continue  # slow_ctx, recorded separately
                    real_continuation.append(m)
                if real_continuation:
                    wake_ctx.record_continuation(real_continuation)
            except Exception:
                logger.debug("Failed to record continuation in audit", exc_info=True)

        def _run_agent():
            nonlocal agent_result, agent_error
            nonlocal _agent_started   # ← 关键仪表:agent 是否真正启动过
            try:
                _agent_started = True   # 标记:从这一行起,事件已被 inline 进 prompt
                agent_result = agent.run_conversation(
                    action_prompt,
                    system_message=system_for_agent,
                    conversation_history=prev_history or None,
                )
            except Exception as e:
                agent_error = e

        context = copy_context()
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="l4-wake")
        try:
            context.run(_run_agent)
        except Exception as e:
            agent_error = e
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        if agent_error:
            raise agent_error

        final_resp = agent_result.get("final_response") or ""
        if not final_resp.strip():
            logger.warning("Wake agent returned empty response — rolling back")
            summary["error"] = "empty_response"
            # 失败 → 事件放回队列 + 每个事件自带指数退避（设计文档 5.5/22.1：
            # failure recovery 不是闹钟的事，是事件自己的事）。
            # ⚠ 2026-06-23 修正: 只有 agent **完全没启动过**(连第一个 LLM call 都没
            # 发出去)才 unconsume。一旦 agent 启动过,事件就 inline 进 prompt
            # 被模型看见了——不"已看见但没回应"不该夺回事件。
            # 历史 BUG: 群消息 awaiting_reply 1421 被 alpha wake 1125
            # 占用 14 分钟跑了 55 个 LLM call 然后崩溃 → unconsume →
            # 下个 cron 又 pop 到 → 又触发 wake 1127 → 又跑偏 → 又崩溃 ...
            # 同一条 awaiting_reply 被复活 2 次,造成"假警报"现象。
            if _agent_started:
                logger.info(
                    "agent already started before failure — NOT unconsuming %d auto-consumed events "
                    "(they were visible to the model in prompt). Will still apply delay_pending_events "
                    "per-event exponential backoff.",
                    len(auto_consumed_eids),
                )
            elif auto_consumed_eids:
                try:
                    from domain.lifecycle.events import unconsume_events
                    unconsume_events(auto_consumed_eids)
                except Exception:
                    pass
            if pending_events:
                try:
                    from domain.lifecycle.events import delay_pending_events
                    n = delay_pending_events(pending_events)
                    logger.info(
                        "Empty response: backed off %d/%d pending events with per-event exponential delay",
                        n, len(pending_events),
                    )
                except Exception as exc:
                    logger.warning("delay_pending_events failed (empty_response): %s", exc)
            _rollback_to_blocked(affair_id, reason="empty_response")
            return summary

        summary["agent_response"] = final_resp[:200]
        logger.info("Wake agent completed: reason=%s, %s", reason, summary["agent_response"][:80])
        # 健康监控：本轮 agent 成功 → 清零连续失败计数（健康自动恢复 ok）。
        try:
            _clear_wake_health_failure(instance_id)
        except Exception:
            pass

        # Agent 没显式调 rest() → 退出 RUNNING，但不再自动塞兜底 timer。
        # rest() 是 agent 的主动职责；这里只把状态归位，下一轮事件到来时
        # （消息/闹钟/作息）再自然 wake。系统不替 agent 做节奏决策。
        try:
            aff = get_affair(affair_id)
            if aff and aff.status == AffairStatus.RUNNING.value:
                update_affair(affair_id, status=AffairStatus.BLOCKED)
                logger.info("Agent ended without rest() — closing session, BLOCKED (no fallback timer)")
        except Exception:
            pass

    except Exception as e:
        # 429 / 网络 / agent crash：失败 → 事件放回队列 + 每个事件自带指数退避
        # （设计文档 5.5 / 22.1：闹钟只管「到没到点」，事件只管「有没有待处理」，
        # failure recovery 不是闹钟的事）。
        # auto-consumed 的事件先 unconsume，然后把所有 pending_events 按各自
        # resurrect_count 推到未来（首发 2 min，2/4/8/.../60 min），靠
        # pop_due_events 的 `fire_at <= now` 守卫让它们在退避窗口内对 cron 不可见。
        # 这从结构上杜绝「每分钟重复触发」——无论 429、网络断、LLM 抽风。
        is_llm_429 = "429" in str(e) or "Too Many Requests" in str(e)
        if is_llm_429:
            logger.warning(
                "Wake agent failed with 429 (token quota exhausted). Affair=%s — "
                "pending events will back off with per-event exponential delay",
                affair_id,
            )
        else:
            logger.exception("Wake agent failed: %s", e)

        summary["error"] = str(e)
        # 健康监控：连续失败到阈值才写 error flow event，让健康检查亮异常 + 显示原因。
        # GLM 偶发超时/429 是常态，单次不当告警；成功一次即清零（下次成功自动恢复）。
        try:
            _record_wake_health_failure(instance_id, str(e), is_429=is_llm_429)
        except Exception:
            pass
        # ⚠ 2026-06-23 修正: empty_response 分支同样的 _agent_started 守卫——
        # agent 已经跑过的失败不该 unconsume(事件已被模型看见)。详见 empty_response
        # 分支注释里的 awaiting_reply 1421 BUG 复盘。
        if _agent_started:
            logger.info(
                "agent already started before exception — NOT unconsuming %d auto-consumed events "
                "(they were visible to the model in prompt).",
                len(auto_consumed_eids),
            )
        elif auto_consumed_eids:
            try:
                from domain.lifecycle.events import unconsume_events
                unconsume_events(auto_consumed_eids)
            except Exception:
                pass
        if pending_events:
            try:
                from domain.lifecycle.events import delay_pending_events
                n = delay_pending_events(pending_events)
                if is_llm_429:
                    logger.info(
                        "429 backoff: %d/%d pending events delayed with per-event exponential delay",
                        n, len(pending_events),
                    )
            except Exception as exc:
                logger.warning("delay_pending_events failed: %s", exc)
        rollback_reason = "429_quota_exhausted" if is_llm_429 else "agent_failed"
        _rollback_to_blocked(affair_id, reason=rollback_reason)
    finally:
        # 记录本轮 session 结束时间，供下一次唤醒判断是否延续。
        if _session_db and session_id:
            try:
                from domain.lifecycle.clock import now_dt
                _last_session_end[instance_id or ""] = {"session_id": session_id, "at": now_dt()}
            except Exception:
                pass
        if _chat_token is not None:
            try:
                from domain.lifecycle.runtime_context import reset_current_event_chat_id
                reset_current_event_chat_id(_chat_token)
            except Exception:
                pass
        try:
            if _session_db and session_id:
                end_reason = "completed"
                if summary.get("error"):
                    end_reason = f"error:{summary['error']}"
                # 把本 session 实际累计的 token 用量回写到 sessions 表
                # （只在本进程同一作用域能拿到 agent 实例时传）
                _in_t = getattr(agent, "session_input_tokens", None)
                _out_t = getattr(agent, "session_output_tokens", None)
                if _in_t is not None and _out_t is not None:
                    _session_db.end_session(
                        session_id, end_reason,
                        input_tokens=_in_t, output_tokens=_out_t,
                    )
                else:
                    _session_db.end_session(session_id, end_reason)
        except Exception as e:
            logger.debug("Failed to mark wake session ended: %s", e)
        # End the audit wake (mirror legacy end_session for the new audit DB).
        if wake_ctx is not None:
            try:
                audit_end_reason = "error" if summary.get("error") else "normal"
                _in_t = getattr(agent, "session_input_tokens", 0)
                _out_t = getattr(agent, "session_output_tokens", 0)
                wake_ctx.end(
                    end_reason=audit_end_reason,
                    input_tokens=_in_t,
                    output_tokens=_out_t,
                )
            except Exception as e:
                # ⚠ bug1 修复：原来这里只 logger.debug 静默吞，导致 ended_at 永远 NULL。
                # 下一条消息的 mid-session 注入靠 ended_at IS NULL 选归属 wake，
                # 就会把消息错挂到这条失败 wake 的 id 上（前端按 wake_id 分组 → 归到上一组）。
                # 兜底：直接 UPDATE 强写 ended_at，确保本 wake 行不会留着 NULL 误导后续注入。
                logger.warning("WakeContext.end failed, force-marking ended_at: %s", e)
                try:
                    from infrastructure.persistence.instance import get_audit
                    from domain.lifecycle.clock import now_iso
                    get_audit(instance_id).execute(
                        "UPDATE wake SET ended_at = COALESCE(ended_at, ?) WHERE id = ?",
                        (now_iso(), wake_ctx.wake_id),
                    )
                except Exception as ee:
                    logger.error("Force-mark ended_at also failed for wake %s: %s",
                                 getattr(wake_ctx, "wake_id", "?"), ee)
        # reset current_wake_id：本 wake 作用域结束，注入归属回到"不在 wake 内"
        if _wake_id_token is not None:
            try:
                from domain.lifecycle.runtime_context import reset_current_wake_id
                reset_current_wake_id(_wake_id_token)
            except Exception:
                pass
        # 记忆巩固：session 结束后自动生成 digest
        try:
            if _session_db and session_id:
                from domain.memory.memory.summaries.consolidation_runtime import consolidate_after_session
                consolidate_after_session(_session_db, session_id)
        except Exception as e:
            logger.debug("Memory consolidation failed: %s", e)

        # P3 T039: session 末把 projects / global_todos re-index 进 unified slice 池,
        # 让"项目档案 / 待办" 能被检索命中(spec §User Story 3 / FR-303 可扩展性)。
        # 跟 consolidate_after_session 同节奏(低频),失败绝对不阻塞 session end。
        try:
            from domain.memory.memory.recall.unified.normalizers import (
                index_projects_and_todos,
            )
            index_projects_and_todos()
        except Exception as e:
            logger.debug("Project/todo re-index (failed, non-blocking): %s", e)

        # Session-end cleanup: consume ALL pending events that weren't consumed
        # during the session. Only do this on success — on failure events were
        # already rolled back so they can be re-popped on retry.
        if pending_events and not summary.get("error"):
            consumed_set = set(auto_consumed_eids)
            for ev in pending_events:
                eid = ev.get("event_id")
                if eid and eid not in consumed_set:
                    try:
                        from domain.lifecycle.events import consume_event
                        consume_event(eid, session_id=session_id or "")
                    except Exception:
                        pass
        set_current_affair(None)
        from infrastructure.config import reset_current_session_id
        reset_current_session_id(_session_id_token)

    return summary


def _rollback_to_blocked(affair_id: str, reason: str = "unknown") -> None:
    """agent 失败时回退到 BLOCKED 状态。

    设计原则（设计文档 5.5 / 22.1）：闹钟只管「到没到时间」，事件队列只管
    「有没有待处理的」。failure recovery 不归属闹钟——闹钟只为真实的未来
    时间点服务（作息、回复等待、deadline、agent 主动 rest）。

    因此本函数：
      - 把 affair 状态置回 BLOCKED
      - **不再注册 retry alarm**

    真正的退避由事件自己负责：调用方在失败后调用 delay_pending_events()
    把 pending_events 推到未来，靠 pop_due_events 的 `fire_at <= now` 守卫
    让它们在退避窗口内对 cron 不可见。这从结构上杜绝「每分钟重复触发」。

    `reason` 仅用于日志，不再参与退避计算（429 vs 普通 失败的区分对事件
    语义没意义——同一次 wake 失败就是同一次失败，看不到下次是否还是 429）。
    """
    try:
        from domain.lifecycle.affairs.runtime import update_affair
        from domain.lifecycle.state_machine import AffairStatus

        update_affair(affair_id, status=AffairStatus.BLOCKED)
        logger.warning("Rollback: set affair %s to BLOCKED (reason=%s)", affair_id, reason)
    except Exception as e:
        logger.exception("Rollback failed for affair %s: %s", affair_id, e)


def run_heartbeat_cycle() -> dict:
    """旧的 CLI heartbeat 命令兼容入口。"""
    try:
        from cron.scheduler import tick

        processed = tick(verbose=False)
        return {"ok": True, "processed": processed}
    except Exception as exc:
        logger.exception("Heartbeat cycle failed: %s", exc)
        return {"ok": False, "error": str(exc)}


def main():
    """CLI 入口点（向后兼容）。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )
    print("L4 scheduler now uses event-driven wake. Use cron tick() instead.")


if __name__ == "__main__":
    main()
