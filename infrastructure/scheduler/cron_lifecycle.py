"""L4 生命周期钩子 — 每个 cron tick 对单个实例执行的唤醒检查。

核心流程 (_run_l4_tick_inner):
  1. 确保 DB 表存在（懒初始化可能遗漏新表）
  2. 检查是否有 wake 正在进行中 → 跳过
  3. 状态守卫：RUNNING 超 5 分钟 → 回退到 BLOCKED
  4. RUNNING 状态 → 注入新事件到运行中会话
  5. BLOCKED 状态 → 各子系统自行检查（闹钟、精力、例行任务、任务惯性）
  6. 拉取事件队列 → 按优先级确定唤醒原因 → 后台线程启动 wake_digital_life()

多实例隔离：
  - 通过 contextvars 设置实例事件上下文
  - _bg_wake 线程同时设置两个 ContextVar 系统（infrastructure + events 层）
"""

from __future__ import annotations

import logging
import os
import threading

logger = logging.getLogger(__name__)


class _SkipL4(Exception):
    """跳过本次 tick 的 L4 检查信号。"""


def run_l4_tick(*, instance_id: str = "", logger: logging.Logger | None = None, adapters=None, loop=None) -> None:
    """对单个实例执行 L4 事件驱动唤醒检查。

    通过 contextvars 设置实例事件上下文，确保事件查询、session 信号、
    DB 路径都解析到正确的实例。调用方必须预先设置 os.environ["DIGITAL_LIFE_INSTANCE_ID"]。

    Args:
        instance_id: 要 tick 的数字生命实例 ID（如 "zero", "alpha"）。
    """
    log = logger or globals()["logger"]
    log.debug("L4 tick: instance=%s", instance_id)

    # Set per-instance event context (thread-safe via contextvars)
    from domain.lifecycle.events import set_instance_context, reset_instance_context
    token = set_instance_context(instance_id or os.environ.get("DIGITAL_LIFE_INSTANCE_ID", "zero"))

    try:
        _run_l4_tick_inner(instance_id, log)
    finally:
        reset_instance_context(token)


