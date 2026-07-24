"""System-level HTTP API for the **global** management console.

这些 endpoint 跨实例：不带 employee_id prefix，不染 ContextVar。提供：
- 全局总览（所有实例状态聚合）：``GET /api/system/overview``
- 实例管理（卡片元数据 avatar/color/tagline）：``GET/PATCH /api/system/instances[/{iid}]``
- 项目（全局共享）：``GET /api/system/projects[?iid=...]``
- 共享能力 / 技能市场：``GET /api/system/skills`` + ``POST /api/system/skills/subscribe``
- 事件类型注册表（CRUD）：``GET/POST/PUT/DELETE /api/system/event-types``

所有写路径都打在全局文件 / 实例 app.yaml 上（不通过 ContextVar 染色）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import yaml
from aiohttp import web

from application.contracts import UseCaseResult
from infrastructure.config import (
    _load_registry,  # type: ignore[attr-defined]
    discover_active_instances,
    discover_instances,
    get_project_root,
    is_instance_active,
)

logger = logging.getLogger("digital_life.system_api")


SYSTEM_API_PREFIX = "/api/system"


# ────────────────────────────────────────────────────────────────────────────
# 注册入口
# ────────────────────────────────────────────────────────────────────────────


def add_system_routes(app: web.Application) -> None:
    """Mount all /api/system/* routes + SPA fallback on master's HTTP server."""
    app.router.add_get(f"{SYSTEM_API_PREFIX}/overview", _handle_overview)
    app.router.add_get(f"{SYSTEM_API_PREFIX}/instances", _handle_instances)
    app.router.add_post(f"{SYSTEM_API_PREFIX}/instances", _handle_create_instance)
    app.router.add_patch(f"{SYSTEM_API_PREFIX}/instances/{{iid}}", _handle_update_instance)
    app.router.add_delete(f"{SYSTEM_API_PREFIX}/instances/{{iid}}", _handle_delete_instance)
    app.router.add_post(
        f"{SYSTEM_API_PREFIX}/instances/{{iid}}/wechat-login/qrcode",
        _handle_wechat_qrcode,
    )
    app.router.add_get(
        f"{SYSTEM_API_PREFIX}/instances/{{iid}}/wechat-login/status",
        _handle_wechat_login_status,
    )
    # 后端代理 ClawBot 二维码页面（绕过 X-Frame-Options: DENY）
    app.router.add_get(
        f"{SYSTEM_API_PREFIX}/instances/{{iid}}/wechat-login/qr-page",
        _handle_wechat_qr_page,
    )
    app.router.add_get(f"{SYSTEM_API_PREFIX}/projects", _handle_projects)
    app.router.add_post(f"{SYSTEM_API_PREFIX}/projects", _handle_create_project)
    app.router.add_delete(f"{SYSTEM_API_PREFIX}/projects/{{pid}}", _handle_delete_project)
    app.router.add_get(f"{SYSTEM_API_PREFIX}/projects/{{pid}}", _handle_project_detail)
    app.router.add_get(f"{SYSTEM_API_PREFIX}/projects/{{pid}}/tasks", _handle_project_tasks)

    # Affair 运维：reset blocked / abort stuck wake（schema-agnostic 一键解卡）
    app.router.add_post(
        f"{SYSTEM_API_PREFIX}/instances/{{iid}}/affairs/reset", _handle_reset_affair
    )
    app.router.add_post(
        f"{SYSTEM_API_PREFIX}/instances/{{iid}}/wakes/{{wake_id}}/abort",
        _handle_abort_wake,
    )

    # Gateway 运维
    app.router.add_post(f"{SYSTEM_API_PREFIX}/gateway/restart", _handle_gateway_restart)
    app.router.add_post(
        f"{SYSTEM_API_PREFIX}/instances/{{iid}}/active",
        _handle_toggle_instance_active,
    )
    app.router.add_get(f"{SYSTEM_API_PREFIX}/skills", _handle_skills_catalog)
    app.router.add_post(f"{SYSTEM_API_PREFIX}/skills/subscribe", _handle_skill_subscribe)

    app.router.add_get(f"{SYSTEM_API_PREFIX}/event-types", _handle_list_event_types)
    app.router.add_post(f"{SYSTEM_API_PREFIX}/event-types", _handle_create_event_type)
    app.router.add_put(
        f"{SYSTEM_API_PREFIX}/event-types/{{type_id}}", _handle_update_event_type
    )
    app.router.add_delete(
        f"{SYSTEM_API_PREFIX}/event-types/{{type_id}}", _handle_delete_event_type
    )

    # 汇总多实例 token 走势 —— 跟单实例 /budget/series 同输出结构
    # 实现: 遍历 apps/*/data/state.db 各跑 budget_log 聚合 SQL, 内存按 at_iso 累加
    app.router.add_get(f"{SYSTEM_API_PREFIX}/budget/series", _handle_system_budget_series)

    # 静态资源：实例 avatar / 动图（仅 GET，相对 apps/{iid}/assets/）
    app.router.add_get(
        r"/employee/{iid:[0-9a-f-]+}/assets/{filename:.*}", _handle_instance_asset
    )

    # 前端构建产物：/assets/* → dist/assets/*
    dist_dir = (
        get_project_root() / "interfaces" / "web" / "employee-console" / "dist"
    )
    if dist_dir.is_dir():
        # assets 子目录（ hashed bundle + css + js + 图片）
        assets_dir = dist_dir / "assets"
        if assets_dir.is_dir():
            app.router.add_static("/assets/", path=str(assets_dir), name="spa-assets")
        # 顶层 favicon.ico / 其它根级静态资源
        for fname in ("favicon.ico", "robots.txt"):
            f = dist_dir / fname
            if f.is_file():
                app.router.add_get(f"/{fname}", _make_static_handler(f))

    # SPA fallback：所有 /system, /instance, /legacy, /system/*, /instance/*, /legacy/*
    # 都服务前端 index.html（路由交给 vue-router 处理；未被精确匹配兜底到这里）
    app.router.add_get("/system", _handle_spa)
    app.router.add_get("/system/{tail:.*}", _handle_spa)
    app.router.add_get("/instance", _handle_spa_root_redirect)
    app.router.add_get("/instance/{tail:.*}", _handle_spa)
    app.router.add_get("/legacy/{tail:.*}", _handle_spa)
    # 简短 alias：/ → /system
    app.router.add_get("/", _handle_root_redirect)

    logger.info("System routes registered under %s", SYSTEM_API_PREFIX)


# ────────────────────────────────────────────────────────────────────────────
# Overview —— 全部实例状态聚合
# ────────────────────────────────────────────────────────────────────────────


async def _handle_system_budget_series(request: web.Request) -> web.Response:
    """GET /api/system/budget/series?hours=24&bucket=hour|day

    汇总多实例的 token 用量走势 —— 跟单实例的 GET /api/employee/{iid}/budget/series
    输出结构相同(`buckets / day_total_used / hour_used / hours / bucket`), 便于前端
    OverviewView 直接复用 OverviewTab 的图表渲染逻辑。

    实现: 遍历 apps/*/data/state.db, 每个 db 跑跟 token_tracker.usage_series 同款
    的 SQL, 内存按 at_iso key 累加各字段(formula: 每个 bucket 的 input/output/total/
    total_summary 等字段累加, count_429 累加)。

    day_total_used / hour_used 从聚合后的 buckets 二次求和得来。
    """
    try:
        q = request.query or {}
        try:
            hours = int(q.get("hours", "24"))
        except ValueError:
            hours = 24
        bucket = (q.get("bucket") or "hour").strip()
        bucket_fmt = {
            "hour": "%Y-%m-%dT%H:00",
            "day": "%Y-%m-%d",
            "minute": "%Y-%m-%dT%H:%M",
        }.get(bucket, "%Y-%m-%dT%H:00")
        import sqlite3
        import time
        from infrastructure.config import get_project_root
        since = time.time() - hours * 3600

        # 遍历每个实例的 state.db, 累加 budget_log
        # 用 dict 合并 at_iso → 各字段求和
        merged: dict[str, dict[str, int]] = {}
        apps_root = get_project_root() / "apps"
        if apps_root.exists():
            for entry in sorted(apps_root.iterdir()):
                if not entry.is_dir():
                    continue
                cfg_path = entry / "config" / "app.yaml"
                if not cfg_path.exists():
                    continue  # 不是有效实例(可能是 eval-sandbox 等)
                db_path = entry / "data" / "state.db"
                if not db_path.exists():
                    continue
                try:
                    conn = sqlite3.connect(str(db_path), timeout=2.0)
                    conn.row_factory = sqlite3.Row
                    try:
                        rows = conn.execute(
                            """
                            SELECT
                                strftime(?, occurred_at, 'unixepoch', '+8 hours') AS b,
                                kind,
                                COALESCE(SUM(input_tokens), 0)  AS s_in,
                                COALESCE(SUM(output_tokens), 0) AS s_out,
                                COALESCE(SUM(total_tokens), 0)  AS s_tot,
                                COUNT(*) AS cnt
                            FROM budget_log
                            WHERE occurred_at >= ?
                            GROUP BY b, kind
                            """,
                            (bucket_fmt, since),
                        ).fetchall()
                    finally:
                        conn.close()
                except Exception:
                    continue  # 该 db 不存在 budget_log 表或损坏 → 跳过
                # 合并到全局
                for r in rows:
                    b = r["b"]
                    bk = merged.setdefault(b, {
                        "at_iso": b, "input": 0, "output": 0, "total": 0,
                        "input_summary": 0, "output_summary": 0, "total_summary": 0,
                        "count_429": 0,
                    })
                    kind = r["kind"] or "llm_call"
                    if kind == "llm_call_429":
                        bk["count_429"] += int(r["cnt"])
                    elif kind == "session_summary":
                        bk["input_summary"] += int(r["s_in"])
                        bk["output_summary"] += int(r["s_out"])
                        bk["total_summary"] += int(r["s_tot"])
                    else:
                        bk["input"] += int(r["s_in"])
                        bk["output"] += int(r["s_out"])
                        bk["total"] += int(r["s_tot"])

        buckets_list = list(merged.values())
        # buckets 在 hour / minute 模式下应按时间正序输出(跟单实例一致的渲染)
        # day 也正序(echarts xAxis category 即可显示)
        buckets_list.sort(key=lambda x: x["at_iso"])
        # 求 day_total_used / hour_used (跨实例聚合后的今日/本时总数)
        import time as _t
        from datetime import datetime, timezone, timedelta
        BJ = timezone(timedelta(hours=8))
        now_bj = datetime.now(BJ)
        today_prefix = now_bj.strftime("%Y-%m-%d")
        hour_prefix = now_bj.strftime("%Y-%m-%dT%H:")
        day_total = 0
        hour_total = 0
        for b in buckets_list:
            at = b["at_iso"]
            if at.startswith(today_prefix):
                day_total += (b["total"] + b["total_summary"])
            if at.startswith(hour_prefix):
                hour_total += (b["total"] + b["total_summary"])

        return web.json_response({
            "buckets": buckets_list,
            "day_total_used": day_total,
            "hour_used": hour_total,
            "hours": hours,
            "bucket": bucket,
        })
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)


