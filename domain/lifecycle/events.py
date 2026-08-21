"""L4 事件总线桥接层 — 面向业务的事件发布/查询/消费 API。

底层存储：LegacyEventBus（SQLite events 表），实现在 domain.lifecycle.event_bus 中。

对外 API：
  emit_event(kind, payload)     — 发布事件（含防抖合并 + 实例隔离）
  pop_due_events(limit)         — 获取到期未消费事件（只读，不消费）
  consume_event(event_id, session_id) — 标记事件已消费
  list_recent_events(hours)     — 列出近期事件（含已消费）
  consume_events_by_kind(kind)  — 按类型批量消费
  pop_events_by_kind(kind)      — 按类型取出并消费

多实例隔离机制：
  通过 contextvars.ContextVar(_instance_channel_var) 实现线程级实例隔离。
  cron 守护线程在每个 tick 开始时调用 set_instance_context(instance_id)，
  后续所有事件操作自动使用正确的 channel 前缀（instance:{uuid}）。
  channel 在 emit 时写入 events 表的 channel 列，查询时通过 LIKE 'instance:{uuid}%' 过滤。

防抖机制 (_apply_debounce)：
  根据 event_registry 配置的 debounce_window_s + merge_policy：
  - latest: 窗口内同类型 → 用新 payload 覆盖最新一条
  - accumulate: 累加 _merged_texts（最多 5 条）+ _merged_count
  - count: 只增计数不合并文本
  定时事件（有 fire_at）不防抖。
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import random
import sys
from datetime import timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set


from .clock import now_dt, now_iso

_repo_root = Path(__file__).resolve().parents[2]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

# 每线程/协程的实例上下文 — cron 守护线程在每个 tick 开始时设置
# channel 格式：instance:{uuid}，所有事件操作自动限域到当前实例
_instance_channel_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "instance_channel", default="instance:zero"
)


def set_instance_context(instance_id: str | None = None) -> contextvars.Token:
    """设置当前实例上下文（每个 cron tick 开始时调用）。

    返回的 Token 用于在 tick 结束时 reset_instance_context() 恢复。
    """
    if instance_id is None:
        instance_id = os.environ.get("DIGITAL_LIFE_INSTANCE_ID", "zero")
    return _instance_channel_var.set(f"instance:{instance_id}")


def reset_instance_context(token: contextvars.Token) -> None:
    """恢复到上一个实例上下文。"""
    _instance_channel_var.reset(token)


def _get_instance_channel() -> str:
    """返回当前实例的 channel 前缀，用于事件隔离。"""
    return _instance_channel_var.get()

from domain.lifecycle.event_bus import LegacyEventBus  # noqa: E402
from domain.lifecycle.affairs.runtime import _conn, init_db  # noqa: E402

logger = logging.getLogger(__name__)


_event_bus = LegacyEventBus(
    connection_factory=_conn,
    init_database=init_db,
    now_iso=now_iso,
    now_dt=now_dt,
)


def _ensure_columns() -> None:
    """幂等扩展 events 表列（fire_at、kind、consumed_by_session_id）。"""
    _event_bus.ensure_columns()


def _caller_summary() -> list[str]:
    """返回 3 帧调用栈摘要（模块.函数:行），用于消费日志反向追踪是谁消费了事件。

    纯诊断辅助，零行为变更；任何异常都吞掉（不能因为日志坏业务）。
    """
    import traceback
    try:
        frames = traceback.extract_stack()[-4:-1]  # 跳过本函数 + consume_event
        return [f"{f.name}@{f.lineno}" for f in frames]
    except Exception:
        return ["?"]


def _apply_debounce(kind: str, payload: Dict, fire_at: Optional[str]) -> Optional[int]:
    """防抖合并 — 窗口期内同类型事件合并为一条，避免事件风暴。

    返回值：
      None → 未命中防抖，正常 insert 新事件
      event_id → 命中防抖，已更新已有事件的 payload，无需再 insert

    合并策略（从 event_registry 读取）：
      - latest: 用新 payload 覆盖旧 payload，只增 _merged_count（如 human_message）
      - accumulate: 累加 _merged_texts 列表（最多 5 条），保留最新 sender_name/text
      - count: 只增 _merged_count，不合并文本内容

    定时事件（有 fire_at）不防抖——每次闹钟到期都是独立事件，必须逐一触发。
    channel 过滤确保多实例之间防抖互不干扰。
    """
    if fire_at:
        return None  # 定时事件不防抖
    try:
        from .event_registry import resolve_event_config
        cfg = resolve_event_config(kind)
    except Exception:
        return None

    window_range = cfg.get("debounce_window_s", (0, 0))
    # 兼容老调用（int 直接当作 (n, n)）
    if isinstance(window_range, int):
        lo = hi = window_range
    elif isinstance(window_range, (list, tuple)) and len(window_range) >= 2:
        lo, hi = int(window_range[0]), int(window_range[1])
        if lo > hi:
            lo, hi = hi, lo
    else:
        lo = hi = 0
    if lo <= 0 and hi <= 0:
        return None
    # 单点窗口（lo == hi）直接用，范围窗口随机取一个值
    window = lo if lo == hi else random.randint(lo, hi)
    policy = cfg.get("merge_policy", "latest")

    cutoff = (now_dt() - timedelta(seconds=window)).isoformat(timespec="seconds")
    instance_channel = _get_instance_channel()
    try:
        with _conn() as conn:
            # channel 用 LIKE 前缀匹配——handler 写入的 channel 实际是
            # "instance:{uuid}/gateway:lark:group"(因为 emit_event 第 213 行拼接了
            # 显式 channel),而 _get_instance_channel 返回的是 "instance:{uuid}"(不含
            # 子通道)。严格 = 匹配永远不命中,debounce 失效。
            # LIKE 让 instance 前缀的所有子通道都能被防抖合并。
            row = conn.execute(
                "SELECT event_id, payload FROM events "
                "WHERE kind = ? AND consumed_at IS NULL AND created_at >= ? "
                "AND (channel LIKE ? OR channel IS NULL) "
                "ORDER BY event_id DESC LIMIT 1",
                (kind, cutoff, instance_channel + "%"),
            ).fetchone()
            if not row:
                return None

            existing_id = row["event_id"]
            try:
                existing_payload = json.loads(row["payload"]) if row["payload"] else {}
            except Exception:
                existing_payload = {}

            if policy == "latest":
                merged = dict(payload)
                merged["_merged_count"] = existing_payload.get("_merged_count", 1) + 1
            elif policy == "count":
                merged = dict(existing_payload)
                merged["_merged_count"] = merged.get("_merged_count", 1) + 1
            else:  # accumulate (default for groups)
                merged = dict(existing_payload)
                merged["_merged_count"] = merged.get("_merged_count", 1) + 1
                # 存 sender+text 对的 list,让 batch 触发时模型能看到"谁说了哪条"
                # (不只文本页 senders,历史 BUG:只存 text 模型不知道发话者语境)
                texts = merged.get("_merged_texts", [])
                if not isinstance(texts, list):
                    texts = []
                new_sender = payload.get("sender_name") or payload.get("sender_id") or "?"
                new_text = payload.get("text") or payload.get("last_sent_text") or ""
                if new_text:
                    texts.append({"sender": new_sender, "text": new_text[:200]})
                # 不设条数上限——按你的设计"按时间区间累积,不看消息数量"
                merged["_merged_texts"] = texts
                # 保留最新发送者信息(main text/sender 字段仍指最新的一条,
                # wake_prompt 主行用最新那条,真正 batch 历史走 _merged_texts)
                if "sender_name" in payload:
                    merged["sender_name"] = payload["sender_name"]
                if "text" in payload:
                    merged["text"] = payload["text"]

            conn.execute(
                "UPDATE events SET payload = ?, created_at = ? "
                "WHERE event_id = ? AND (channel LIKE ? OR channel IS NULL)",
                (json.dumps(merged, ensure_ascii=False), now_iso(),
                 existing_id, instance_channel + "%"),
            )
            logger.info(
                "EMIT_DEBOUNCE_MERGED kind=%s event_id=%d policy=%s merged_count=%d "
                "window_s=%d channel_prefix=%r",
                kind, existing_id, policy, merged.get("_merged_count", 1), window,
                instance_channel,
            )
            return existing_id
    except Exception as exc:
        logger.warning("debounce check failed for %s: %s", kind, exc)
        return None


def emit_event(
    kind: str,
    payload: Optional[Dict] = None,
    fire_at: Optional[str] = None,
    channel: Optional[str] = None,
) -> int:
    """发布或调度一个事件——事件生命周期的起点。

    流程(refactor/emit-driven-wake 之后):
      1. **严格校验** kind ∈ event_registry — 未注册直接 raise ValueError,不写库
      2. 解析当前实例 channel 前缀,实现多实例隔离
      3. 走防抖合并(``_apply_debounce``),命中则返回已有 event_id(**不重复叫醒**)
      4. 未命中 → INSERT 到 events 表
      5. 新建事件 + ``fire_at is None`` → 立刻调 ``_wake_or_inject()``
         即时唤醒决策统一归此入口(handler / cron / broadcast 都不再各自叫醒)

    设计依据:**事件平权(语义无特权),但 emit 成功后立刻尝试 wake,叫醒决策
    统一归事件队列。延迟由防抖/消息系统(group_buffer)控制,不再由 cron 60s
    节拍决定**。详见 docs/design 第 5.1/14.2 节修订版。

    防抖命中不叫醒:首次 emit 已经叫过,合并是"这条事件已在队列里"的状态。

    channel 参数:外部显式传入(如 gateway:lark:user)会被拼接为
    instance:{uuid}/gateway:lark:user,多实例隔离 + 保留子通道信息。

    fire_at: NULL=立即可触发;非 NULL=定时事件(到点才 due),由 cron 接管叫醒。
    """
    # Resolve instance-scoped channel for isolation
    instance_channel = _get_instance_channel()

    # 1. 严格校验:kind 必须在 event_registry 注册。未注册 raise ValueError——
    #    防止悄悄写一堆不认识的事件进队列(历史 emit_event 默默 warning 不拦,
    #    导致 typo kind 的事件永远 cron-pop 不到它们对应的 prompt_template)。
    from .event_registry import validate_event_type

    validate_event_type(kind, raise_on_unknown=True)

    payload = payload or {}

    # ⚠ 诊断日志[嵌入]:群消息穿透排查用。零行为变更,仅 INFO 级。
    # 关键字段:chat_id / sender_name / chat_name / text 头部——足以回溯入站轨迹
    # 而不爆量。text 截到 60 字以免长消息刷屏。
    _diag_payload = {
        k: (v[:60] if isinstance(v, str) and len(v) > 60 else v)
        for k, v in payload.items()
        if k in ("chat_id", "sender_name", "sender_id", "chat_name",
                 "text", "mentions_bot", "_merged_count", "reason",
                 "name", "schedule_id")
    }
    logger.info(
        "EMIT_BEGIN kind=%s instance_channel=%r explicit_channel=%r payload_head=%r",
        kind, instance_channel, channel, _diag_payload,
    )

    merged_id = _apply_debounce(kind, payload, fire_at)
    if merged_id is not None:
        logger.info(
            "EMIT_END kind=%s result=debounced-merged return_event_id=%d",
            kind, merged_id,
        )
        # 合并不叫醒:首次 emit 已经触发过 wake,合并是"事件已在队列"的状态。
        return merged_id

    # Resolve final channel: explicit channel is prefixed with instance to isolate it
    if channel is not None:
        effective_channel = f"{instance_channel}/{channel}"
    else:
        effective_channel = instance_channel

    try:
        new_id = _event_bus.emit_event(
            kind=kind, payload=payload, fire_at=fire_at, channel=effective_channel,
        )
        logger.info(
            "EMIT_END kind=%s result=inserted return_event_id=%d effective_channel=%r",
            kind, new_id, effective_channel,
        )
    except Exception as exc:
        logger.error(
            "EMIT_END kind=%s result=FAILED effective_channel=%r exc=%r",
            kind, effective_channel, exc,
        )
        raise

    # ⭐ 唯一叫醒入口:新建事件(fire_at is None)→ 立刻决定 wake / inject。
    #    定时事件(fire_at ≠ None)走 alarm 通道,到期时由 cron 取走叫醒。
    if fire_at is None:
        try:
            _wake_or_inject(new_id)
        except Exception as exc:
            # 叫醒失败不能影响 emit 返回值(INSERT 已成功)——靠 60s cron 兜底。
            logger.warning(
                "WAKE_OR_INJECT failed for event_id=%d (will rely on cron): %s",
                new_id, exc,
            )

    return new_id


def _wake_or_inject(triggering_event_id: int) -> None:
    """emit 新建事件后的**唯一叫醒入口**。

    决策表(所有外部系统 emit 事件后都走这里,handler/cron/broadcast 不需各自叫醒):

    | 状态                   | wake_in_progress | 行为                                       |
    |------------------------|------------------|--------------------------------------------|
    | affair == RUNNING      | 任意              | signal_new_events(mid-session 注入内存池) |
    | affair == BLOCKED/PEND | True             | return(防重复叫醒)                       |
    | affair == BLOCKED/PEND | False            | 起后台线程 wake(pop 全 due 一起带走)     |
    | affair 不存在/其它      | 任意              | warn + return(等 cron 兜底)             |

    实例隔离:用当前 set_instance_context 的 ContextVar。所有 emit 调用方
    (handler / broadcast / cron / monitor)都已设置好此 ContextVar。

    失败:任何异常都被吞(调用方已 catch),由 60s cron tick 兜底。
    """
    import threading

    # 解析 instance_id (instance:{uuid} → uuid)
    instance_channel = _get_instance_channel()
    instance_id = instance_channel.split(":", 1)[1] if ":" in instance_channel else ""

    if not instance_id or instance_id == "zero":
        # default 值 "instance:zero" 意味着 ContextVar 没设——本函数被错误地从不带
        # 实例上下文的线程调用。直接 return 让 cron 兜底,避免事件被叫醒后 channel
        # prefix 不匹配的事件队列 pop 不到(BUG C 历史)。
        logger.warning(
            "WAKE_OR_INJECT skip: no instance context for event %d "
            "(channel=%r) — relying on cron",
            triggering_event_id, instance_channel,
        )
        return

    # 账号级熔断闸口（堵真人消息的关键点）：
    # _wake_or_inject 是真人消息即时唤醒的唯一入口，**不经 budget_gate**
    # （budget_gate 只在 cron 路径）。账号级 429 时若放它过去，每次真人消息都会
    # 重新起 wake、_chat 撞 429 三次——把账号级限流从暂停变成反复触发。
    # 熔断生效则直接 return，事件留在 due 队列；熔断到期后 cron 自然 pop 接管。
    # 闸口自身故障必须 fail-through（多打一次 API 比漏唤醒可接受少很多，但这里
    # 事件有 cron 兜底，与熔断本身的 fail-open 语义一致）。
    try:
        from infrastructure.budget.circuit_breaker import is_tripped
        from infrastructure.ai.config import resolve_runtime_provider
        from infrastructure.ai import load_runtime_config
        _key = resolve_runtime_provider(config=load_runtime_config()).get("api_key") or ""
        _tripped, _cb_info = is_tripped(_key)
        if _tripped:
            logger.info(
                "WAKE_OR_INJECT blocked by circuit breaker: event %d will retry on cron "
                "after expires_at=%s (reason started by instance=%s)",
                triggering_event_id,
                _cb_info.get("expires_at"),
                _cb_info.get("tripped_by"),
            )
            return
    except Exception as exc:
        logger.debug("WAKE_OR_INJECT circuit breaker check failed (let through): %s", exc)

    try:
        # 1. 查 affair 状态
        from domain.lifecycle.affairs.runtime import get_affair
        from domain.orchestration.lifecycle_orchestration.bootstrap.runtime import _find_life_affair

        life_aid = _find_life_affair()
        if not life_aid:
            logger.debug("WAKE_OR_INJECT: no life affair — skip")
            return
        aff = get_affair(life_aid)
        if not aff:
            logger.debug("WAKE_OR_INJECT: no affair row — skip")
            return

        # 2. RUNNING → mid-session 注入内存池,不叫醒
        #    (check_before_send 会在模型下次工具调用时感知这些注入事件)
        if aff.status == "RUNNING":
            logger.info(
                "WAKE_OR_INJECT midsession: instance=%s affair=%s event=%d "
                "affair_status=RUNNING → injecting, NOT dispatching new wake",
                instance_id, life_aid[:8], triggering_event_id,
            )
            _inject_to_running_session(triggering_event_id, instance_id)
            return

        # 3. BLOCKED/PENDING 但已有 wake 在跑 → 不重复叫
        #    防止 wake→fast-path→emit→新 wake→...的死循环。
        from domain.lifecycle.scheduler import _is_wake_in_progress
        if _is_wake_in_progress(instance_id):
            logger.info(
                "WAKE_OR_INJECT skip: wake in progress for %s (event %d queued)",
                instance_id, triggering_event_id,
            )
            return

        # 4. BLOCKED → pop 所有 due 批量 wake(不只触发的那一条)
        events = pop_due_events(limit=20)
        if not events:
            # race: app INSERT 后这里 pop 不到(read 视图延迟)。靠 cron 兜底。
            logger.debug(
                "WAKE_OR_INJECT: pop returned 0 right after insert event %d — race?",
                triggering_event_id,
            )
            return
        reason = _choose_reason(events)
        logger.info(
            "WAKE_OR_INJECT dispatch: instance=%s reason=%s pending=%d event_ids=%s",
            instance_id, reason, len(events),
            [e.get("event_id") for e in events],
        )

        # 后台显式重设 ContextVar(对照 cron_lifecycle._bg_wake 的姿势):
        # threading.Thread 不会继承父线程的 ContextVar,必须显式 set。
        _captured_iid = instance_id
        _captured_aid = life_aid
        _captured_reason = reason
        _captured_events = events
        # force_new_session（/new）已改为 session 实体状态（user_reset 终态），
        # 不再走 ContextVar 透传——旧捕获/重放块已删（链路伪状态，跨事件即丢）。

        def _bg_wake() -> None:
            import os as _os
            _os.environ["DIGITAL_LIFE_INSTANCE_ID"] = _captured_iid
            try:
                from infrastructure.config import set_current_instance_id
                set_current_instance_id(_captured_iid)
            except Exception:
                pass
            try:
                set_instance_context(_captured_iid)
            except Exception:
                pass
            try:
                from domain.lifecycle.scheduler import wake_digital_life
                wake_digital_life(
                    _captured_aid, _captured_reason, "",
                    list(_captured_events),
                )
            except Exception as exc:
                logger.warning("WAKE_OR_INJECT background wake error: %s", exc)

        threading.Thread(target=_bg_wake, daemon=True).start()
    except Exception as exc:
        logger.warning("WAKE_OR_INJECT failed: %s", exc)


def _inject_to_running_session(event_id: int, instance_id: str) -> None:
    """RUNNING 中 emit 新事件 → mid-session 通知运行中的会话。

    设计原则（不区分 kind、完全统一）：

    RUNNING 中新事件到达时**不打断正在跑的 wake**，只把"有何新东西到了"
    这件事写到内存 ``signalled_events`` 池。所有事件类型走同一条路——

    - **不写 sessions.db**（不渲染 wake-prompt 模板，那是叫醒用的；也不是
      "user 消息"——它没有 user 角色）。
    - **不立即 consume**：DB events 队列里事件原样保留。
    - **不镜像到 runtime_log.turn**（前端 Transcript 的纳管由 agent 层
      ``wake_signal`` 写 audit_ctx 时一起做）。
    - **不做任何按 kind 分流的处理**。

    后处理交给模型自己——它在自己下一轮 LLM call 启动时主动扫内存池
    （``agent._inject_signalled_events``）。模型那一层**才**按事件类型决定渲染
    与消费策略：message/group_message 展示全文 + 同步消费；其它 kind 只展示
    ID+类型、由模型主动 ``sense_event_detail`` 决定查看。

    **rest 边界（所有 kind 一致）**：本函数不提前消费 DB 事件。即使模型在扫到
    内存池之前就 ``rest()`` 结束当前 wake，事件依然完整保留在 events 队列，cron
    下一轮 ``pop_due_events`` 自然取它、走正常 wake 流程——没有"消息丢失"风险。
    """
    try:
        from domain.lifecycle.event_registry import get_event_type

        ev = _peek_single_event(event_id)
        if not ev:
            logger.debug("mid-session inject: event %d not found, skip", event_id)
            return

        kind = ev.get("kind", "")
        eid = ev.get("event_id", event_id)
        ev_type = get_event_type(kind)
        payload = ev.get("payload", {}) if isinstance(ev.get("payload"), dict) else {}

        # 统一对所有 kind 走同一条路：仅 signal 到内存 signalled_events 池。
        # 不渲染、不写 DB、不消费 events 队列、不镜像 audit。
        summary = {
            "event_id": eid,
            "kind": kind,
            "display_name": ev_type.display_name if ev_type else kind,
            "description": ev_type.description if ev_type else "",
            "payload": payload,
        }
        try:
            from domain.lifecycle.session_events import signal_new_events
            signal_new_events([summary], instance_id=instance_id)
            logger.info(
                "WAKE_OR_INJECT mid-session signaled %s event %d to RUNNING %s "
                "(no early consume; awaits model next-turn pickup or cron fallback)",
                kind, eid, instance_id,
            )
        except Exception as exc:
            logger.debug("signal_new_events failed: %s", exc)

        # 语音呼唤注入 RUNNING session 时，也要把 event_platform 切到 voice，
        # 否则 express_to_human 默认走飞书（用户的语音会被飞书文字回复）。
        # wake 启动路径在 scheduler._wake_digital_life_inner 里检测 reply_channel，
        # 但 inject 不走那条路——这里补上。
        #
        # 注意：必须同时写 ContextVar + 全局 _REPLY_CONTEXT mirror。
        # ContextVar 跨线程不可见（inject 在 emit 线程，LLM tool call 在 agent 线程），
        # 只有全局 dict 能跨线程传递。
        if payload.get("reply_channel") == "voice":
            try:
                from domain.lifecycle.runtime_context import set_current_event_platform
                set_current_event_platform("voice")
            except Exception:
                pass
            try:
                # 全局 mirror（跨线程可见，action_tools._get_runtime_channel_prefix 的 fallback）
                from interfaces.tools.action_tools import _REPLY_CONTEXT
                from infrastructure.config import get_app_instance_id
                _iid = get_app_instance_id() or instance_id
                _REPLY_CONTEXT.setdefault(_iid, {})["platform"] = "voice"
                logger.info("mid-session inject: event_platform=voice (reply_channel=voice, event %d)", eid)
            except Exception as exc:
                logger.debug("set event_platform=voice on inject failed: %s", exc)
    except Exception as exc:
        logger.warning("mid-session inject failed for event %d: %s", event_id, exc)


def _peek_single_event(event_id: int) -> dict | None:
    """只读取出一条事件（不 consume），供 mid-session 注入用。"""
    try:
        import sqlite3
        from infrastructure.config import get_runtime_state_db_path
        sdb_path = get_runtime_state_db_path()
        if not sdb_path.exists():
            return None
        conn = sqlite3.connect(str(sdb_path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT event_id, channel, payload, created_at, fire_at, kind, "
                "consumed_at, resurrect_count FROM events "
                "WHERE event_id = ? AND consumed_at IS NULL",
                (event_id,),
            ).fetchone()
            if not row:
                return None
            import json
            payload = {}
            try:
                payload = json.loads(row["payload"]) if row["payload"] else {}
            except Exception:
                payload = {}
            return {
                "event_id": row["event_id"],
                "channel": row["channel"],
                "payload": payload,
                "created_at": row["created_at"],
                "fire_at": row["fire_at"],
                "kind": row["kind"],
                "consumed_at": row["consumed_at"],
                "resurrect_count": row["resurrect_count"],
            }
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("_peek_single_event failed for %d: %s", event_id, exc)
        return None


# lazy import for choose_reason — 避免模块加载时循环依赖
def _choose_reason(events: list[dict]) -> str:
    from domain.lifecycle.wakeup_policy import choose_reason
    return choose_reason(events)


def pop_due_events(limit: int = 50) -> List[Dict]:
    """取出当前实例到期未消费的事件（只读不消费，用于 cron tick 和 sense 工具）。

    查询条件：consumed_at IS NULL AND (fire_at IS NULL OR fire_at <= now)
             AND channel LIKE 'instance:{uuid}%'
    按 event_id ASC 排序，确保先入先出。
    """
    channel_prefix = _get_instance_channel()
    events = _event_bus.pop_due_events(limit=limit, channel_prefix=channel_prefix)
    logger.info(
        "POP_DUE channel_prefix=%r limit=%d returned=%d kinds=%s",
        channel_prefix, limit, len(events),
        [e.get("kind") for e in events][:10],  # 只丢 kind 列表，前 10
    )
    return events


def list_recent_events(
    hours: float = 6.0,
    kinds: Optional[Set[str]] = None,
    include_consumed: bool = True,
    limit: int = 100,
) -> List[Dict]:
    """列出近期事件（含已消费），用于生命周期上下文和 sense 工具查询。

    按 created_at 时间窗口 + channel 前缀过滤，结果按 event_id DESC 排序。
    支持按 kinds 集合二次过滤（在 Python 侧进行）。
    """
    channel_prefix = _get_instance_channel()
    return _event_bus.list_recent_events(
        hours=hours,
        kinds=kinds,
        include_consumed=include_consumed,
        limit=limit,
        channel_prefix=channel_prefix,
    )


def consume_event(event_id: int, target_affair_id: Optional[str] = None, session_id: Optional[str] = None) -> None:
    """标记事件已消费——事件生命周期的终点。

    写入 consumed_at（当前时间）和 consumed_by_session_id（消费会话）。
    consumed_by_session_id 被 calendar() 用于按会话聚合展示过往活动记录。

    唯一的生产消费入口是 sense_event_detail 工具。session_id 优先使用显式传入值，
    否则从 ContextVar 读取（scheduler 在 session 创建时设置）。
    """
    from infrastructure.config import get_current_session_id

    sid = session_id or get_current_session_id() or None
    logger.info(
        "CONSUME event_id=%d target_affair=%r session_id=%r caller_stack_head=%s",
        event_id, target_affair_id, sid,
        # 拿一行 caller 上下文，方便辨认是 sense_event_detail / scheduler auto / eof cleanup
        # 哪条路径消费的。栈太深只取 3 帧。
        "; ".join(_caller_summary())[:160],
    )
    _event_bus.consume_event(event_id=event_id, target_affair_id=target_affair_id, session_id=sid)


def unconsume_events(event_ids: list[int]) -> int:
    """回退事件消费——将已消费事件恢复为未消费状态。

    用于 LLM 调用失败等异常场景：auto-consume 后 agent 报错，需要让事件
    在下一轮 tick 重新被 pop 出来。返回实际回退的数量。
    """
    channel_prefix = _get_instance_channel()
    n = _event_bus.unconsume_events(event_ids=event_ids, channel_prefix=channel_prefix)
    logger.info(
        "UNCONSUME channel_prefix=%r requested=%d actual=%d ids=%s",
        channel_prefix, len(event_ids), n, event_ids[:10],
    )
    return n


def backoff_minutes_for(event: dict, *, base: float = 2.0, cap: float = 60.0) -> float:
    """根据单个事件已有的失败次数算下次退避分钟数。

    事件自己记得被「复活」过几次（resurrect_count）。退避 = base * 2^n，
    n = resurrect_count（当前已有失败次数）。封顶 cap。

      首次失败（n=0）→ 2 min
      第二次（n=1）→ 4 min
      第三次（n=2）→ 8 min
      ...
      第八次及以后 → 60 min（封顶）

    纯函数，不碰数据库；调用方拿到值后再传给 delay_pending_events。
    """
    try:
        n = int(event.get("resurrect_count", 0) or 0)
    except Exception:
        n = 0
    return min(cap, base * (2 ** n))


def delay_pending_events(events: list[dict], *, base: float = 2.0, cap: float = 60.0) -> int:
    """事件消费失败后的「自我指数退避」——把这次 wake 抓出来的整批事件
    推迟到同一个未来时间点，再集体重试。

    设计依据（设计文档 5.5 / 22.1）：闹钟只管「到没到时间」，事件只管
    「有没有待处理」。failure recovery 不归属闹钟——闹钟只为真实的未来
    时间点服务（作息、回复等待、deadline、agent 主动 rest）。事件被消费
    失败时，自己学会推迟下次露面时间：
      - consumed_at 重置为 NULL（再次未消费）
      - fire_at 设为 now + backoff（推到未来）
      - resurrect_count 自增

    pop_due_events 的 SQL 守卫 `fire_at <= now` 会自动让这些事件在退避
    窗口内对 cron 不可见，从结构上杜绝「每分钟重复触发」。

    三层解耦（与用户对齐的最终语义）：
      - **触发层**：每个事件记自己的 resurrect_count（事件个体属性）
      - **呈现层**：数字生命睁眼时只关心「看到一件还是多件」，走
        单事件或多事件清单路径（设计文档 5.3），不感知退避状态
      - **失败处置层（本函数）**：同一批被这次 wake 抓出来的事件，
        如果失败 → 一起退避到本批里**最大的**那个下次唤醒时间
        （取这批里 resurrect_count 最大的事件应得的退避分钟数）

    同频而不是各自独立退避的原因：一次 wake 是一个"挤压队列"的处理尝试。
    失败了，整个队列一起退避到同一下次尝试点，下次它们仍然形成队列
    一起被单次会话消费；而不是被拆散、各自飘到不同的未来点。后者会
    退化成"一次只能做一件事"——违背"挤压队列一次性消费"的设计意图。

    Args:
        events: pending_events 列表（带 event_id 字段的 dict）
        base: 退避基数（分钟）。默认 2 → 2/4/8/16/32/60。
        cap: 单次退避上限（分钟）。默认 60。

    Returns:
        实际被推迟的事件数。
    """
    if not events:
        return 0
    # 同频到本批最大值：避免「同一次 wake 的事件下一次被拆到不同时间点」
    max_minutes = 0.0
    eids: list[int] = []
    for ev in events:
        eid = ev.get("event_id")
        if not eid:
            continue
        eids.append(int(eid))
        # 每个事件按自己的 resurrect_count 算应得分钟（保留独立计数语义），
        # 但实际施加的是这批的最大值，确保下次同步回来。
        minutes = backoff_minutes_for(ev, base=base, cap=cap)
        if minutes > max_minutes:
            max_minutes = minutes
    if not eids:
        return 0
    channel_prefix = _get_instance_channel()
    total = _event_bus.delay_pending_events(
        event_ids=eids, minutes=max_minutes, channel_prefix=channel_prefix,
    )
    if total:
        logger.info(
            "delay_pending_events: %d event(s) backed off together to +%d min "
            "(同步退避 = 整批下次一起回来形成队列)",
            total, int(max_minutes),
        )
    return total


def consume_events_by_kind(kind: str, session_id: Optional[str] = None) -> int:
    """按类型批量消费到期事件——一次性把同类型未消费事件全部标记已消费。

    常用于 session 结束时由 system 批量消费 system 类事件
    （routine/timer/vital_threshold/initiative 等）。
    返回实际消费的事件数量。
    """
    from infrastructure.config import get_current_session_id

    sid = session_id or get_current_session_id() or None
    channel_prefix = _get_instance_channel()
    return _event_bus.consume_events_by_kind(kind=kind, channel_prefix=channel_prefix, session_id=sid)


def pop_events_by_kind(kind: str, limit: int = 10, session_id: Optional[str] = None) -> List[Dict]:
    """按类型取出并消费到期事件——先 SELECT 再 UPDATE consumed_at。

    与 consume_events_by_kind 的区别：pop 会返回事件内容（payload），
    consume 只批量标记不返回内容。用于需要拿到事件数据后再消费的场景。
    """
    from infrastructure.config import get_current_session_id

    sid = session_id or get_current_session_id() or None
    channel_prefix = _get_instance_channel()
    return _event_bus.pop_events_by_kind(kind=kind, limit=limit, channel_prefix=channel_prefix, session_id=sid)


def cancel_pending_events(
    kind: Optional[str] = None,
    payload_filter: Optional[Dict] = None,
) -> int:
    """Deprecated shim — LegacyEventBus.cancel_pending_events 的 facade 入口。

    向后兼容：历史调用方 / 测试从 ``domain.lifecycle.events`` 导入此名。
    生产已改走 alarms.cancel_alarm* 系列；保留这个 wrapper 维持公开 import 契约
    （见 events.py 模块顶部的 facade 设计：本模块包 ``_event_bus`` 实例并暴露方法）。
    """
    channel_prefix = _get_instance_channel()
    return _event_bus.cancel_pending_events(
        kind=kind, payload_filter=payload_filter, channel_prefix=channel_prefix,
    )


def schedule_vital_threshold_events(
    predictor,
    *,
    clear_previous: bool = True,
    horizon_hours: float = 72.0,
    segment_filter: Optional[set] = None,
) -> List[Dict]:
    """Deprecated shim — vital_threshold 已改走 emit_event 路径
    （domain.vital.simulation.energy_events.check_energy_events）。

    保留以兼容旧版本调用方与本仓 import 契约，签名与
    LegacyEventBus.schedule_vital_threshold_events 对齐。
    """
    return _event_bus.schedule_vital_threshold_events(
        predictor,
        clear_previous=clear_previous,
        horizon_hours=horizon_hours,
        segment_filter=segment_filter,
    )


def count_pending_by_kind_and_payload(kind: str, payload_key: str, payload_value: str) -> int:
    """统计队列里未消费的同源事件数。

    产品语义（设计文档 6.4 + 你确认的「一次提醒就够」原则）：
    - 「待办太久没动」是一种状态性提醒——同一待办只要队列里已经有一个
      未消费的提醒，就不该再发第二个。
    - 第一次发出后，要么模型推进了（更新 updated_at）→ 状态自然消除，
      要么模型没推进（系统挂 / 失败 / 忙别的）→ 那 1 个未消费的事件
      就是充足的通知，再多也是噪音 + 浪费 token。

    用法：emit_task_reminder 前先 check_count_pending_by_payload('task_reminder',
    'task_id', 'xxx')——如果 >0 就静默跳过。

    Args:
        kind: 事件类型，如 "task_reminder"
        payload_key: payload 中的去重 key，如 "task_id"
        payload_value: 该 key 的目标值

    Returns:
        队列里未消费的同源事件数（0 表示没有，可以发新的）。
    """
    channel_prefix = _get_instance_channel()
    return _event_bus.count_pending_by_payload_key(
        kind=kind,
        payload_key=payload_key,
        payload_value=payload_value,
        channel_prefix=channel_prefix,
    )


def _row_to_dict(row):
    """向后兼容的 SQLite Row → dict 转换，委托 LegacyEventBus.row_to_dict。"""
    return _event_bus.row_to_dict(row)


__all__ = [
    "set_instance_context",
    "reset_instance_context",
    "emit_event",
    "pop_due_events",
    "list_recent_events",
    "consume_event",
    "consume_events_by_kind",
    "pop_events_by_kind",
]