def _run_l4_tick_inner(instance_id: str, log) -> None:
    """单实例 L4 tick 核心逻辑。

    状态机流转：
      RUNNING 超时（>5min）→ 回退 BLOCKED + 定时重试
      RUNNING 正常 → 注入新事件到会话 + 返回
      BLOCKED → 各子系统检查（闹钟/精力/例行/惯性）→ 拉取事件 → 后台唤醒

    唤醒原因按事件优先级（priority）确定，取最高优先级事件的 kind。
    wake_digital_life() 在独立线程中执行，避免阻塞 cron 循环。
    """
    try:
        from domain.lifecycle.affairs.runtime import init_db
        init_db()
    except Exception:
        pass

    try:
        from domain.orchestration.lifecycle_orchestration.bootstrap.runtime import _find_life_affair
        from domain.lifecycle.affairs.runtime import (
            init_db,
            get_affair,
            update_affair,
        )
        from domain.lifecycle.state_machine import AffairStatus, WaitType
        from domain.lifecycle.clock import now_dt

        life_aid = _find_life_affair()
        if not life_aid:
            return

        try:
            from domain.lifecycle.scheduler import _is_wake_in_progress

            if _is_wake_in_progress(instance_id):
                log.debug("L4: wake in progress for %s - skipping tick", instance_id)
                raise _SkipL4()
        except ImportError:
            pass
        except _SkipL4:
            raise

        # ── State guard ────────────────────────────────────────────────────────
        #
        # 历史实现:看 affairs.updated_at 时长 > 300s 就回退 BLOCKED。但 updated_at
        # 在 wake 运行期间完全不动(只在 update_affair 状态变更时刷新),所以一条
        # 合法的 10 分钟长 wake 5 分钟后就被判 stale 回退 → affair 变 BLOCKED →
        # 后续 emit 的新事件走 _wake_or_inject 的"新 wake"分支而不是 midsession
        # 注入。这就是 alpha "wake #1181 还在跑, #1182 却被新触发"的根因。
        #
        # 现在基于真实存活信号(wake_liveness.evaluate_wake_alive):
        #   1) 进程内 _wake_in_progress(快路径,已在上方 _SkipL4 处理)
        #   2) turn 表最近写入(细粒度心跳,跨重启也准)
        # 只有两者都说"死"才回退。阈值统一从 wake_liveness.STALE_RUNNING_SECONDS
        # 读(默认 1800s,实测合法 wake 跑过 1296s 仍正常完成)。

        aff = get_affair(life_aid)
        if aff and aff.status == AffairStatus.RUNNING.value:
            try:
                from domain.lifecycle.wake_liveness import evaluate_wake_alive

                alive, reason, signals = evaluate_wake_alive(instance_id)
                log.info(
                    "L4_STATE_GUARD affair=%s instance=%s alive=%s reason=%s signals=%s",
                    life_aid[:8], instance_id[:8], alive, reason, signals,
                )
                if alive:
                    # wake 还活着,保留 affair=RUNNING(=midsession 注入的前提),
                    # vitals tick 照常跑,直接返回本轮。
                    try:
                        from domain.vital.simulation import get_engine, reset_engine
                        reset_engine()
                        get_engine().tick(state="RUNNING")
                    except Exception as exc:
                        log.debug("L4: vitals tick (RUNNING) failed: %s", exc)
                    return

                # 真死 → 回退 BLOCKED + close session + 写 wait_intent。
                log.warning(
                    "L4: affair RUNNING deemed dead (reason=%s signals=%s) — rolling back",
                    reason, signals,
                )
                from datetime import timedelta

                from domain.lifecycle.affairs.runtime import WaitIntent, set_wait_intent

                update_affair(life_aid, status=AffairStatus.BLOCKED)
                # 硬停 vs 崩溃区分（2026-09-10 重启打断事故）：turn 心跳非常新
                # （<3min，说明刚还在正常干活）却被判死 = 进程被外部 SIGTERM
                # 拦腰打断，不是模型自己崩的。退避 2min 是给崩溃重试的；
                # 硬停应该尽快续上（wake fast-path 60s 内 re-wake 让模型接着干）。
                _hard_stop = (
                    signals.get("last_turn_at") is not None
                    and reason == "turn_stale"
                    and (float(signals.get("turn_age_s") or 0)) < 180
                )
                retry_delay_min = 1 if _hard_stop else 2
                if _hard_stop:
                    log.info(
                        "L4: hard-stop detected (turn_age=%.0fs < 180s) — short retry in %dmin for resume",
                        float(signals.get("turn_age_s") or 0), retry_delay_min,
                    )
                retry_at = (now_dt() + timedelta(minutes=retry_delay_min)).isoformat(timespec="seconds")
                set_wait_intent(
                    life_aid,
                    WaitIntent(
                        wait_type=WaitType.UNTIL,
                        resume_when=retry_at,
                        reason="stale_running_rollback",
                        resume_action="",
                        meta={},
                    ),
                )
                # 不再 set_alarm('stale_running_rollback')。
                # 设计文档 5.5 / 22.1：闹钟只管「到没到点」，事件队列只管
                # 「有没有待处理」。affair 已回 BLOCKED，下一轮 cron tick（60s）
                # 会自然重扫事件队列；若上一轮 wake 真的崩了，它的失败分支自己
                # 会用 delay_pending_events 把 pending events 带退避推到未来，
                # 不需要这里额外再喊一次闹钟。

                # 收尾 session：写 ended_at + 内存 _last_session_end
                # 让下一次 wake 在 15min 内能命中 continuation
                try:
                    from domain.lifecycle.scheduler import _last_session_end
                    from domain.lifecycle.clock import now_dt as _now_dt
                    # session_id 可能在 affair.meta_json 里 or scheduler 内部
                    # 安全做法：从 SessionDB 查最近未结束的 session
                    from infrastructure.ai.session_db import SessionDB
                    sdb = SessionDB()
                    row = sdb._conn.execute(
                        "SELECT id FROM sessions WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 1"
                    ).fetchone()
                    if row:
                        sdb.end_session(row["id"], "stale_running_rollback")
                        _last_session_end[instance_id or ""] = {
                            "session_id": row["id"],
                            "at": _now_dt(),
                        }
                        log.info("L4: stale rollback closed session %s", row["id"])
                except Exception as exc:
                    log.debug("stale rollback session close failed: %s", exc)
            except Exception as exc:
                log.debug("RUNNING timeout check failed: %s", exc)

        # mid-session 注入已经在 emit_event 内 inline 完成（_signal_event_to_running_session），
        # 不再在这里单独 _inject_events——所有事件来源（人/bot/alarm）经过 emit 都会
        # 立即让 RUNNING 实例的信号队列看到。stale 检测 + vitals tick 已在上方
        # evaluate_wake_alive 分支内处理完毕(alive → vitals.tick + return；
        # dead → 回退 BLOCKED 后落到下方 BLOCKED 路径)。

        if not aff or aff.status != AffairStatus.BLOCKED.value:
            return

        # ── BLOCKED: 各子系统自行检查 → 推事件到队列 ──────────────────────────

        try:
            from domain.lifecycle.alarms import fire_due_alarms
            fired = fire_due_alarms()
            if fired:
                log.debug("L4: %d alarms fired", len(fired))
        except Exception as exc:
            log.debug("L4: alarm fire failed: %s", exc)

        try:
            from domain.vital.simulation import get_engine, reset_engine
            reset_engine()
            get_engine().tick(state="BLOCKED")
        except Exception as exc:
            log.debug("L4: vitals tick (BLOCKED) failed: %s", exc)

        try:
            from domain.lifecycle.routine_scheduler import ensure_routine_events
            ensure_routine_events()
        except Exception:
            pass

        # 任务惯性：任务系统自己判断是否推 task_momentum 事件
        try:
            from domain.todos import check_task_momentum
            momentum = check_task_momentum()
            if momentum:
                from domain.lifecycle.events import emit_event
                emit_event(kind="task_momentum", payload=momentum)
                log.info("L4: task_momentum event emitted")
        except Exception as exc:
            log.debug("L4: task_momentum check failed: %s", exc)

        # ── 拉取事件队列 ─────────────────────────────────────────────────────

        from domain.lifecycle.events import pop_due_events

        events = pop_due_events(limit=50)
        if not events:
            log.debug("L4: no due events — sleeping")
            return

        # ⚠ 诊断日志[tick决策]：群消息穿透排查用。零行为变更。
        log.info(
            "TICK_POP instance=%s count=%d details=%s",
            instance_id, len(events),
            [(e.get("event_id"), e.get("kind"),
              (e.get("payload") or {}).get("sender_name"),
              (e.get("payload") or {}).get("chat_id"))
             for e in events],
        )

        # All events are equal — no special filtering. Let the trigger chain
        # and wake prompt decide how to present them.

        # ── 确定唤醒原因 ───────────────────────────────────────────────────────
        # 抽到 domain/lifecycle/wakeup_policy.choose_reason(refactor/emit-driven-wake)。
        # cron tick 和 events.emit→_wake_or_inject 共用同一份优先级判断——
        # "事件平权"的产品语义在这里落实(决策点只有一份)。
        from domain.lifecycle.wakeup_policy import choose_reason

        reason = choose_reason(events)

        log.info("L4: waking — reason=%s events=%d", reason, len(events))
        log.info(
            "TICK_WAKE decision instance=%s reason=%s top_event=%s all_event_ids=%s",
            instance_id, reason,
            [(e.get("event_id"), e.get("kind"))
             for e in events if e.get("kind") == reason][:3],
            [e.get("event_id") for e in events],
        )

        # ── Token 预算闸门（基础设施级硬保护）─────────────────────────────
        # 不管事件优先级高低、精力是否充分，"这个小时/今日还能烧 token 么"
        # 由 budget_gate 决定。超阈值时拒绝 wake，把当前事件推到下个窗口
        # （delay_pending_events 复用现有退避路径，不发 retry alarm）。
        # **真人消息（message/group_message）+ 出生事件穿透闸门**，确保用户
        # 主动 @ 时永远能叫醒。
        # 设计依据（详见 docs/design 二十三章）：06-14 一夜 ~130MB 日志 +
        # GLM 配额爆掉死循环，根因是缺乏"还能不能烧"的基础设施级闸门。
        try:
            from infrastructure.budget import should_allow_wake
            from domain.lifecycle.events import delay_pending_events
            allowed, _gate_reason, _state = should_allow_wake(reason, instance_id or "")
            if not allowed:
                log.warning(
                    "L4: budget gate refusing wake for %s — events pushed to next window; "
                    "high-priority (message/group_message/birth) bypasses",
                    instance_id or "<?>",
                )
                # 复用事件自退避：把整批 events 推到 +5 分钟（保守 short backoff，
                # 比方说下个 cron tick 又看到的概率小）。token 窗口重置时自然恢复。
                try:
                    delay_pending_events(events, base=5.0, cap=5.0)
                except Exception as exc:
                    log.debug("budget-gate delay failed: %s", exc)
                return
        except Exception as exc:
            log.debug("budget gate check failed (let through): %s", exc)

        # ── 账号级熔断闸门（按 api_key 分区，跨实例共享）─────────────────────
        # budget_gate 只管"该实例 token 用量超没超"，不感知账号级 429。当某把
        # key 收到 429 被 trip 后，共用该 key 的所有实例（含本实例后续 cron tick）
        # 必须在此停下——否则限流期间每个 tick 仍会 pop events 起 wake 撞 429。
        # 与 budget_gate 不同：熔断闸**不区分事件优先级**（429 说明账号额度真炸，
        # 真人消息打进来也是 429），所以此处不保留 HIGH_PRIORITY_KINDS 穿透。
        # 已 pop 的 events 推到 +5 分钟，让熔断到期后下个 tick 自然 pop 接管。
        try:
            from infrastructure.budget.circuit_breaker import is_tripped
            from infrastructure.ai import load_runtime_config, resolve_runtime_provider
            from domain.lifecycle.events import delay_pending_events as _delay_for_cb
            _rt = resolve_runtime_provider(config=load_runtime_config())
            _cb_tripped, _cb_info = is_tripped(_rt.get("api_key") or "")
            if _cb_tripped:
                log.warning(
                    "L4: circuit breaker tripped for %s until %s "
                    "(tripped_by=%s) — delaying ALL %d events incl. high-priority",
                    instance_id or "<?>",
                    _cb_info.get("expires_at"),
                    _cb_info.get("tripped_by"),
                    len(events),
                )
                try:
                    _delay_for_cb(events, base=5.0, cap=5.0)
                except Exception as exc:
                    log.debug("circuit-breaker delay failed: %s", exc)
                return
        except Exception as exc:
            log.debug("circuit breaker check failed (let through): %s", exc)

        # ── Dispatch ──────────────────────────────────────────────────────────

        try:
            from domain.lifecycle.scheduler import wake_digital_life

            _captured_instance_id = instance_id

            def _bg_wake() -> None:
                if _captured_instance_id:
                    os.environ["DIGITAL_LIFE_INSTANCE_ID"] = _captured_instance_id
                    try:
                        from infrastructure.config import set_current_instance_id
                        set_current_instance_id(_captured_instance_id)
                    except Exception:
                        pass
                    try:
                        from domain.lifecycle.events import set_instance_context
                        set_instance_context(_captured_instance_id)
                    except Exception:
                        pass
                try:
                    wake_digital_life(life_aid, reason, extra="", pending_events=events)
                except Exception as exc:
                    log.warning("L4 background wake_digital_life error: %s", exc)

            thread = threading.Thread(target=_bg_wake, daemon=True)
            thread.start()
            log.info("L4: wake_digital_life dispatched (instance=%s, reason=%s)", instance_id, reason)
        except Exception as exc:
            log.warning("L4 wake_digital_life dispatch failed: %s", exc)

    except _SkipL4:
        return
    except Exception as exc:
        log.warning("L4 event check failed: %s", exc)