async def _handle_overview(request: web.Request) -> web.Response:
    """Return aggregated status across all instances.

    轻量实现：扫 registry + 逐个实例 state.db 取 vitals + 全局项目计数。
    未来可扩展为 master 从子进程收集上报。
    """
    try:
        instances = _instances_summary()
        projects = _list_projects_summary()
        return web.json_response(
            {
                "instances": instances,
                "instance_count": len(instances),
                "active_count": sum(1 for i in instances if i["active"]),
                "projects": projects,
                "project_count": len(projects),
            }
        )
    except Exception as exc:
        logger.exception("overview failed")
        return web.json_response({"error": str(exc)}, status=500)


def _instances_summary() -> list[dict[str, Any]]:
    """聚合每个实例的 display_name/meta + active + 当前活力 + 运行状态。

    三维度独立返回（runtime / process / health）+ 一个 visual 复合字段供前端
    直接识别用。

    runtime / process / health 互不影响 —— lifecycle 核心 RUNNING/BLOCKED 状态
    不会被任何一项改变（reset 也只动 health/wake 审计，不动 affairs.status）。
    """
    registry = _load_registry() or {}
    out: list[dict[str, Any]] = []
    for iid, meta in registry.items():
        active = is_instance_active(iid)
        energy, runtime_state, process_state, health_state, health_reason = (
            _read_instance_runtime_state(iid, active)
        )
        visual_state = _visual_state(runtime_state, process_state, health_state)
        out.append(
            {
                "id": iid,
                "display_name": meta.get("display_name") or iid[:8],
                "active": active,
                "avatar": meta.get("avatar", ""),
                "accent_color": meta.get("accent_color", ""),
                "tagline": meta.get("tagline", ""),
                "energy": energy,
                # 三维度独立返回，前端可各自读
                "runtime_state": runtime_state,   # lifecycle 状态（resting/working/idle）
                "process_state": process_state,    # 物理进程（offline/online）
                "health_state": health_state,      # 健康度（ok/error）—— 事件驱动
                "health_reason": health_reason,    # 失败原因 / 恢复提示
                # 通道状态：每个通道是否已连接（凭证齐全=connected，否则=unconfigured）
                "channels": _read_channel_status(iid),
                # 复合视觉态（前端 status 灯直接用这个）
                "status": visual_state,
            }
        )
    return out


def _parse_iso_to_epoch(iso: str) -> float:
    """ISO 时间字符串 → unix epoch（用于跨表时间戳对比）。

    flow_event_log_events.timestamp 存的是 TEXT（ISO，如 '2026-06-27T14:57:01.877659+00:00'），
    turn.timestamp 存的是 REAL（epoch）。health_state 的 auto-recover 判定需要把两者统一到 epoch
    比"最近成功 assistant turn 是否比 critical event 更新"。

    parse 失败（空串/非法/无法识别时区）→ 返回 -inf（负无穷），使 health 判定的
    `latest_successful_assistant_at >= critical_at` 保守走 "未恢复" 分支——
    即"无法确定 critical 时间时认为它最近、未恢复"，宁可多显示一次 error 也不误报为 ok。
    """
    if not iso:
        return float("-inf")
    try:
        from datetime import datetime, timezone

        # 兼容带 Z / 带偏移 / naive 三种 ISO 写法
        text = iso.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return float("-inf")


def _read_instance_runtime_state(iid: str, active: bool) -> tuple[float, str, str, str, str]:
    """读实例运行态 → (energy, runtime_state, process_state, health_state, health_reason)。

    **三维度独立观察**，互不干扰，避免动 lifecycle 核心 RUNNING/BLOCKED 状态机：

    ── 1. runtime_state (核心 lifecycle，只读不写) ──
      "working"  affair=RUNNING + 当前有 active wake
      "resting"  affair=BLOCKED/PENDING（数字生命主动等）或 energy<20
      "idle"     affair=RUNNING 无 active wake

    ── 2. process_state (物理进程是否在跑) ──
      "online"   active=True
      "offline"  active=False

    ── 3. health_state (执行健康度，事件驱动 / 不进 lifecycle) ──
      "ok"       最近一次模型调用成功 / 无严重事件
      "error"    最近一次模型调用失败 OR 有未恢复的 critical 事件

    health_state 完全基于两个事件源（不再用 db 间接信号 + 不需 cooldown file）：
      a) runtime_log.db.turn.error:   单次 LLM 调用 / 工具调用的失败
      b) state.db.flow_event_log_events.severity IN ('error','critical'): 事件流级别

    recovery 路径（自动）：
      - 任意一次 role=assistant 的 turn 无 error → ok（"模型成功" = 恢复）
      - 任意一次 severity=info 事件后 → 旧 error/critical 不再触发（按"最近一次"规则）

    前端根据这三维度组合出视觉状态（offline/error/resting/working/idle）。
    """
    # 1. process_state
    if not active:
        return 0.0, "resting", "offline", "ok", ""

    runtime_db = get_project_root() / "apps" / iid / "data" / "runtime_log.db"
    state_db = get_project_root() / "apps" / iid / "data" / "state.db"

    energy: float = 0.0
    affair_status: str = ""
    has_active_wake: bool = False
    health_state: str = "ok"
    health_reason: str = ""

    # 2. 读 state.db: energy + affair
    if state_db.exists():
        try:
            import sqlite3

            conn = sqlite3.connect(str(state_db))
            try:
                try:
                    row = conn.execute(
                        "SELECT energy FROM vitals ORDER BY rowid DESC LIMIT 1"
                    ).fetchone()
                    if row:
                        energy = float(row[0] or 0)
                except sqlite3.OperationalError:
                    pass
                try:
                    a_row = conn.execute(
                        "SELECT status FROM affairs ORDER BY updated_at DESC LIMIT 1"
                    ).fetchone()
                    if a_row:
                        affair_status = str(a_row[0] or "").upper()
                except sqlite3.OperationalError:
                    pass
            finally:
                conn.close()
        except Exception:
            pass

    # 3. 读 runtime_log.db: turns (active wake + 最近的成败) + flow events
    # 把"最近一次 turn" 和 "最近一次 error turn 的时间戳" 都取出来
    latest_turn_id: int = 0
    latest_turn_role: str = ""
    latest_turn_has_error: bool = False
    latest_turn_error_text: str = ""
    # 最近一次成功 assistant turn 的 epoch（用于 auto-recover 判定）。
    latest_successful_assistant_at: float = 0.0
    latest_critical_event_at_iso: str = ""
    latest_critical_event_summary: str = ""

    if runtime_db.exists():
        try:
            import sqlite3

            conn = sqlite3.connect(str(runtime_db))
            try:
                # 最近一次 turn（不论 role）
                try:
                    latest = conn.execute(
                        "SELECT id, role, COALESCE(error, ''), started_at IS NOT NULL "
                        "FROM turn ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                    # ↑ sqlite 不识别列名（started_at 不在 turn），换简单版
                except sqlite3.OperationalError:
                    latest = None
                # 上面语句会因列名错失败，使用下面这个稳的
                try:
                    latest = conn.execute(
                        "SELECT id, role, COALESCE(error, '') FROM turn ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                    if latest:
                        latest_turn_id = int(latest[0] or 0)
                        latest_turn_role = str(latest[1] or "")
                        latest_turn_error_text = str(latest[2] or "")
                        latest_turn_has_error = bool(latest_turn_error_text)
                except sqlite3.OperationalError:
                    pass

                # auto-recover 用的"最近一次成功 assistant turn"时间戳（REAL epoch）。
                # 只有 role=assistant 且无 error 的 turn 才算"模型成功"——user turn 成功
                # 不代表模型能工作。无成功 turn（实例从没跑过）→ 0.0，让恢复判定保守放行。
                # 见 health_state 判定段（"recovered"）。
                try:
                    succ_row = conn.execute(
                        "SELECT timestamp FROM turn "
                        "WHERE role='assistant' AND (error IS NULL OR error='') "
                        "ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                    if succ_row:
                        latest_successful_assistant_at = float(succ_row[0] or 0.0)
                except sqlite3.OperationalError:
                    pass

                # active wake：只看「最新一条 wake」的 ended_at。
                # 旧的失败 wake 可能因进程重启/异常留下 ended_at IS NULL 的孤儿，
                # 如果用 "任意一条 NULL" 判定 working，孤儿会永久把状态灯钉死。
                # 真正在跑的 wake 一定是 id 最大的那一条且尚未 end。
                try:
                    latest_wake_row = conn.execute(
                        "SELECT COALESCE(ended_at, 0) FROM wake "
                        "ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                    if latest_wake_row and float(latest_wake_row[0] or 0) == 0:
                        has_active_wake = True
                except sqlite3.OperationalError:
                    pass
            finally:
                conn.close()
        except Exception:
            pass

    # 3b. 读 state.db.flow_event_log_events: 最近 critical/error
    if state_db.exists():
        try:
            import sqlite3

            conn = sqlite3.connect(str(state_db))
            try:
                try:
                    evt = conn.execute(
                        "SELECT timestamp, summary FROM flow_event_log_events "
                        "WHERE severity IN ('error','critical') "
                        "ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                    if evt:
                        latest_critical_event_at_iso = str(evt[0] or "")
                        latest_critical_event_summary = str(evt[1] or "")
                except sqlite3.OperationalError:
                    pass
            finally:
                conn.close()
        except Exception:
            pass

    # 4. runtime_state（lifecycle 只读）
    if affair_status in ("BLOCKED", "PENDING"):
        runtime_state = "resting"
    elif energy < 20:
        runtime_state = "resting"
    elif has_active_wake:
        runtime_state = "working"
    else:
        runtime_state = "idle"

    # 5. health_state（事件驱动）
    # 优先级：未恢复的 critical 事件 > 最近 turn error > 默认 ok
    #
    # BUG 修复（2026-06-29）：原实现只看"DB 里是否有 severity=error 的 flow_event"就 error，
    # 导致实例一旦撞过 3 次失败写了 critical event，即便后来已恢复正常产出 assistant turn，
    # health 也永久 stuck 在 error（前端红灯钉死）。alpha/贝塔实测确认：今天 10:36/10:54
    # 已恢复成功 turn，但 health 仍 error。
    #
    # 正确语义（与模块 docstring :218-219 一致）：任意一次成功的 assistant turn
    # （比 critical event 更新）= 模型已恢复。用 epoch 对比跨表异构时间戳
    # （turn.timestamp=REAL, flow_event timestamp=TEXT ISO）。
    critical_at = _parse_iso_to_epoch(latest_critical_event_at_iso or "")
    # recovered = 最近成功 assistant turn 比 critical event 更新（含相等，保守起见视为已覆盖）
    # 仅当确实有过成功 assistant turn（>0）才算恢复——从没成功过的实例不能靠这个翻成 ok。
    recovered = (
        latest_successful_assistant_at > 0
        and latest_successful_assistant_at >= critical_at
    )
    if latest_critical_event_at_iso and not recovered:
        # 有未恢复的 critical 事件（最近成功 turn 早于故障）→ error
        health_state = "error"
        health_reason = (
            f"事件流严重错误：{latest_critical_event_summary or 'unknown'}"
            f"（{latest_critical_event_at_iso}）"
        )
    elif latest_critical_event_at_iso and recovered:
        # 故障已被后续成功 turn 覆盖 → 恢复成 ok，reason 留一个提示便于前端展示"已恢复"
        health_state = "ok"
        health_reason = (
            f"已恢复：最近成功 assistant turn（epoch={latest_successful_assistant_at:.0f}）"
            f"覆盖了历史故障（{latest_critical_event_at_iso}）"
        )
    elif latest_turn_has_error:
        health_state = "error"
        health_reason = (
            f"模型调用失败：{latest_turn_error_text[:200]}（turn #{latest_turn_id}, role={latest_turn_role}）"
        )
    else:
        health_state = "ok"
        if latest_turn_id:
            health_reason = f"无错误，最近成功 turn #{latest_turn_id}（role={latest_turn_role or '—'}）"
        else:
            health_reason = ""

    return energy, runtime_state, "online", health_state, health_reason


def _visual_state(runtime: str, process: str, health: str) -> str:
    """Three-axis state → single visual status for frontend.

    优先级：process=offline → 离线；health=error → 异常；
    其它 → 跟 runtime 一致（resting/working/idle）。
    runtime 字段（RUNNING/BLOCKED）原样保留，模型逻辑完全不受影响。
    """
    if process == "offline":
        return "offline"
    if health == "error":
        return "error"
    return runtime


def _status_label(visual: str) -> str:
    """前端展示用中文短名。"""
    return {
        "offline": "离线",
        "error": "异常",
        "resting": "休息中",
        "working": "工作中",
        "idle": "待命",
    }.get(str(visual or "idle"), str(visual or "—"))


def _read_channel_status(iid: str) -> list[dict[str, str]]:
    """读实例各通道的连接状态——有凭证=connected，无凭证=unconfigured。"""
    import yaml as _yaml
    import os as _os

    root = get_project_root()
    cfg_path = root / "apps" / iid / "config" / "app.yaml"
    secrets_path = root / "apps" / iid / "config" / "secrets.env"
    if not cfg_path.exists():
        return []
    try:
        cfg = _yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return []

    # 读 secrets.env
    secrets: dict[str, str] = {}
    if secrets_path.exists():
        for line in secrets_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                secrets[k.strip()] = v.strip().strip('"').strip("'")

    # 通道字段统一收敛到 channels.<type>
    channels = []

    # 飞书：channels.feishu.app_id
    ch_feishu = (cfg.get("channels") or {}).get("feishu") or {}
    feishu_app_id = str(ch_feishu.get("app_id") or "").strip()
    feishu_secret = secrets.get("FEISHU_APP_SECRET", "").strip()
    channels.append({
        "platform": "feishu",
        "label": "飞书",
        "status": "connected" if feishu_app_id and feishu_secret else "unconfigured",
        "identity": feishu_app_id[:16] + "…" if len(feishu_app_id) > 16 else feishu_app_id,
    })

    # 微信：WECHAT_BOT_TOKEN
    wechat_token = secrets.get("WECHAT_BOT_TOKEN", "").strip()
    ch_wechat = (cfg.get("channels") or {}).get("wechat") or {}
    wechat_bot_id = str(ch_wechat.get("bot_id") or "").strip()
    channels.append({
        "platform": "wechat",
        "label": "微信",
        "status": "connected" if wechat_token else "unconfigured",
        "identity": wechat_bot_id[:16] + "…" if len(wechat_bot_id) > 16 else wechat_bot_id,
    })

    return channels


# ────────────────────────────────────────────────────────────────────────────
# Instances —— 卡片元数据 Pillar
# ────────────────────────────────────────────────────────────────────────────


async def _handle_instances(request: web.Request) -> web.Response:
    """GET /api/system/instances —— 卡片完整字段（含 avatar/color/tagline/energy/status）。"""
    return web.json_response({"instances": _instances_summary()})


async def _handle_update_instance(request: web.Request) -> web.Response:
    """PATCH /api/system/instances/{iid} —— 改卡片元数据，写入 app.yaml。

    支持字段（按白名单过滤，防止越权写其他字段）：
      avatar / accent_color / tagline / display_name
    """
    iid = request.match_info["iid"]
    if iid not in (_load_registry() or {}):
        return web.json_response({"error": f"unknown instance: {iid}"}, status=404)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "expected JSON object"}, status=400)

    allowed = ("display_name", "avatar", "accent_color", "tagline")
    updates: dict[str, Any] = {}
    for key in allowed:
        if key in body:
            value = body[key]
            if value is None:
                value = ""
            text = str(value).strip()
            if key == "display_name" and not text:
                return web.json_response(
                    {"error": "display_name must not be empty"}, status=400
                )
            updates[key] = text

    if not updates:
        return web.json_response(
            {"error": "no updatable fields supplied (allowed: avatar, accent_color, tagline, display_name)"},
            status=400,
        )

    try:
        _patch_instance_app_yaml(iid, updates)
    except Exception as exc:
        logger.exception("instance update failed")
        return web.json_response({"error": str(exc)}, status=500)

    # 读回最新 registry
    registry = _load_registry()
    meta = registry.get(iid, {})
    return web.json_response(
        {
            "ok": True,
            "instance": {
                "id": iid,
                "display_name": meta.get("display_name") or iid[:8],
                "avatar": meta.get("avatar", ""),
                "accent_color": meta.get("accent_color", ""),
                "tagline": meta.get("tagline", ""),
            },
        }
    )


_wechat_login_state: dict[str, Any] = {}


async def _handle_wechat_qrcode(request: web.Request) -> web.Response:
    """POST /api/system/instances/{iid}/wechat-login/qrcode —— 获取二维码。

    ClawBot API 格式（来自 npm 包 @tencent-weixin/openclaw-weixin 2.4.4 源码）：
      POST /ilink/bot/get_bot_qrcode?bot_type=3  body={"local_token_list":[]}
      返回 {qrcode: "token", qrcode_img_content: "https://...", ret: 0}
    """
    iid = request.match_info["iid"]
    if iid not in (_load_registry() or {}):
        return web.json_response({"error": f"unknown instance: {iid}"}, status=404)

    import httpx

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://ilinkai.weixin.qq.com/ilink/bot/get_bot_qrcode?bot_type=3",
                json={"local_token_list": []},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.exception("wechat qrcode fetch failed")
        return web.json_response({"error": f"获取二维码失败: {exc}"}, status=502)

    if data.get("ret") not in (0, None):
        return web.json_response(
            {"error": f"ClawBot 返回错误: {data.get('err_msg', data.get('ret'))}"}, status=502
        )

    qrcode_token = data.get("qrcode") or ""
    qrcode_url = data.get("qrcode_img_content") or ""

    if not qrcode_token or not qrcode_url:
        return web.json_response(
            {"error": f"ClawBot 未返回二维码数据, response: {data}"}, status=502
        )

    _wechat_login_state[iid] = {"qrcode_token": qrcode_token, "status": "pending"}

    return web.json_response({"qrcode_url": qrcode_url, "qrcode_token": qrcode_token})


async def _handle_wechat_login_status(request: web.Request) -> web.Response:
    """GET /api/system/instances/{iid}/wechat-login/status —— 轮询扫码状态。

    ClawBot API 格式：
      GET /ilink/bot/get_qrcode_status?qrcode=xxx  (长轮询 hold 35s)
      返回 status: wait | scaned | need_verifycode | confirmed | expired

    confirmed 时返回 ilink_bot_id + bot_token。
    """
    iid = request.match_info["iid"]
    state = _wechat_login_state.get(iid)
    if not state:
        return web.json_response({"status": "pending"})

    if state.get("status") == "confirmed":
        return web.json_response({
            "status": "confirmed",
            "bot_id": state.get("bot_id", ""),
            "token_written": state.get("token_written", False),
        })

    qrcode_token = state.get("qrcode_token", "")
    if not qrcode_token:
        return web.json_response({"status": "pending"})

    import httpx

    try:
        async with httpx.AsyncClient(timeout=40) as client:
            resp = await client.get(
                f"https://ilinkai.weixin.qq.com/ilink/bot/get_qrcode_status?qrcode={qrcode_token}",
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return web.json_response({"status": "pending"})

    status = data.get("status", "")
    # 映射状态给前端理解
    if status == "confirmed":
        bot_token = data.get("bot_token") or ""
        bot_id = data.get("ilink_bot_id") or data.get("bot_id") or ""
        if bot_token:
            _write_env_secret(iid, "WECHAT_BOT_TOKEN", bot_token)
            try:
                _patch_instance_app_yaml(iid, {
                    "channels": {
                        "wechat": {
                            "type": "wechat_clawbot",
                            "domain": "https://ilinkai.weixin.qq.com",
                            "bot_id": bot_id,
                        }
                    }
                })
            except Exception:
                pass
            _wechat_login_state[iid] = {"status": "confirmed", "bot_id": bot_id, "token_written": True}
            return web.json_response({
                "status": "confirmed",
                "bot_id": bot_id,
                "token_written": True,
            })
    elif status == "scaned":
        return web.json_response({"status": "scaned", "message": "已扫码，等待确认…"})
    elif status == "need_verifycode":
        return web.json_response({"status": "need_verifycode", "message": "请在手机上输入验证码"})
    elif status == "expired":
        return web.json_response({"status": "expired", "message": "二维码已过期，请重新获取"})

    return web.json_response({"status": "wait"})


async def _handle_wechat_qr_page(request: web.Request) -> web.Response:
    """GET /api/system/instances/{iid}/wechat-login/qr-page?qrcode_url=xxx

    后端用 Python qrcode 库把 ClawBot 返回的 URL 编码成 PNG 图片。
    微信原始页面是 JS 渲染的 + X-Frame-Options: DENY——不能直接 iframe。
    """
    qr_url = request.query.get("qrcode_url") or ""
    if not qr_url:
        return web.Response(text="missing qrcode_url", status=400)

    import io
    import qrcode

    qr = qrcode.QRCode(version=1, box_size=8, border=2)
    qr.add_data(qr_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)

    return web.Response(body=buf.getvalue(), content_type="image/png")


def _write_env_secret(iid: str, key: str, value: str) -> None:
    """往实例 secrets.env 写入/更新一个 key。"""
    secrets_path = get_project_root() / "apps" / iid / "config" / "secrets.env"
    secrets_path.parent.mkdir(parents=True, exist_ok=True)
    lines = secrets_path.read_text(encoding="utf-8").splitlines() if secrets_path.exists() else []
    written = False
    out: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if stripped.startswith(f"{key}="):
            out.append(f"{key}={value}")
            written = True
        else:
            out.append(raw)
    if not written:
        out.append(f"{key}={value}")
    secrets_path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


def _patch_instance_app_yaml(iid: str, updates: dict[str, Any]) -> None:
    """把 updates 合并写入 apps/{iid}/config/app.yaml（整体重写）。"""
    from infrastructure.config import get_instance_config_path

    path = get_instance_config_path(iid)
    if not path.exists():
        raise FileNotFoundError(f"instance config not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"instance config is not a mapping: {path}")
    for key, value in updates.items():
        raw[key] = value
    path.write_text(
        yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )


# ────────────────────────────────────────────────────────────────────────────
# Projects —— 跨实例共享
# ────────────────────────────────────────────────────────────────────────────


async def _handle_projects(request: web.Request) -> web.Response:
    """GET /api/system/projects[?iid=xxx] —— 全部项目或某实例参与的项目。

    按 iid 过滤：复用 ProjectConfig.get_position_for_instance（positions.assignees
    含该 iid 才返回该 project）。
    """
    iid = (request.query.get("iid") or "").strip()
    try:
        from domain.project.loader import load_all_projects

        all_projects = load_all_projects() or {}
    except Exception as exc:
        logger.exception("projects load failed")
        return web.json_response({"error": str(exc)}, status=500)

    items: list[dict[str, Any]] = []
    for pid, cfg in all_projects.items():
        if iid:
            position = cfg.get_position_for_instance(iid)
            if position is None and cfg.manager != iid:
                continue  # 该实例不参与此项目，过滤掉
        items.append(_project_summary(pid, cfg, iid))

    return web.json_response({"projects": items, "project_count": len(items)})


def _instance_display_names(iids: list[str]) -> dict[str, str]:
    """Convert instance UUID list to {iid: display_name} lookup."""
    if not iids:
        return {}
    registry = _load_registry() or {}
    out = {}
    for iid in iids:
        if not iid:
            continue
        if iid in registry:
            out[iid] = registry[iid].get("display_name") or iid[:8]
        elif isinstance(iid, str) and not (iid.startswith("c-") or len(iid) >= 16):
            # 不是 UUID 形态（比如 'human:张三'）—— 原样作为 display name
            out[iid] = iid
        else:
            out[iid] = (iid or "").strip()[:8] + "…" if iid else "—"
    return out


def _project_summary(pid: str, cfg: Any, viewer_iid: str = "") -> dict[str, Any]:
    position_for_viewer = (
        cfg.get_position_for_instance(viewer_iid) if viewer_iid else None
    )
    manager_iid = str(getattr(cfg, "manager", "")).strip()
    # 收集所有 position 里的 assignee，做批量 name lookup（避免每个 position 都查一次）
    positions_raw = []
    all_iids = [manager_iid] if manager_iid else []
    for p in (getattr(cfg, "positions", []) or []):
        for a in (p.assignees or []):
            if str(a).strip() not in all_iids:
                all_iids.append(str(a).strip())
    name_lookup = _instance_display_names(all_iids)

    positions = []
    for p in (getattr(cfg, "positions", []) or []):
        assignees = [str(a).strip() for a in (p.assignees or []) if str(a).strip()]
        positions.append({
            "id": p.id,
            "name": p.name,
            "assignees": assignees,
            "assignee_names": [name_lookup.get(a, a[:8] + "…" if a else "—") for a in assignees],
        })

    return {
        "id": pid,
        "name": getattr(cfg, "name", pid),
        "description": getattr(cfg, "description", ""),
        "status": getattr(cfg, "status", "active"),
        "manager": manager_iid,
        "manager_name": name_lookup.get(manager_iid, manager_iid[:8] + "…" if manager_iid else "—"),
        "group_chat_id": getattr(cfg, "group_chat_id", ""),
        "positions": positions,
        "viewer_position": position_for_viewer.name if position_for_viewer else None,
    }


def _list_projects_summary() -> list[dict[str, Any]]:
    from domain.project.loader import load_all_projects

    all_projects = load_all_projects() or {}
    return [_project_summary(pid, cfg) for pid, cfg in all_projects.items()]


async def _handle_create_project(request: web.Request) -> web.Response:
    """POST /api/system/projects —— 新建项目。

    Body: {name, description?, manager, positions?:[{id,name,assignees[]}], group_chat_id?}
    使用 domain.project.manager.create_project_full 创建骨架（不 emit 事件；
    事件触发由前端另行决定或后续按需补；保持 system API 作为纯 CRUD）。
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "expected JSON object"}, status=400)

    name = str(body.get("name") or "").strip()
    if not name:
        return web.json_response({"error": "name is required"}, status=400)
    description = str(body.get("description") or "")
    manager = str(body.get("manager") or "").strip()
    group_chat_id = str(body.get("group_chat_id") or "").strip()
    raw_positions = body.get("positions") or []
    if not isinstance(raw_positions, list):
        raw_positions = []
    positions = [
        {
            "id": str(p.get("id") or p.get("name") or "").strip(),
            "name": str(p.get("name") or p.get("id") or "").strip(),
            "assignees": [
                str(a).strip() for a in (p.get("assignees") or []) if str(a).strip()
            ],
        }
        for p in raw_positions
        if isinstance(p, dict)
    ]
    # 必须有至少一个 position 给到 manager，否则 manager 拿不到角色
    if not any(manager in p["assignees"] for p in positions) and manager:
        positions.insert(
            0,
            {
                "id": "lead",
                "name": "Lead",
                "assignees": [manager],
            },
        )

    # project_id 用 name slug + 短随机后缀（避免冲突）
    import re
    import secrets

    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.lower()).strip("-") or "project"
    project_id = f"{slug}-{secrets.token_hex(3)}"

    try:
        from domain.project.manager import create_project_full

        ok = create_project_full(
            project_id, name, description, manager, positions, group_chat_id
        )
        if not ok:
            return web.json_response(
                {"error": "create_project_full returned False"}, status=500
            )
    except Exception as exc:
        logger.exception("project create failed")
        return web.json_response({"error": str(exc)}, status=500)

    # 读回完整结构返回
    from domain.project.loader import load_project

    cfg = load_project(project_id)
    return web.json_response(
        {"ok": True, "project": _project_summary(project_id, cfg) if cfg else {"id": project_id, "name": name}}
    )


async def _handle_create_instance(request: web.Request) -> web.Response:
    """POST /api/system/instances —— 新建实例（init_instance + 自动初始化）。

    Body: {display_name, tagline?, accent_color?, avatar?, feishu_app_id?, feishu_app_secret?, glm_api_key?}
    Returns: {ok, instance: {...}} + 提示重启网关后实例才自动 spawn。
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "expected JSON object"}, status=400)
    display_name = str(body.get("display_name") or "").strip()
    if not display_name:
        return web.json_response({"error": "display_name is required"}, status=400)

    try:
        from infrastructure.bootstrap.instance import init_instance
    except Exception as exc:
        logger.exception("init_instance import failed")
        return web.json_response(
            {"error": f"cannot import init_instance: {exc}"}, status=500
        )

    glm_api_key = str(body.get("glm_api_key") or "").strip()
    feishu_app_id = str(body.get("feishu_app_id") or "").strip()
    feishu_app_secret = str(body.get("feishu_app_secret") or "").strip()

    try:
        inst_path = init_instance(
            display_name,
            glm_api_key=glm_api_key,
            feishu_app_id=feishu_app_id,
            feishu_app_secret=feishu_app_secret,
        )
    except SystemExit as exc:
        return web.json_response({"error": f"init_instance aborted: {exc}"}, status=400)
    except Exception as exc:
        logger.exception("init_instance failed")
        return web.json_response({"error": str(exc)}, status=500)

    iid = inst_path.name
    # user 想给新实例带上的元数据（tagline/accent_color/avatar）写到 app.yaml
    extra_updates: dict[str, Any] = {}
    if body.get("tagline"):
        extra_updates["tagline"] = str(body["tagline"]).strip()
    if body.get("accent_color"):
        extra_updates["accent_color"] = str(body["accent_color"]).strip()
    if body.get("avatar"):
        extra_updates["avatar"] = str(body["avatar"]).strip()
    if extra_updates:
        try:
            _patch_instance_app_yaml(iid, extra_updates)
        except Exception:
            # 非关键：registry 已经能扫到 display_name，元数据失败不阻塞创建
            logger.warning("could not apply extra updates to %s", iid, exc_info=True)

    # 读回 registry 给完整 instance 字段
    registry = _load_registry() or {}
    meta = registry.get(iid, {})

    # 自动加入 last_active.json + 直接 spawn 子进程（不需要重启 gateway）
    spawn_ok = False
    spawn_error = ""
    try:
        from gateway.instance_supervisor import write_last_active, InstanceSupervisor
        # 把新实例追加到 last_active.json
        import json as _json
        path = get_project_root() / "var" / "run" / "last_active.json"
        existing = []
        if path.exists():
            try:
                existing = list(_json.loads(path.read_text(encoding="utf-8")).get("instances") or [])
            except Exception:
                existing = []
        if iid not in existing:
            existing.append(iid)
        write_last_active(existing)

        # 直接 spawn（如果 supervisor 存在的话）
        sup = request.app.get("supervisor")
        if sup and isinstance(sup, InstanceSupervisor):
            import asyncio as _aio
            await sup._spawn(iid)
            spawn_ok = True
        else:
            spawn_ok = False
            spawn_error = "supervisor not available in app context"
    except Exception as exc:
        spawn_error = str(exc)
        logger.warning("auto-spawn new instance %s failed: %s", iid, exc)

    result_hint = (
        f"新实例「{display_name}」已初始化"
        + (f"并自动启动（PID 已 spawn）。" if spawn_ok else
           f"。自动 spawn 失败（{spawn_error}），请重启网关 `digital-life restart`。")
        + " 首次唤醒前请确保 secrets.env 的 LLM_API_KEY 与 FEISHU_APP_SECRET 正确。"
    )

    return web.json_response({
        "ok": True,
        "instance": {
            "id": iid,
            "display_name": meta.get("display_name") or display_name,
            "tagline": meta.get("tagline", ""),
            "accent_color": meta.get("accent_color", ""),
            "avatar": meta.get("avatar", ""),
            "active": is_instance_active(iid),
            "spawned": spawn_ok,
            "hint": result_hint,
        },
    })


async def _handle_project_detail(request: web.Request) -> web.Response:
    """GET /api/system/projects/{pid} —— 项目详情示意 + tasks/workspace 概况 + memory 路径占位。"""
    pid = _safe_relative_name(request.match_info["pid"])
    if not pid:
        return web.json_response({"error": "invalid pid"}, status=400)
    from domain.project.loader import load_project

    cfg = load_project(pid)
    if not cfg:
        return web.json_response({"error": "project not found"}, status=404)

    summary = _project_summary(pid, cfg)
    # 项目本体里能看的额外字段（goal/kpis/thesis/reviews）
    summary["goal"] = getattr(cfg, "goal", "")
    summary["kpis"] = list(getattr(cfg, "kpis", []) or [])
    summary["thesis"] = getattr(cfg, "thesis", "")
    summary["review_schedule"] = getattr(cfg, "review_schedule", "")

    # 项目目录里有的内容
    project_dir = get_project_root() / "projects" / pid
    summary["workspace_dir"] = str(project_dir / "docs") if (project_dir / "docs").exists() else ""
    summary["memory_dir"] = str(project_dir / "memory") if (project_dir / "memory").exists() else ""
    summary["files"] = _scan_project_files(project_dir)

    # tasks 计数 — Phase 4:走 global_todos.db WHERE project_id=pid
    try:
        from domain.project.crud import list_deliverables
        tasks = list_deliverables(db=None, project_id=pid)
        status_counts: dict[str, int] = {}
        for t in tasks:
            s = str(t.get("status") or "open")
            status_counts[s] = status_counts.get(s, 0) + 1
        summary["task_count"] = len(tasks)
        summary["task_status"] = status_counts
    except Exception:
        summary["task_count"] = 0
        summary["task_status"] = {}

    return web.json_response({"project": summary})


def _scan_project_files(project_dir: Path) -> list[dict[str, str]]:
    """列出项目目录里的重要文件（docs/、memory/、data/、根级 *.md 等）。"""
    out: list[dict[str, str]] = []
    if not project_dir.exists():
        return out
    for root, dirs, files in __import__("os").walk(project_dir):
        # 跳过 data 目录的 sqlite/db；保留 docs/memory
        rel = str(Path(root).relative_to(project_dir)).replace("\\", "/")
        dirs[:] = [d for d in dirs if d not in {".git", "__pycache__"}]
        for f in files:
            if f.endswith((".db", ".sqlite", ".sqlite3")) and rel == "data":
                continue
            path = (Path(root) / f).relative_to(project_dir)
            out.append({
                "path": str(path).replace("\\", "/"),
                "size_bytes": (Path(root) / f).stat().st_size,
            })
    return out[:60]  # cap


async def _handle_project_tasks(request: web.Request) -> web.Response:
    """GET /api/system/projects/{pid}/tasks —— 项目下所有 deliverables。

    Phase 4 (2026-06-24): list_deliverables 内部走 global_todos.db,
    无项目本地 db。返回 WHERE project_id=pid AND linked_deliverable_id != '' 的 todo。
    """
    pid = _safe_relative_name(request.match_info["pid"])
    if not pid:
        return web.json_response({"error": "invalid pid"}, status=400)
    try:
        from domain.project.crud import list_deliverables
        tasks = list_deliverables(db=None, project_id=pid)
    except Exception as exc:
        logger.exception("list project tasks failed")
        return web.json_response({"tasks": [], "error": str(exc)})
    # 同时给每个 assignee_instance 配 display_name
    name_lookup = _instance_display_names([
        str(t.get("assignee_instance") or "") for t in tasks if t.get("assignee_instance")
    ])
    enriched = []
    for t in tasks:
        aid = str(t.get("assignee_instance") or "")
        t2 = dict(t)
        t2["assignee_name"] = name_lookup.get(aid, aid[:8] + "…" if aid else "—")
        enriched.append(t2)
    return web.json_response({"tasks": enriched, "task_count": len(enriched)})


async def _handle_reset_affair(request: web.Request) -> web.Response:
    """POST /api/system/instances/{iid}/affairs/reset —— 解卡：abort 卡住的 wake。

    **重要原则**：不动 lifecycle 核心 RUNNING/BLOCKED 状态。这条命令只做
    「清除健康态 error 的可能根因」—— 把 audit_DB 里 ended_at IS NULL 的
    长 wake 标 ended，使 health_state 在下次心跳时回落到 ok。

    affair 的 RUNNING/BLOCKED 切换继续由 lifecycle 模型自主决定（emit_wait /
    emit_done）—— 任何模型逻辑都不受这 endpoint 影响。

    Body 可选: {abort_stuck_wakes: bool = true, also_unblock_affair: bool = false}
    """
    iid = request.match_info["iid"]
    if iid not in (_load_registry() or {}):
        return web.json_response({"error": f"unknown instance: {iid}"}, status=404)
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body or {}
    abort_wakes = bool(body.get("abort_stuck_wakes", True))
    also_unblock = bool(body.get("also_unblock_affair", False))

    # 1. abort stuck wakes（默认做，无副作用）
    aborted_wakes = _abort_stuck_wakes(iid) if abort_wakes else 0

    # 1b. 注：health_state 是事件驱动的（基于 turn.error / flow_event severity），
    #     abort 完卡住的 wake 后，下次 poll 自动重判 → ok
    #     不需要手动 cooldown file 标记

    # 2. 也清 BLOCKED affair → RUNNING（默认不做，仅在用户主动确认时）
    affairs_unblocked = 0
    if also_unblock:
        try:
            from infrastructure.config import set_current_instance_id, reset_current_instance_id
            token = set_current_instance_id(iid)
            try:
                from domain.lifecycle.affairs.runtime import (
                    list_affairs, update_affair, clear_wait_intent,
                )
                from domain.lifecycle.state_machine import AffairStatus
                blocked = list_affairs(status=AffairStatus.BLOCKED)
                for aff in blocked:
                    aid = getattr(aff, "affair_id", None) or (aff.get("affair_id") if isinstance(aff, dict) else None)
                    if not aid:
                        continue
                    update_affair(aid, status=AffairStatus.RUNNING.value)
                    try:
                        clear_wait_intent(aid)
                    except Exception:
                        pass
                    affairs_unblocked += 1
            finally:
                reset_current_instance_id(token)
        except Exception as exc:
            logger.exception("also_unblock failed")

    return web.json_response({
        "ok": True,
        "instance_id": iid,
        "wakes_aborted": aborted_wakes,
        "affairs_unblocked": affairs_unblocked,
        "hint": (
            f"abort {aborted_wakes} 个卡住的 wake。"
            + (f" also reset {affairs_unblocked} 个 BLOCKED affair(s) → RUNNING。" if also_unblock else "")
            + " lifecycle 状态机的 RUNNING/BLOCKED 切换仍由模型自主决定。"
        ),
    })


def _abort_stuck_wakes(iid: str) -> int:
    """把 audit 库里 ended_at IS NULL 的旧 wake 全部标 ended（防止再被认成"进行中"）。

    wakes 表位于 ``apps/{iid}/data/runtime_log.db`` 的 ``wake`` 表（单数）。
    """
    try:
        import sqlite3
        import time

        runtime_db = get_project_root() / "apps" / iid / "data" / "runtime_log.db"
        if not runtime_db.exists():
            return 0
        conn = sqlite3.connect(str(runtime_db))
        try:
            cur = conn.cursor()
            try:
                cur.execute("UPDATE wake SET ended_at = ? WHERE ended_at IS NULL", (time.time(),))
                rowcount = cur.rowcount
                conn.commit()
                return rowcount
            except sqlite3.OperationalError:
                return 0
        finally:
            conn.close()
    except Exception:
        logger.warning("abort stuck wakes failed", exc_info=True)
    return 0


async def _handle_abort_wake(request: web.Request) -> web.Response:
    """POST /api/system/instances/{iid}/wakes/{wake_id}/abort —— 强制 abort 单个 wake。

    Body 可选: {reason: str}
    """
    iid = request.match_info["iid"]
    wake_id = request.match_info["wake_id"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    reason = str((body or {}).get("reason") or "aborted by console")

    try:
        import sqlite3
        import time

        runtime_db = get_project_root() / "apps" / iid / "data" / "runtime_log.db"
        if not runtime_db.exists():
            return web.json_response({"error": "runtime_log.db not found"}, status=404)
        conn = sqlite3.connect(str(runtime_db))
        try:
            cur = conn.cursor()
            try:
                cur.execute(
                    "UPDATE wake SET ended_at = ? WHERE id = ?",
                    (time.time(), wake_id),
                )
                if cur.rowcount == 0:
                    return web.json_response({"error": "wake not found"}, status=404)
                conn.commit()
            except sqlite3.OperationalError as exc:
                return web.json_response({"error": f"db error: {exc}"}, status=500)
        finally:
            conn.close()
    except Exception as exc:
        logger.exception("abort wake failed")
        return web.json_response({"error": str(exc)}, status=500)

    return web.json_response({
        "ok": True,
        "instance_id": iid,
        "wake_id": wake_id,
        "reason": reason,
    })


async def _handle_gateway_restart(request: web.Request) -> web.Response:
    """POST /api/system/gateway/restart —— 重启 gateway 主进程（含所有实例子进程）。

    实现机制：写一个 trigger 文件，master 主循环的 restart watcher 检测到后
    spawn 一个独立 session 的 `digital-life restart` 子进程——与 CLI 重启语义
    完全一致（stop 老 master → start 新 master），**裸跑环境也自重启**，
    不依赖 launchd/systemd 等外部 wrapper。

    Body 可选: {reason: str}
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    reason = str((body or {}).get("reason") or "manual trigger from console")

    # 写 trigger 文件（master restart watcher 检测到它后 spawn detached restart）
    trigger_path = get_project_root() / "var" / "run" / ".restart_requested"
    try:
        trigger_path.parent.mkdir(parents=True, exist_ok=True)
        import time
        trigger_path.write_text(
            f"reason={reason}\nrequested_at={time.time()}\n",
            encoding="utf-8",
        )
    except Exception as exc:
        logger.exception("write restart trigger failed")
        return web.json_response({"error": str(exc)}, status=500)

    return web.json_response({
        "ok": True,
        "reason": reason,
        "hint": "重启请求已写入 var/run/.restart_requested；master restart watcher 会检测并"
                " spawn 一个 `digital-life restart` 子进程完成 stop + start（与 CLI 重启同语义，"
                "裸跑也自重启，无需 launchd/systemd）。",
        "restart_in_seconds": 5,
    })


async def _handle_toggle_instance_active(request: web.Request) -> web.Response:
    """POST /api/system/instances/{iid}/active —— 设置实例 active 状态（online/offline 切换）。

    Body: {active: bool, reason?: str}
    active=false 时 master 不会 spawn 该实例的子进程；active=true 时下次 master
    tick / restart 后会自动 spawn。

    副作用：会写 apps/{id}/config/app.yaml 的 active 字段。如果当前实例子进程
    正在跑，会请求 supervisor 把它 terminate（不强制 kill）。
    """
    iid = request.match_info["iid"]
    if iid not in (_load_registry() or {}):
        return web.json_response({"error": f"unknown instance: {iid}"}, status=404)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict) or "active" not in body:
        return web.json_response({"error": "field 'active' required in body"}, status=400)
    target_active = bool(body.get("active"))
    reason = str(body.get("reason") or "")

    try:
        from infrastructure.config import set_instance_active
        previous = is_instance_active(iid)
        set_instance_active(iid, target_active)
    except Exception as exc:
        logger.exception("set_instance_active failed")
        return web.json_response({"error": str(exc)}, status=500)

    # 同步 last_active.json + spawn / stop 子进程
    spawn_action = ""
    try:
        import json as _json
        from gateway.instance_supervisor import write_last_active
        path = get_project_root() / "var" / "run" / "last_active.json"
        existing = []
        if path.exists():
            try:
                existing = list(_json.loads(path.read_text(encoding="utf-8")).get("instances") or [])
            except Exception:
                existing = []

        if target_active and iid not in existing:
            # active=true → 加入列表 → 即时 spawn
            existing.append(iid)
            write_last_active(existing)
            sup = request.app.get("supervisor")
            if sup:
                import asyncio as _aio
                await sup._spawn(iid)
                spawn_action = f"实例已 spawn（pid 已启动）。"
            else:
                spawn_action = "已加入 last_active，下次重启后 spawn。"
        elif not target_active and iid in existing:
            # active=false → 从列表移除 → 立即停子进程
            existing.remove(iid)
            write_last_active(existing)
            sup = request.app.get("supervisor")
            if sup and hasattr(sup, "stop"):
                try:
                    await sup.stop(iid)
                    spawn_action = "实例子进程已停止。"
                except Exception:
                    spawn_action = "已从 last_active 移除，进程自然回收。"
            else:
                spawn_action = "已从 last_active 移除，进程自然回收。"
    except Exception as exc:
        spawn_action = f"last_active 同步失败（{exc}）；重启网关后生效。"

    return web.json_response({
        "ok": True,
        "instance_id": iid,
        "previous_active": previous,
        "active": target_active,
        "reason": reason,
        "hint": (
            f"app.yaml.active 已改为 {target_active}。{spawn_action}"
        ),
    })


async def _handle_delete_project(request: web.Request) -> web.Response:
    """DELETE /api/system/projects/{pid} —— 删除项目。

    物理删除 projects/{pid}/ 目录（含 project.yaml + deliverables 物理文件等）。
    Phase 4 (2026-06-24) 后该目录不再含 todos.db —— 项目 todos 一直在
    global_todos.db(`${pid}` 命名空间下)。删项目时给这些 todos 标 cancelled,
    不删除 row（保留审计）。

    若想归档而非删除,建议把 status 设为 archived（archive_project）。
    """
    pid = _safe_relative_name(request.match_info["pid"])
    if not pid:
        return web.json_response({"error": "invalid pid"}, status=400)
    project_dir = get_project_root() / "projects" / pid
    if not project_dir.exists():
        return web.json_response({"error": "not found"}, status=404)
    try:
        import shutil

        shutil.rmtree(project_dir)
    except Exception as exc:
        logger.exception("project delete failed")
        return web.json_response({"error": str(exc)}, status=500)

    # cancel 关联实例 todos —— 不删，设 cancelled 留痕
    cancelled_count = _cancel_todos_for_deleted_project(pid)
    return web.json_response({
        "ok": True,
        "id": pid,
        "todos_cancelled": cancelled_count,
        "hint": (
            f"项目已删除。关联的 {cancelled_count} 个待办已标记为 cancelled（保留记录）。"
            if cancelled_count
            else "项目已删除。无关联待办。"
        ),
    })


async def _handle_delete_instance(request: web.Request) -> web.Response:
    """DELETE /api/system/instances/{iid} —— 彻底删除实例（不可恢复）。

    步骤：
      1. 校验 iid 在 registry 中存在
      2. 停子进程（graceful SIGTERM → SIGKILL 兜底，最多 10s）
      3. 从 var/run/last_active.json 移除条目
      4. 物理删除 apps/{id}/ 目录（含 state.db / runtime_log / memories /
         secrets.env / persona / config —— 全部不可恢复）

    不支持归档；如需保留请手动备份 apps/{id}/ 后再调本接口。
    """
    iid = request.match_info["iid"]
    if iid not in (_load_registry() or {}):
        return web.json_response({"error": f"unknown instance: {iid}"}, status=404)

    from infrastructure.config import get_instance_dir
    instance_dir = get_instance_dir(iid)
    # 二次检查：目录确实在 apps/ 下，防止 iid 是相对路径或绝对路径包含 ..
    try:
        apps_root = (get_project_root() / "apps").resolve()
        resolved = instance_dir.resolve()
        resolved.relative_to(apps_root)  # 抛 ValueError 表示越界
    except (ValueError, OSError):
        return web.json_response({"error": "invalid instance_id"}, status=400)
    if not instance_dir.exists():
        return web.json_response({"error": f"directory not found: {instance_dir}"}, status=404)

    stop_note = ""
    # 1. 停子进程
    sup = request.app.get("supervisor")
    if sup and hasattr(sup, "stop_instance"):
        try:
            stopped = await sup.stop_instance(iid)
            stop_note = "已停止" if stopped else "未运行"
        except Exception as exc:
            logger.warning("stop_instance %s failed during delete: %s", iid[:8], exc)
            stop_note = f"停止失败（{exc}），目录仍删除"

    # 2. 从 last_active.json 移除条目
    try:
        import json as _json
        from gateway.instance_supervisor import write_last_active
        path = get_project_root() / "var" / "run" / "last_active.json"
        existing: list[str] = []
        if path.exists():
            try:
                existing = list(_json.loads(path.read_text(encoding="utf-8")).get("instances") or [])
            except Exception:
                existing = []
        if iid in existing:
            existing.remove(iid)
            write_last_active(existing)
    except Exception as exc:
        logger.warning("last_active sync failed during delete: %s", exc)

    # 3. 物理删除 apps/{id}/ — 先统计大小用于反馈，再 rmtree
    size_note = ""
    try:
        import shutil
        total = 0
        for p in instance_dir.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
        shutil.rmtree(instance_dir)
        size_note = f"已清理 {total / 1024 / 1024:.1f} MB"
    except Exception as exc:
        logger.exception("instance delete failed")
        return web.json_response({"error": str(exc)}, status=500)

    return web.json_response({
        "ok": True,
        "instance_id": iid,
        "process": stop_note,
        "disk": size_note,
        "hint": "实例已彻底删除：进程已停止、apps/{id}/ 目录已物理移除。",
    })


def _cancel_todos_for_deleted_project(pid: str) -> int:
    """Phase 4:global_todos.db 是单一真相,直接 SQL UPDATE。

    旧版遍历每个 apps/<id>/data/todos/todos.db(实例 backup),Phase 4 后这些 backup 已删。
    """
    try:
        from infrastructure.persistence.global_todos import get_global_todos_db
        db = get_global_todos_db()
        cur = db.execute(
            "UPDATE todos SET status = 'cancelled', updated_at = ? "
            "WHERE project_id = ? AND status NOT IN ('cancelled','done','completed')",
            (_now_iso(), pid),
        )
        cancelled = cur.rowcount
        db.commit()
        db.close()
        return cancelled
    except Exception:
        logger.exception("cancel todos for deleted project failed")
        return 0


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ────────────────────────────────────────────────────────────────────────────
# Skills —— 市场目录 + 订阅
# ────────────────────────────────────────────────────────────────────────────


async def _handle_skills_catalog(request: web.Request) -> web.Response:
    """GET /api/system/skills —— 全局技能目录（来自 interfaces/skills + shared/skills）。

    每条目：name / scope (system|shared) / description（从 SKILL.md 取第一段）/ path。
    可选 query ``?iid=xxx``：在该实例下，额外补 ``subscribed: bool`` 字段。
    """
    iid = (request.query.get("iid") or "").strip()
    catalog = _build_skills_catalog()
    if iid:
        subscribed = _read_instance_skills(iid)
        for item in catalog:
            item["subscribed"] = item["name"] in subscribed

    return web.json_response({"skills": catalog, "skill_count": len(catalog)})


def _build_skills_catalog() -> list[dict[str, Any]]:
    """扫描 interfaces/skills/ + shared/skills/ 下每个目录，拼成 catalog。"""
    root = get_project_root()
    catalog: list[dict[str, Any]] = []

    for scope, base in (("system", root / "interfaces" / "skills"),
                        ("shared", root / "shared" / "skills")):
        if not base.exists() or not base.is_dir():
            continue
        for entry in sorted(base.iterdir()):
            if not entry.is_dir():
                continue
            if entry.name.startswith(".") or entry.name.startswith("_"):
                continue
            catalog.append(
                {
                    "name": entry.name,
                    "scope": scope,
                    "description": _extract_skill_description(entry),
                    "path": str(entry.relative_to(root)),
                }
            )
    return catalog


def _extract_skill_description(skill_dir: Path) -> str:
    """从 SKILL.md 提取 description：优先 front matter 的 description 字段，
    没有则用首段非标题文本。上限 160 字符。
    """
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return ""
    try:
        text = skill_md.read_text(encoding="utf-8")
    except Exception:
        return ""
    # 1. 优先解析 front matter 的 description
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end > 0:
            front = text[3:end]
            for line in front.splitlines():
                stripped = line.strip()
                if stripped.lower().startswith("description:"):
                    desc = stripped.split(":", 1)[1].strip().strip('"').strip("'")
                    if desc:
                        return desc[:160]
    # 2. fallback：首段非标题、非空行
    in_front = text.startswith("---")
    for ln in text.splitlines():
        stripped = ln.strip()
        if not stripped:
            continue
        if stripped == "---":
            in_front = not in_front
            continue
        if in_front:
            continue
        if stripped.startswith("#"):
            continue
        return stripped[:160]
    return ""


def _read_instance_skills(iid: str) -> set[str]:
    """读 apps/{iid}/config/app.yaml 的 skills 列表。

    schema 兼容 list[str] 和 list[{name, enabled, ...}] 双形态。
    """
    from infrastructure.config import get_instance_config_path

    path = get_instance_config_path(iid)
    if not path.exists():
        return set()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        items = raw.get("skills") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            return set()
        out: set[str] = set()
        for item in items:
            if isinstance(item, str):
                out.add(item.strip())
            elif isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                enabled = item.get("enabled", True)
                # enabled=False 的不视为订阅；老 list[str] 默认全部 enabled=True
                if name and enabled:
                    out.add(name)
        return out
    except Exception:
        return set()


async def _handle_skill_subscribe(request: web.Request) -> web.Response:
    """POST /api/system/skills/subscribe —— 给实例订阅 / 取消订阅技能。

    Body: {"instance_id": "...", "skill": "name", "subscribed": bool}
    写回 app.yaml 的 skills 列表（保持 list[str] 形态，简单可控；后续可升级为 list[dict]）。
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "expected JSON object"}, status=400)

    iid = str(body.get("instance_id") or "").strip()
    skill = str(body.get("skill") or "").strip()
    subscribed = bool(body.get("subscribed", True))
    if not iid or not skill:
        return web.json_response(
            {"error": "instance_id and skill are required"}, status=400
        )
    if iid not in (_load_registry() or {}):
        return web.json_response({"error": f"unknown instance: {iid}"}, status=404)

    try:
        current = _read_instance_skills(iid)
        if subscribed:
            current.add(skill)
        else:
            current.discard(skill)
        _patch_instance_app_yaml(iid, {"skills": sorted(current)})
    except Exception as exc:
        logger.exception("skill subscribe failed")
        return web.json_response({"error": str(exc)}, status=500)

    return web.json_response(
        {
            "ok": True,
            "instance_id": iid,
            "skill": skill,
            "subscribed": subscribed,
            "skills": sorted(current),
        }
    )


# ────────────────────────────────────────────────────────────────────────────
# Event types —— 注册表 CRUD（config/event-packages/{type_id}/manifest.yaml）
# ────────────────────────────────────────────────────────────────────────────


_ALLOWED_MANIFEST_KEYS = (
    "type_id",
    "display_name",
    "trigger_type",
    "prompt",
    "allowed_tools",
    "context_policy",
    "auth_policy",
)


async def _handle_list_event_types(request: web.Request) -> web.Response:
    """GET /api/system/event-types —— 合并两个事实源：

    1) config/event-packages/{type_id}/manifest.yaml（前端 CRUD 写入的、简化 schema）
    2) config/event_types.yaml（运行时事实源，完整 schema 含 wake_prompt/context_policy）

    每条返回统一字段；来自 schema (2) 的标 ``origin: legacy_yaml``，前端展示时
    区分"运行时事件 / 待迁移事件"。返回字段：

      origin:
        - "manifest"     —— 来自 event-packages/manifest.yaml（可前端 CRUD）
        - "legacy_yaml"  —— 来自 event_types.yaml（运行时事实源，前端只读，
                            迁移后变 "manifest"）
    """
    manifest_items = []
    packages = _event_packages_dir()
    if packages.exists():
        for manifest in sorted(packages.glob("*/manifest.yaml")):
            item = _read_event_manifest(manifest)
            item["origin"] = "manifest"
            manifest_items.append(item)

    legacy_items = _read_legacy_event_types()
    manifest_keys = {it["type_id"] for it in manifest_items}
    legacy_only = [it for it in legacy_items if it["type_id"] not in manifest_keys]
    for it in legacy_only:
        it["origin"] = "legacy_yaml"

    items = manifest_items + legacy_only
    return web.json_response({
        "event_types": items,
        "count": len(items),
        "manifest_count": len(manifest_items),
        "legacy_count": len(legacy_only),
    })


def _read_legacy_event_types() -> list[dict[str, Any]]:
    """从 config/event_types.yaml 读取，转成 manifest 兼容字段。"""
    path = get_project_root() / "config" / "event_types.yaml"
    if not path.is_file():
        return []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    types = raw.get("event_types") if isinstance(raw, dict) else None
    if not isinstance(types, dict):
        return []
    out: list[dict[str, Any]] = []
    for tid, body in types.items():
        if not isinstance(body, dict):
            continue
        # legacy 字段都成 list 转 csv 给前端
        tools = body.get("allowed_tools") or []
        if isinstance(tools, list):
            tools_str = ",".join(str(t) for t in tools)
        else:
            tools_str = str(tools)
        cp = body.get("context_policy") or {}
        cp_str = ",".join(f"{k}={v}" for k, v in cp.items()) if isinstance(cp, dict) else str(cp)
        out.append({
            "type_id": tid,
            "display_name": str(body.get("display_name") or "").strip(),
            "trigger_type": str(body.get("trigger_type") or "").strip(),
            "prompt": _truncate(body.get("wake_prompt") or "", 200),
            "allowed_tools": [t.strip() for t in tools_str.split(",") if t.strip()],
            "context_policy": cp_str[:200],
            "auth_policy": "",
            "path": "config/event_types.yaml",
        })
    return out


def _truncate(text: Any, n: int) -> str:
    s = str(text or "").strip()
    return s if len(s) <= n else s[:n] + "…"


async def _handle_create_event_type(request: web.Request) -> web.Response:
    """POST /api/system/event-types —— 新建 event-package。

    Body: {type_id, display_name, trigger_type, prompt, allowed_tools?, context_policy?, auth_policy?}
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "expected JSON object"}, status=400)
    type_id = str(body.get("type_id") or "").strip()
    if not type_id:
        return web.json_response({"error": "type_id required"}, status=400)
    safe_type = _safe_relative_name(type_id)
    if not safe_type:
        return web.json_response({"error": "invalid type_id"}, status=400)

    packages = _event_packages_dir()
    target = packages / safe_type / "manifest.yaml"
    if target.exists():
        return web.json_response(
            {"error": f"event type {safe_type} already exists"}, status=409
        )

    try:
        manifest = _build_manifest_dict(body, safe_type)
        _write_event_manifest(target, manifest)
    except Exception as exc:
        logger.exception("create event type failed")
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response({"ok": True, "event_type": _read_event_manifest(target)})


async def _handle_update_event_type(request: web.Request) -> web.Response:
    """PUT /api/system/event-types/{type_id} —— 更新或创建 manifest。"""
    type_id = _safe_relative_name(request.match_info["type_id"])
    if not type_id:
        return web.json_response({"error": "invalid type_id"}, status=400)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "expected JSON object"}, status=400)

    target = _event_packages_dir() / type_id / "manifest.yaml"
    try:
        existing = _read_event_manifest(target) if target.exists() else {}
        merged = {**existing, **{k: v for k, v in body.items() if k in _ALLOWED_MANIFEST_KEYS and v is not None}}
        merged["type_id"] = type_id  # type_id 不可改
        _write_event_manifest(target, merged)
    except Exception as exc:
        logger.exception("update event type failed")
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response({"ok": True, "event_type": _read_event_manifest(target)})


async def _handle_delete_event_type(request: web.Request) -> web.Response:
    """DELETE /api/system/event-types/{type_id} —— 删除 event-package 目录。"""
    type_id = _safe_relative_name(request.match_info["type_id"])
    if not type_id:
        return web.json_response({"error": "invalid type_id"}, status=400)
    target_dir = _event_packages_dir() / type_id
    if not target_dir.exists():
        return web.json_response({"error": "not found"}, status=404)
    try:
        import shutil

        shutil.rmtree(target_dir)
    except Exception as exc:
        logger.exception("delete event type failed")
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response({"ok": True, "type_id": type_id})


def _event_packages_dir() -> Path:
    return get_project_root() / "config" / "event-packages"


def _safe_relative_name(name: str) -> str:
    """Sanitize a path segment to prevent directory traversal."""
    cleaned = "".join(c for c in name.strip() if c.isalnum() or c in ("-", "_"))
    if not cleaned or cleaned in (".", ".."):
        return ""
    return cleaned


def _build_manifest_dict(body: dict[str, Any], type_id: str) -> dict[str, Any]:
    manifest: dict[str, Any] = {"type_id": type_id}
    for key in _ALLOWED_MANIFEST_KEYS:
        if key == "type_id":
            continue
        if key in body and body[key] is not None:
            value: Any
            if key == "allowed_tools":
                tools = body[key]
                if isinstance(tools, list):
                    value = ",".join(str(t).strip() for t in tools if str(t).strip())
                else:
                    value = str(tools).strip()
            elif key == "context_policy":
                cp = body[key]
                if isinstance(cp, dict):
                    value = ",".join(f"{k}={v}" for k, v in cp.items())
                else:
                    value = str(cp).strip()
            else:
                value = str(body[key]).strip()
            if value:
                manifest[key] = value
    return manifest


def _write_event_manifest(target: Path, manifest: dict[str, Any]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    text_lines = []
    for key in _ALLOWED_MANIFEST_KEYS:
        if key in manifest and manifest[key] not in (None, ""):
            text_lines.append(f"{key}: {manifest[key]}")
    target.write_text("\n".join(text_lines) + "\n", encoding="utf-8")


def _read_event_manifest(manifest_path: Path) -> dict[str, Any]:
    """读 manifest.yaml，schema 宽容：缺失字段补空串。"""
    data: dict[str, Any] = {}
    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        if isinstance(raw, dict):
            data = raw
    except Exception:
        pass
    return {
        "type_id": str(data.get("type_id") or manifest_path.parent.name).strip(),
        "display_name": str(data.get("display_name") or "").strip(),
        "trigger_type": str(data.get("trigger_type") or "").strip(),
        "prompt": str(data.get("prompt") or "").strip(),
        "allowed_tools": [
            t.strip() for t in str(data.get("allowed_tools") or "").split(",") if t.strip()
        ],
        "context_policy": str(data.get("context_policy") or "").strip(),
        "auth_policy": str(data.get("auth_policy") or "").strip(),
        "path": str(manifest_path.relative_to(get_project_root())),
    }


# ────────────────────────────────────────────────────────────────────────────
# 静态资源：实例 avatar / 动图
# ────────────────────────────────────────────────────────────────────────────


async def _handle_instance_asset(request: web.Request) -> web.Response:
    """GET /employee/{iid}/assets/{filename} —— 服务实例 assets 目录。

    路径已被 router 模式限定：filename 不能含 ``..``（aiohttp 默认灌入失败），
    这里再做一次 contained_path 校验防越界。
    """
    iid = request.match_info["iid"]
    filename = request.match_info["filename"]
    assets_dir = get_project_root() / "apps" / iid / "assets"
    # 防 traversal：filename 内合法字符以外的拒绝
    if not _is_safe_filename(filename):
        return web.json_response({"error": "invalid filename"}, status=400)
    file_path = (assets_dir / filename).resolve()
    try:
        contained = assets_dir.resolve()
        file_path.relative_to(contained)
    except ValueError:
        return web.json_response({"error": "out of bounds"}, status=400)
    if not file_path.is_file():
        return web.json_response({"error": "not found"}, status=404)
    return web.FileResponse(file_path)


def _is_safe_filename(filename: str) -> bool:
    if not filename:
        return False
    if "/" in filename or "\\" in filename or ".." in filename:
        return False
    return True


def _make_static_handler(file_path: Path):
    """Build a handler that serves a fixed static file."""
    async def _handler(request: web.Request) -> web.Response:
        return web.FileResponse(file_path)
    return _handler


# ────────────────────────────────────────────────────────────────────────────
# SPA fallback：send index.html for client-side routes
# ────────────────────────────────────────────────────────────────────────────


def _spa_index_path() -> Path:
    """前端 dist 根。和 employee_console_routes._web_root() 同一定位逻辑。"""
    return (
        get_project_root()
        / "interfaces"
        / "web"
        / "employee-console"
        / "dist"
        / "index.html"
    )


async def _handle_spa(request: web.Request) -> web.Response:
    """serve SPA index.html — client router takes over from there."""
    index = _spa_index_path()
    if not index.exists():
        return web.json_response(
            {"error": "frontend dist not built. run `npm run build` in interfaces/web/employee-console"},
            status=503,
        )
    return web.FileResponse(index)


async def _handle_spa_root_redirect(request: web.Request) -> web.Response:
    """/instance → /instance（router 会自己重定向到 overview，无 aid 也会去选择默认实例）。"""
    # 不做 redirect，直接服务 SPA，让前端 vue-router 决定（通常 redirect to /system 默认入口）
    return await _handle_spa(request)


async def _handle_root_redirect(request: web.Request) -> web.Response:
    """/ → /system（默认入口）。"""
    raise web.HTTPFound("/system")


__all__ = ["add_system_routes", "SYSTEM_API_PREFIX"]
