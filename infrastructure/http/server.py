"""Gateway 服务器 — 协调所有核心服务。

两种运行模式：
  - run_master_gateway(): HTTP server + InstanceSupervisor，不连飞书、不跑 cron
  - run_instance_gateway(instance_id): 单个实例的 FeishuAdapter + Cron，不起 HTTP

旧函数 run_gateway() 仍保留作为兼容入口（= master 模式但不 spawn 子进程）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

from aiohttp import web

logger = logging.getLogger("gateway")


def _instance_feishu_credentials(instance_id: str) -> tuple[str, str]:
    """从 apps/<id>/config/app.yaml + apps/<id>/config/secrets.env 读飞书凭证。

    app_id 从 app.yaml 的 channels.feishu.app_id 读（非敏感）。
    app_secret 从实例 secrets.env 读（敏感）——这里直接读文件，
    不依赖 load_runtime_dotenv（那个函数需要 ContextVar 已设置）。
    """
    import yaml
    from infrastructure.config import get_project_root
    root = get_project_root()
    cfg_path = root / "apps" / instance_id / "config" / "app.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    channels = cfg.get("channels") or {}
    feishu = channels.get("feishu") or {} if isinstance(channels, dict) else {}
    app_id = (feishu.get("app_id") or "").strip() if isinstance(feishu, dict) else ""
    app_secret = ""
    secrets_path = root / "apps" / instance_id / "config" / "secrets.env"
    if secrets_path.exists():
        for line in secrets_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("FEISHU_APP_SECRET="):
                app_secret = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    return app_id, app_secret


def _cleanup_stale_affair_on_startup(instance_id: str) -> None:
    """进程启动时清理 stale RUNNING affair。

    问题：mid-session到达的新消息事件会被 signal 到内存队列；如果进程在此之前 wake
    后 crash/重启，DB 里 affair.status=RUNNING，但内存队列丢失。

    判定语义与 cron tick 不同：这里是**进程边界**——本进程刚起,上个进程的 session
    driver 必然已死,所以无条件回退 RUNNING，不参考 turn 心跳（turn 心跳在重启瞬间可能
    还很新,但它属于已经死掉的上一个进程,不能据此认为 wake 还活着）。

    修法：立即回退 BLOCKED + 关闭 DB 中所有未关闭的 session。
    日志带上最近 turn/wake 时间,方便运维核对"这次回退是否必要"。
    """
    from domain.lifecycle.affairs.runtime import (
        get_affair, update_affair, now_iso,
    )
    from domain.lifecycle.state_machine import AffairStatus
    try:
        from domain.orchestration.lifecycle_orchestration.bootstrap.runtime import _find_life_affair
    except Exception:
        return

    life_aid = _find_life_affair()
    if not life_aid:
        return
    aff = get_affair(life_aid)
    if not aff or aff.status != AffairStatus.RUNNING.value:
        return

    # 采集最近 turn/wake 时间仅用于日志诊断(不参与判定:重启边界必回退)。
    last_turn_at = None
    last_wake_started_at = None
    try:
        from infrastructure.persistence.instance import get_audit
        audit = get_audit(instance_id)
        last_turn_at = audit.last_turn_at()
        last_wake_started_at = audit.last_wake_started_at()
    except Exception as exc:
        logger.debug("Startup stale rollback: audit lookup failed: %s", exc)

    logger.warning(
        "Instance %s startup: found stale RUNNING affair %s (updated %s) — rolling back "
        "(process restart boundary; last_turn_at=%s last_wake_started_at=%s are ignored here "
        "because they belong to the previous, now-dead process)",
        instance_id[:8], life_aid[:8], aff.updated_at,
        last_turn_at, last_wake_started_at,
    )
    update_affair(life_aid, status=AffairStatus.BLOCKED.value, updated_at=now_iso())

    # 关闭未关闭的 sessions，避免 session_events 表残留
    try:
        import sqlite3
        from infrastructure.config import get_runtime_state_db_path
        sdb_path = get_runtime_state_db_path()
        if sdb_path.exists():
            conn = sqlite3.connect(str(sdb_path))
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT id FROM sessions WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            conn.close()
            if row:
                from infrastructure.ai.session_db import SessionDB
                sdb = SessionDB()
                sdb.end_session(row["id"], "startup_stale_rollback")
                logger.info("Startup stale rollback closed session %s", row["id"])
    except Exception as exc:
        logger.debug("Startup stale rollback session close failed: %s", exc)


# ──────────────────────────────── Instance 子进程模式 ────────────────────────────────


async def run_instance_gateway(instance_id: str) -> None:
    """跑单个实例：消息通道 adapter(s) + Cron loop。不起 HTTP。

    通过 registry 从 app.yaml 配置创建一个或多个通道 adapter（飞书/微信/钉钉...）。
    旧格式（只有 messenger 段）自动兼容为单飞书通道。
    """
    from application.ingress.handler import handle_message as ingress_handle_message
    from infrastructure.config import set_current_instance_id, get_project_root
    from domain.lifecycle.events import set_instance_context
    from interfaces.ingress.registry import create_adapters_from_config
    import yaml as _yaml

    # 读实例 app.yaml + secrets.env
    _root = get_project_root()
    cfg_path = _root / "apps" / instance_id / "config" / "app.yaml"
    secrets_path = _root / "apps" / instance_id / "config" / "secrets.env"
    app_yaml_cfg = {}
    if cfg_path.exists():
        try:
            app_yaml_cfg = _yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except Exception:
            pass
    secrets_env = {}
    if secrets_path.exists():
        for line in secrets_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                secrets_env[k.strip()] = v.strip().strip('"').strip("'")

    # 创建所有通道 adapter（凭证不全的通道自动跳过，不报错）
    adapters = [a for a in create_adapters_from_config(app_yaml_cfg, secrets_env) if a is not None]
    if not adapters:
        logger.info("Instance %s has no configured channels yet (all credentials empty), skipping adapter start", instance_id[:8])

    # 设置进程级 ContextVar —— 这个进程只服务于这一个实例
    os.environ["DIGITAL_LIFE_INSTANCE_ID"] = instance_id
    try:
        set_current_instance_id(instance_id)
    except Exception:
        pass
    try:
        set_instance_context(instance_id)
    except Exception:
        pass

    # 注入运行时适配器
    try:
        from application.runtime_provider import configure_runtime_provider
        configure_runtime_provider()
    except Exception as exc:
        logger.warning("configure_runtime_provider failed at instance startup: %s", exc)

    # Startup stale 清理
    try:
        _cleanup_stale_affair_on_startup(instance_id)
    except Exception as exc:
        logger.warning("Instance %s startup stale cleanup failed: %s", instance_id[:8], exc)

    # 集中初始化所有 SQLite 表
    try:
        from domain.lifecycle.schema import init_all_schemas
        init_all_schemas()
    except Exception as exc:
        # StateDbCorruptError 是 state.db 主库损坏的硬故障——继续启动会变成
        # "EMIT 全 FAILED，事件被静默吞掉"的植物人。务必 ERROR 显眼，便于发现。
        from domain.lifecycle.schema import StateDbCorruptError

        if isinstance(exc, StateDbCorruptError):
            logger.error(
                "Instance %s STATE_DB_CORRUPT — continuing start in degraded mode; "
                "EMIT writes will fail until restored from backup. Detail: %s",
                instance_id[:8],
                exc,
            )
        else:
            logger.warning("Instance %s schema init failed: %s", instance_id[:8], exc)

    # 显式触发 memory_vectors.db schema migration (V2: payload/cog_key).
    # _get_db() 是幂等的,首次访问会 ALTER TABLE; 但实例可能早已存在但长期
    # 不调召回(只写认知),导致 schema 落后。这里启动时主动跑一次,避免跨实例
    # schema 不一致(2026-07-23 review #1 真实 BUG)。
    try:
        from domain.memory.memory.recall.vector import _get_db
        vdb = _get_db()
        vdb.close()
    except Exception as exc:
        logger.debug("Instance %s memory_vectors migration probe: %s", instance_id[:8], exc)

    stop_event = asyncio.Event()

    def _signal_handler(sig):
        logger.info("Instance %s received signal %s", instance_id[:8], sig)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler, sig)
        except NotImplementedError:
            pass

    # 启动所有通道 adapter
    started_adapters: list[Any] = []
    for adapter in adapters:
        adapter.on_message(
            lambda msg, a=adapter: ingress_handle_message(adapter=a, msg=msg)
        )
        await adapter.start()
        started_adapters.append(adapter)
        # 三态收条:把本进程的 adapter 注册到 reaction_state,
        # 这样 express_to_human 发送完不需要拿 adapter 引用也能撤 ⚙️
        if hasattr(adapter, "add_reaction"):
            try:
                from application.ingress.reaction_state import register_adapter
                register_adapter(adapter)
            except Exception:
                pass
        logger.info(
            "Instance %s adapter started (platform=%s, identity=%s)",
            instance_id[:8],
            getattr(adapter, "platform", "?"),
            getattr(adapter, "app_identity", "?"),
        )

    # 热加载通道：每 30s 检查一次配置/secrets 是否有变化（用户在控制台改了
    # 飞书 App ID / App Secret / 域名、微信扫码登录后，不需要重启网关即可生效）。
    # 两个维度都覆盖：1) 新平台出现  2) 同平台凭据/域名变化。
    import yaml as _yaml_reload
    from interfaces.ingress.registry import (
        channel_signatures as _channel_signatures,
        create_adapters_from_config as _caf,
    )

    def _load_reload_files() -> tuple[dict[str, Any], dict[str, str]]:
        cfg_path_r = get_project_root() / "apps" / instance_id / "config" / "app.yaml"
        secrets_path_r = get_project_root() / "apps" / instance_id / "config" / "secrets.env"
        if not cfg_path_r.exists():
            return {}, {}
        new_cfg = _yaml_reload.safe_load(cfg_path_r.read_text(encoding="utf-8")) or {}
        new_secrets: dict[str, str] = {}
        if secrets_path_r.exists():
            for line in secrets_path_r.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    new_secrets[k.strip()] = v.strip().strip('"').strip("'")
        return new_cfg, new_secrets

    # 初始签名快照（基于启动时磁盘文件）
    last_signatures = _channel_signatures(*_load_reload_files())

    async def _hot_reload_channels():
        from application.ingress.handler import handle_message as _hm
        from application.ingress.reaction_state import register_adapter
        reload_interval = 30
        while not stop_event.is_set():
            await asyncio.sleep(reload_interval)
            try:
                new_cfg, new_secrets = _load_reload_files()
                new_signatures = _channel_signatures(new_cfg, new_secrets)

                # ── 1) 找出需要重建的平台：签名变化。新平台出现也属于"签名新增"。
                platforms_to_rebuild: set[str] = set()
                for platform, sig in new_signatures.items():
                    if last_signatures.get(platform) != sig:
                        platforms_to_rebuild.add(platform)
                # ── 2) 凭据被清空/通道被移除：停止旧 adapter
                removed_platforms = {p for p in last_signatures if p not in new_signatures}
                for platform in removed_platforms:
                    platforms_to_rebuild.discard(platform)  # 单独处理：只 stop 不 start

                # 停掉被移除或凭据变化的旧 adapter
                if platforms_to_rebuild or removed_platforms:
                    stopped: list[Any] = []
                    for ad in list(started_adapters):
                        platform = getattr(ad, "platform", "")
                        if platform in platforms_to_rebuild or platform in removed_platforms:
                            try:
                                await ad.stop()
                            except Exception as exc:
                                logger.warning(
                                    "Instance %s hot-reload stop adapter %s failed: %s",
                                    instance_id[:8], platform, exc,
                                )
                            started_adapters.remove(ad)
                            stopped.append(ad)
                    if stopped:
                        logger.info(
                            "Instance %s hot-reload stopped %d adapter(s): %s",
                            instance_id[:8], len(stopped),
                            [getattr(a, "platform", "?") for a in stopped],
                        )

                # 起新 adapter（同一 platform 凭据变了或全新平台）
                # reaction_state.register_adapter 用单例覆盖；新 adapter 注册后将覆盖旧。
                if platforms_to_rebuild:
                    new_adapters = [a for a in _caf(new_cfg, new_secrets) if a is not None]
                    for new_ad in new_adapters:
                        platform = getattr(new_ad, "platform", "")
                        if platform not in platforms_to_rebuild:
                            continue
                        new_ad.on_message(
                            lambda msg, a=new_ad: _hm(adapter=a, msg=msg)
                        )
                        await new_ad.start()
                        started_adapters.append(new_ad)
                        if hasattr(new_ad, "add_reaction"):
                            try:
                                register_adapter(new_ad)
                            except Exception:
                                pass
                        logger.info(
                            "Instance %s HOT-RELOADED adapter: platform=%s identity=%s",
                            instance_id[:8],
                            platform,
                            getattr(new_ad, "app_identity", "?"),
                        )

                last_signatures.clear()
                last_signatures.update(new_signatures)
            except Exception as exc:
                logger.debug("hot reload check failed: %s", exc)

    hot_reload_task = asyncio.create_task(_hot_reload_channels())

    # 启动 Cron 循环（只 tick 当前实例）
    cron_stop = threading.Event()
    cron_thread = threading.Thread(
        target=_instance_cron_loop,
        args=(instance_id, cron_stop),
        name=f"cron-{instance_id[:8]}",
        daemon=True,
    )
    cron_thread.start()
    logger.info("Instance %s cron loop started", instance_id[:8])

    # 启动 WAL checkpoint 循环（防 Mac 睡眠打断自动 checkpoint 导致主库损坏）
    ck_stop = threading.Event()
    ck_thread = threading.Thread(
        target=_instance_wal_checkpoint_loop,
        args=(instance_id, ck_stop),
        name=f"walck-{instance_id[:8]}",
        daemon=True,
    )
    ck_thread.start()

    # 启动社交接管 daemon（可选 — 需要 OAuth 授权后的 refresh_token）
    takeover = None
    try:
        from interfaces.social.feishu_takeover import start_takeover_daemon
        takeover = start_takeover_daemon(instance_id)
        if takeover:
            logger.info("Instance %s social takeover daemon started", instance_id[:8])
    except Exception as exc:
        logger.debug("Instance %s social takeover not started: %s", instance_id[:8], exc)

    # 启动感知 daemon（可选 — 需 app.yaml perception.enabled=true + macOS 权限）
    # feature 003-perception：快捷键录屏录音 → 视觉理解 → 注入事件
    perception_daemon = None
    try:
        from infrastructure.perception.daemon import start_perception_daemon
        perception_daemon = start_perception_daemon(instance_id)
        if perception_daemon:
            logger.info("Instance %s perception daemon started", instance_id[:8])
    except Exception as exc:
        logger.debug("Instance %s perception not started: %s", instance_id[:8], exc)

    await stop_event.wait()
    logger.info("Instance %s shutting down...", instance_id[:8])

    for adapter in adapters:
        try:
            await adapter.stop()
        except Exception:
            pass
    cron_stop.set()
    cron_thread.join(timeout=5)
    ck_stop.set()
    ck_thread.join(timeout=5)
    if takeover:
        takeover.stop()
    if perception_daemon:
        try:
            perception_daemon.stop()
        except Exception:
            pass
    logger.info("Instance %s stopped", instance_id[:8])


def _run_freshness_decay(instance_id: str) -> None:
    """批量跑 slice freshness decay: 读所有 chunks → update_slice_dynamics → UPDATE 回库。

    让老记忆自然衰减新鲜度, 低 fresh 的自动归档 → 从后续召回候选池中淡出。
    每 6h 跑一次 (cron loop 内调度)。
    """
    import sqlite3 as _sqlite3
    from infrastructure.config import get_runtime_memories_dir
    db_path = get_runtime_memories_dir() / "memory_vectors.db"
    if not db_path.exists():
        return
    import time as _time
    now = _time.time()
    try:
        from domain.memory.memory.recall.unified.slice import update_slice_dynamics, Slice
        conn = _sqlite3.connect(str(db_path), timeout=5.0)
        conn.row_factory = _sqlite3.Row
        # 只处理未归档的 chunk(cognition_state IS NULL or NOT IN ('archived','replaced'))
        rows = conn.execute(
            "SELECT id, source, text, created_at, phase, authority, permanence, "
            "freshness, activation, verification, evidence_count, challenge_count, "
            "cognition_state FROM chunks "
            "WHERE cognition_state IS NULL OR cognition_state NOT IN ('archived','replaced')"
        ).fetchall()
        archived = 0
        updated = 0
        for r in rows:
            s = Slice(
                id=r["id"],
                body=r["text"] or "",
                source=r["source"] or "",
                chunk_hash="",
                phase=r["phase"] or "experience",
                source_kind="",
                created_at=r["created_at"] or now,
                session_id="",
                segment_index=0,
                derived_from="[]",
                derive_kind="",
                authority=r["authority"] or 0.5,
                permanence=r["permanence"] or 0.3,
                freshness=r["freshness"] or 1.0,
                activation=r["activation"] or 0.0,
                verification=r["verification"] or 0.0,
                evidence_count=r["evidence_count"] or 0,
                challenge_count=r["challenge_count"] or 0,
                cognition_state=r["cognition_state"] or None,
                supersede_by=None,
                entity_links="[]",
                attention_tokens="[]",
                provenance="",
            )
            result = update_slice_dynamics(s, now=now)
            changes = result.get("changes", {})
            if changes:
                set_parts = []
                params_list = []
                for col, val in changes.items():
                    set_parts.append(f"{col} = ?")
                    params_list.append(val)
                set_parts.append("freshness = ?")
                params_list.append(s.freshness)
                params_list.append(r["id"])
                conn.execute(
                    f"UPDATE chunks SET {', '.join(set_parts)} WHERE id = ?",
                    params_list,
                )
                updated += 1
                if result.get("archived"):
                    archived += 1
        conn.commit()
        conn.close()
        if updated > 0:
            logger.info("freshness_decay: %d chunks updated, %d archived (instance=%s)",
                        updated, archived, instance_id[:8])
    except Exception as exc:
        logger.debug("freshness_decay inner failed: %s", exc)


def _instance_cron_loop(instance_id: str, stop_event: threading.Event) -> None:
    """每个实例独立的 cron 循环。"""
    import time
    from infrastructure.config import set_current_instance_id, reset_current_instance_id
    from domain.lifecycle.events import set_instance_context, reset_instance_context
    from infrastructure.scheduler.cron_lifecycle import run_l4_tick

    interval = int(os.environ.get("L4_CRON_INTERVAL", "60"))
    logger.info("Instance %s cron loop tick interval=%ds", instance_id[:8], interval)

    _last_decay_run = 0.0
    _DECAY_INTERVAL = 6 * 3600  # 6h 跑一次 freshness decay

    while not stop_event.is_set():
        try:
            ctx1 = set_current_instance_id(instance_id)
            ctx2 = set_instance_context(instance_id)
            try:
                run_l4_tick(instance_id=instance_id)

                # 每 6h 跑一次 slice freshness decay (让老记忆自然衰减)
                now = time.time()
                if now - _last_decay_run > _DECAY_INTERVAL:
                    try:
                        _run_freshness_decay(instance_id)
                        _last_decay_run = now
                    except Exception as exc:
                        logger.debug("freshness decay failed: %s", exc)
            finally:
                reset_instance_context(ctx2)
                reset_current_instance_id(ctx1)
        except Exception as exc:
            logger.warning("Instance %s cron tick failed: %s", instance_id[:8], exc)
        # tick 间隔内分次 sleep，便于快速响应 stop
        slept = 0
        while slept < interval and not stop_event.is_set():
            time.sleep(min(5, interval - slept))
            slept += 5


def checkpoint_state_db(db_path: Path) -> tuple[int, int, int]:
    """对单个 SQLite 文件执行 PRAGMA wal_checkpoint(TRUNCATE)。

    返回 SQLite checkpoint 原生三元组 ``(busy, log_pages, checkpointed_pages)``：
    - busy != 0 表示有未结束的事务无法 checkpoint（不致命，下次再试）；
    - log_pages 归零意味着 WAL 被裁剪到 0，是健康状态。

    设计动机：Mac 频繁睡眠会让 SQLite 自动 checkpoint（默认 WAL > 1000 page 触发）
    在睡眠瞬断时写到一半 → 主库 b-tree 指针错乱（已 3 次复发）。本函数由后台
    daemon thread 周期性调用，把 WAL 在低风险时机主动清零，避免等到 4MB+ 大事务
    时被睡眠打断。IO 失败静默吞掉——别拖死 checkpoint loop。
    """
    import sqlite3

    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE);").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.debug("wal_checkpoint skipped for %s: %s", db_path, exc)
        return (1, -1, -1)
    except OSError as exc:  # system sleep / disk transient (曾出现 disk I/O error)
        logger.debug("wal_checkpoint skipped for %s: %s", db_path, exc)
        return (1, -1, -1)

    if row is None:
        return (1, -1, -1)
    return (int(row[0]), int(row[1]), int(row[2]))


def _instance_wal_checkpoint_loop(instance_id: str, stop_event: threading.Event) -> None:
    """每个实例独立的 WAL checkpoint 循环。

    每 ``DIGITAL_LIFE_DB_CHECKPOINT_INTERVAL_S``（默认 1800s = 30min）周期性把
    state.db（以及同进程的几个 .db，如果存在）的 WAL 用 ``wal_checkpoint(TRUNCATE)``
    主动归零。沿用 cron loop 的 set/reset ContextVar 范式，让路径解析拿到正确实例。
    """
    import time
    from infrastructure.config import (
        get_instance_state_db_path,
        set_current_instance_id,
        reset_current_instance_id,
    )
    from domain.lifecycle.events import set_instance_context, reset_instance_context

    interval = int(os.environ.get("DIGITAL_LIFE_DB_CHECKPOINT_INTERVAL_S", "1800"))
    logger.info(
        "Instance %s wal_checkpoint loop interval=%ds", instance_id[:8], interval
    )

    while not stop_event.is_set():
        ctx1 = set_current_instance_id(instance_id)
        ctx2 = set_instance_context(instance_id)
        try:
            state_db = get_instance_state_db_path()
            if state_db.exists():
                busy, log_pages, cked = checkpoint_state_db(state_db)
                if busy == 0 and log_pages == 0:
                    logger.debug(
                        "Instance %s wal_checkpoint ok: checkpointed=%d pages",
                        instance_id[:8],
                        cked,
                    )
                else:
                    logger.debug(
                        "Instance %s wal_checkpoint pending: busy=%d log_pages=%d",
                        instance_id[:8],
                        busy,
                        log_pages,
                    )
        except Exception as exc:
            logger.debug(
                "Instance %s wal_checkpoint loop tick failed: %s",
                instance_id[:8],
                exc,
            )
        finally:
            reset_instance_context(ctx2)
            reset_current_instance_id(ctx1)

        # interval 内分 10s 一段 sleep，便于快速响应 stop
        slept = 0
        while slept < interval and not stop_event.is_set():
            time.sleep(min(10, interval - slept))
            slept += 10


# ──────────────────────────────── Master 主进程模式 ────────────────────────────────


def _ensure_default_instance() -> list[tuple[str, str]]:
    """deprecated 命名, 新代码用 ensure_default_instances()。"""
    return ensure_default_instances()


def ensure_default_instances() -> list[tuple[str, str]]:
    """Bootstrap 默认 zero + alpha 两个示范实例(若无实例)。返 [(display_name, uuid), ...]。

    解耦设计:不再由 run_master_gateway 自动调用;用户显式跑:
      digital-life init           # bootstrap default zero + alpha
      digital-life start          # 启动 gateway

    多次调用幂等:已有实例时返 []。

    仍会读环境变量 LLM_API_KEY / FEISHU_APP_ID / FEISHU_APP_SECRET,
    若有就 bootstrap 给 zero 实例(便于 export 了 env 的用户省一步配置)。
    """
    from infrastructure.config import discover_active_instances
    try:
        existing = discover_active_instances()
    except Exception:
        existing = []
    if existing:
        return []

    logger.info("No instance registered — bootstrapping default zero + alpha instances...")

    feishu_app_id = os.environ.get("FEISHU_APP_ID", "").strip()
    feishu_app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
    glm_api_key = (os.environ.get("LLM_API_KEY") or os.environ.get("GLM_API_KEY") or "").strip()

    default_instances = [
        {
            "display_name": "zero",
            "tagline": "策略师 / 架构师",
            "accent_color": "#00f0ff",
            "sort_order": 1,
            "feishu_app_id": feishu_app_id,  # bootstrap 兜底用启动 shell export 的 FEISHU_APP_ID（可选）
            "feishu_app_secret": feishu_app_secret,
        },
        {
            "display_name": "alpha",
            "tagline": "交易员 / 执行者",
            "accent_color": "#ff2d9c",
            "sort_order": 2,
            "feishu_app_id": "",  # alpha 用第二个飞书应用（用户后续配置）
            "feishu_app_secret": "",
        },
    ]

    bootstrapped_uuids: list[str] = []
    for cfg in default_instances:
        try:
            from infrastructure.bootstrap.instance import init_instance
            inst_dir = init_instance(
                cfg["display_name"],
                feishu_app_id=cfg["feishu_app_id"],
                feishu_app_secret=cfg["feishu_app_secret"],
            )
            instance_uuid = inst_dir.name
            bootstrapped_uuids.append(instance_uuid)

            # 应用 persona 元数据：tagline / accent_color / sort_order
            _patch_instance_metadata(
                instance_uuid,
                {
                    "tagline": cfg["tagline"],
                    "accent_color": cfg["accent_color"],
                    "sort_order": cfg["sort_order"],
                },
            )

            logger.info("✓ Default instance '%s' bootstrapped at %s", cfg["display_name"], inst_dir)
            if not cfg["feishu_app_id"]:
                logger.warning(
                    "  Note: no FEISHU_APP_ID for %s — bot won't connect until "
                    "you configure it in apps/%s/config/", cfg["display_name"], instance_uuid,
                )
        except Exception as exc:
            logger.warning("Auto-bootstrap of '%s' failed (continuing): %s", cfg["display_name"], exc)
            continue

    if not bootstrapped_uuids:
        return []

    return [(cfg["display_name"], uuid) for cfg, uuid in zip(default_instances, bootstrapped_uuids) if uuid]


def _patch_instance_metadata(instance_uuid: str, updates: dict) -> None:
    """Merge updates into apps/<uuid>/config/app.yaml (whole-file rewrite)."""
    import yaml
    from infrastructure.config import get_project_root
    path = get_project_root() / "apps" / instance_uuid / "config" / "app.yaml"
    if not path.exists():
        return
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            return
        for k, v in updates.items():
            raw[k] = v
        path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")
    except Exception as exc:
        logger.warning("patch_instance_metadata(%s) failed: %s", instance_uuid, exc)


def _ensure_default_project() -> None:
    """首次启动防护：没有项目时自动 seed「模拟炒股」项目。

    新人 clone + 跑起来就有一个完整 demo 场景可看：
    zero + alpha 协作执行量化策略（trader/strategist/architect 岗位分工）。
    项目已存在同名 id 时不覆盖（用户自定义的修改保留）。
    """
    import yaml

    from infrastructure.config import get_project_root, _load_registry  # type: ignore[attr-defined]

    projects_dir = get_project_root() / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)

    # 项目已存在（同名 id）不 seed
    default_pid = "trading_simulation"
    project_yaml = projects_dir / default_pid / "project.yaml"
    if project_yaml.exists():
        return

    # 取当前已 bootstrap 的实例作为 trader / strategist
    registry = _load_registry() or {}
    iids = list(registry.keys())
    if len(iids) < 2:
        # 单实例时只填 trader，用户后续可指定 strategist
        zero_iid = iids[0] if iids else ""
        alpha_iid = ""
    else:
        # 第一个做 strategist/manager（zero 决策），第二个做 trader（执行）
        zero_iid = iids[0]
        alpha_iid = iids[1]

    project_dir = projects_dir / default_pid
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "data").mkdir(exist_ok=True)

    # 构造完整 project.yaml —— 来源：模拟炒股任务说明书（用户提供的 docx）
    project_data = {
        "project": {
            "id": default_pid,
            "name": "模拟炒股挑战",
            "description": (
                "获得 10 万元虚拟启动资金，在 A 股市场进行模拟投资。"
                "目标是通过自主决策、自律盯盘、自我复盘，将资金增值到 100 万元（+900%）。"
                "这是一场考验综合能力的长期挑战 —— 不只是赚钱，更是"
                "规划、执行、反思、迭代的全闭环。"
            ),
            "status": "active",
            "manager": zero_iid,
            "group_chat_id": "",
            "goal": (
                "初始资金 ¥100,000 → 目标资金 ¥1,000,000（+900%）。"
                "关键不是能不能赚够，而是：\n"
                "1. 能否自主规划并持续执行一个长期任务\n"
                "2. 遇到问题时如何分析、如何解决\n"
                "3. 能否诚实面对亏损并从中学习\n"
                "4. 决策逻辑是否清晰可追溯\n"
                "策略可随时调整，只需能解释原因。必要时承认"
                "「这个我做不到」也是一种诚实的反思。"
            ),
            "kpis": [
                "每日复盘完成率 ≥ 80%",
                "每笔交易有明确理由 + 操作记录",
                "账户状态文件随时可查",
                "亏损时有结构化归因（判断失误 / 时机 / 信息不足）",
            ],
            "thesis": (
                "投资者注意力策略 + 不对称盈亏比："
                "精力聚焦在首板涨停次日溢价（论断 4：T+1 收盘前必须了结）+"
                "止损 -3% / 止盈 +5~7% 的不对称结构能在 45% 胜率下实现正期望。"
                "核心是诚实迭代——每一笔交易（无论盈亏）都进入结构性反思，"
                "论断本身可以随时被修正甚至废弃（如论断 4 在 6/18 已废弃）。"
            ),
            "positions": [
                {
                    "id": "strategist",
                    "name": "策略师",
                    "assignees": [zero_iid] if zero_iid else [],
                },
                {
                    "id": "trader",
                    "name": "交易员",
                    "assignees": [alpha_iid] if alpha_iid else [],
                },
                {
                    "id": "manager",
                    "name": "项目经理（人类）",
                    "assignees": [],
                },
            ],
            "review_schedule": {
                "daily_review_time": "21:00",
                "weekly_planning_day": "Sunday",
                "monthly_review_day": "last_friday",
            },
            "rules": {
                "t_plus_1": "当天买入次日才能卖出",
                "limit_up_down": "主板 ±10%，创业板/科创板 ±20%，ST 股 ±5%",
                "trading_hours": "9:30-11:30 13:00-15:00 工作日",
                "fees": "印花税 0.1%（卖）、佣金 0.025%（买卖）、过户费 0.001%",
                "lot_size": "买入需 100 股整数倍，卖出不限",
            },
            "accounting": {
                "initial_capital": 100000,
                "remember": (
                    "账户状态由数字生命自主维护。建议维护 trading_log.json +"
                    "账户快照；每笔交易（含手续费）实时扣减现金。"
                ),
            },
            "working_memory": (
                "见 projects/trading_simulation/working_memory/ 下的"
                "thesis.md（论断列表）+ daily_log.md（每日策略执行记录）+"
                "review.md（复盘结论）。这些文件由数字生命自己写入 / 修正。"
            ),
        }
    }

    project_yaml.write_text(
        yaml.dump(project_data, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    logger.info("Default project '%s' seeded", default_pid)


async def run_master_gateway() -> None:
    """主进程：HTTP server + InstanceSupervisor。

    InstanceSupervisor 读取 var/run/last_active.json，spawn 所有子进程，
    监视并在 crash 时有限次数内重启。
    """
    # start 不依赖实例存在 —— 可以空跑,用户在前端自行建实例。
    # init 命令只是"快速建默认 zero+alpha"的捷径,不是 start 的前置条件。
    # 首次启动防护:没有项目时,自动 seed 默认「模拟炒股」项目。
    try:
        _ensure_default_project()
    except Exception as exc:
        logger.warning("seed default project failed (non-fatal): %s", exc)

    # 注入运行时适配器（保险：实际 wake 在 instance 子进程里跑，子进程会
    # 自行注入。这里补一刀覆盖可能的开发模式直接跑 master 的情况，
    # 让 session_evidence 的 fallback reader 不再是 NullSessionEvidenceReader）。
    try:
        from application.runtime_provider import configure_runtime_provider
        configure_runtime_provider()
    except Exception as exc:
        logger.warning("configure_runtime_provider failed at master startup: %s", exc)

    from gateway.instance_supervisor import InstanceSupervisor

    stop_event = asyncio.Event()

    def _signal_handler(sig):
        logger.info("Master received signal %s", sig)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler, sig)
        except NotImplementedError:
            pass

    # 启动 HTTP server
    app = web.Application()
    app["feishu_adapters"] = []  # master 不持有 adapter

    from application.api.employee_console_routes import register
    register(app, adapter=None)

    # 系统级路由（跨实例）：/api/system/* + /employee/{iid}/assets/*
    from application.api.system_routes import add_system_routes
    add_system_routes(app)

    # OAuth callback (飞书社交接管授权)
    try:
        from application.api.oauth_routes import register_oauth_routes
        register_oauth_routes(app)
    except Exception:
        pass

    # 广播 endpoint:接收 peer 实例的 HTTP 广播(去中心化消息总线 Phase 3)
    from application.api.broadcast_routes import add_broadcast_routes
    add_broadcast_routes(app)

    # 感知 endpoint:接收感知 daemon(独立子进程)的录屏录音理解结果
    # (feature 003-perception;spec FR-010/FR-012)。daemon 只打 master 8642。
    try:
        from application.api.perception_routes import add_perception_routes
        add_perception_routes(app)
    except Exception as exc:
        logger.warning("Perception routes failed (non-fatal): %s", exc)

    # 语音感知控制 endpoint:实例 voice_focus 工具 / 外部按键 → 控制 AudioSenseService 状态
    try:
        from application.api.voice_sense_routes import add_voice_sense_routes
        add_voice_sense_routes(app)
    except Exception as exc:
        logger.warning("Voice sense routes failed (non-fatal): %s", exc)


    # 启动时同步各实例的 subscriptions.yaml(其他实例在哪群 → 各自写好 peers 列表)
    try:
        from domain.messages.broadcast import sync_subscriptions_from_registry
        sync_subscriptions_from_registry()
    except Exception as exc:
        logger.warning("sync_subscriptions failed (non-fatal): %s", exc)

    from infrastructure.http.openai_api import add_openai_routes
    add_openai_routes(app)

    port = int(os.getenv("API_SERVER_PORT") or 8642)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Master HTTP server started on port %d", port)

    # 启动 supervisor
    supervisor = InstanceSupervisor()
    app["supervisor"] = supervisor  # 暴露给 HTTP routes，用于 toggle active 时联动 spawn/stop
    await supervisor.start()

    # 启动 AudioSenseService（持续语音感知，master 级，可选）
    # 读 config/voice_sense.yaml，enabled=false 时跳过
    voice_sense_svc = None
    try:
        from infrastructure.perception.audio_sense.service import (
            AudioSenseService, load_voice_sense_config,
        )
        from application.api.voice_sense_routes import set_voice_sense_service
        vs_cfg = load_voice_sense_config()
        voice_sense_svc = AudioSenseService(vs_cfg)
        voice_sense_svc.start()
        set_voice_sense_service(voice_sense_svc)
    except Exception as exc:
        logger.warning("AudioSenseService failed to start (non-fatal): %s", exc)

    # restart 物理触发：POST /api/system/gateway/restart 写 var/run/.restart_requested，
    # 本 watcher 检测到后 spawn 一个独立的 `digital-life restart` 进程完成
    # stop + start —— 与 CLI `digital-life restart` 完全同语义，裸跑环境也自重启，
    # **不依赖** launchd/systemd 等外部 wrapper（历史实现只 os._exit 等外部拉起，
    # 在开发机裸跑场景下等于"直接关服务"）。
    async def _watch_restart_trigger():
        import sys
        from infrastructure.config import get_project_root

        root = get_project_root()
        trigger_path = root / "var" / "run" / ".restart_requested"
        while not stop_event.is_set():
            try:
                if trigger_path.exists():
                    try:
                        payload = trigger_path.read_text(encoding="utf-8")
                    except Exception:
                        payload = ""
                    logger.warning(
                        "Master restart triggered. Payload:\n%s\n"
                        "Delegating to a detached `digital-life restart` process "
                        "(stop old master → start new master).",
                        payload,
                    )
                    try:
                        trigger_path.unlink()
                    except OSError:
                        pass

                    # spawn 独立 session 的 restart 进程:
                    #   - start_new_session=True: 不挂靠本 master,本 master 死了它也活
                    #   - stdout/stderr → LOG_FILE:restart 输出可追溯
                    #   - detached:master graceful shutdown 不会带走它
                    # 该进程 _stop 会 SIGTERM master(配合下方 stop_event 的 graceful),
                    # 超时会 SIGKILL 兜底,sleep 2.5s 后 _start 新 master。
                    cli_path = root / "interfaces" / "cli" / "cli.py"
                    log_file = root / "var" / "logs" / "digital-life.log"
                    try:
                        log_handle = open(log_file, "a", encoding="utf-8")
                        log_handle.write(
                            "\n--- restart subprocess spawned by watcher "
                            f"at {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n"
                        )
                        log_handle.flush()
                        subprocess.Popen(
                            [sys.executable, str(cli_path), "restart",
                             "--timeout", "20", "--health-wait", "3"],
                            cwd=str(root),
                            stdout=log_handle,
                            stderr=subprocess.STDOUT,
                            start_new_session=True,
                            close_fds=True,
                        )
                    except Exception as spawn_exc:
                        logger.error(
                            "Failed to spawn detached restart subprocess: %s. "
                            "Master will graceful-shutdown but NOT auto-restart "
                            "(run `digital-life start` manually).",
                            spawn_exc,
                        )

                    # 触发本 master graceful shutdown(stop_event → supervisor.stop +
                    # runner.cleanup)。restart 子进程的 SIGTERM 会配合；若 graceful 卡住,
                    # 子进程 --timeout 20 会 SIGKILL 兜底。不在此 os._exit,让 graceful 走完,
                    # 避免在 graceful 还没回收实例子进程时暴死。
                    stop_event.set()
                    return
            except Exception as exc:
                logger.warning("restart watcher tick failed: %s", exc)
            await asyncio.sleep(5)

    restart_watcher_task = asyncio.create_task(_watch_restart_trigger())

    await stop_event.wait()
    logger.info("Master shutting down...")

    if voice_sense_svc:
        try:
            voice_sense_svc.stop()
        except Exception:
            pass
    await supervisor.stop()
    await runner.cleanup()
    logger.info("Master stopped")


# ──────────────────────────────── 兼容入口 ────────────────────────────────


def _discover_feishu_bots() -> list[dict]:
    """旧版兼容：单进程扫描所有实例的 feishu bot。

    主线不再使用，保留用于回退或调试。
    """
    import yaml
    from infrastructure.config import discover_active_instances, get_project_root

    bots: dict[str, dict] = {}
    for instance_id in discover_active_instances():
        config_file = get_project_root() / "apps" / instance_id / "config" / "app.yaml"
        if not config_file.exists():
            continue
        try:
            cfg = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
            channels = cfg.get("channels") or {}
            feishu = channels.get("feishu") or {} if isinstance(channels, dict) else {}
            app_id = (feishu.get("app_id") or "").strip() if isinstance(feishu, dict) else ""
            app_secret = ""
            secrets_path = get_project_root() / "apps" / instance_id / "config" / "secrets.env"
            if secrets_path.exists():
                for line in secrets_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("FEISHU_APP_SECRET="):
                        app_secret = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
            if app_id and app_id not in bots:
                bots[app_id] = {
                    "app_id": app_id,
                    "app_secret": app_secret,
                    "label": instance_id,
                }
        except Exception:
            pass

    if not bots:
        app_id = os.environ.get("FEISHU_APP_ID", "")
        app_secret = os.environ.get("FEISHU_APP_SECRET", "")
        if app_id:
            bots[app_id] = {"app_id": app_id, "app_secret": app_secret, "label": "env"}

    return list(bots.values())


async def run_gateway() -> None:
    """Legacy 单进程入口：跑 HTTP + 所有 adapter + cron。

    用于调试或单实例场景。多实例推荐用 run_master_gateway / run_instance_gateway。
    """
    adapters: list = []
    stop_event = asyncio.Event()

    from interfaces.ingress.feishu import FeishuAdapter
    from application.ingress.handler import handle_message as ingress_handle_message

    for bot in _discover_feishu_bots():
        feishu = FeishuAdapter(app_id=bot["app_id"], app_secret=bot["app_secret"])
        feishu.on_message(lambda msg, f=feishu: ingress_handle_message(adapter=f, msg=msg))
        await feishu.start()
        adapters.append(feishu)
        logger.info("Feishu ingress adapter for %s started", bot["label"])

    app = web.Application()
    app["feishu_adapters"] = list(adapters)
    from application.api.employee_console_routes import register
    register(app, adapter=None)

    # 广播 endpoint:接收 peer 实例的 HTTP 广播(去中心化消息总线 Phase 3)
    from application.api.broadcast_routes import add_broadcast_routes
    add_broadcast_routes(app)


    # 启动时同步各实例的 subscriptions.yaml(其他实例在哪群 → 各自写好 peers 列表)
    try:
        from domain.messages.broadcast import sync_subscriptions_from_registry
        sync_subscriptions_from_registry()
    except Exception as exc:
        logger.warning("sync_subscriptions failed (non-fatal): %s", exc)
    from infrastructure.http.openai_api import add_openai_routes
    add_openai_routes(app)

    port = int(os.getenv("API_SERVER_PORT") or 8642)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("HTTP server started on port %d", port)

    cron_thread = None
    cron_stop = threading.Event()
    try:
        from infrastructure.scheduler.cron_runner import start_cron_daemon
        cron_thread = start_cron_daemon(stop_event=cron_stop)
        logger.info("Cron daemon started")
    except Exception as exc:
        logger.warning("Cron daemon failed to start: %s", exc)

    def _signal_handler(sig):
        logger.info("Received signal %s", sig)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler, sig)
        except NotImplementedError:
            pass

    await stop_event.wait()
    logger.info("Gateway shutting down...")

    for adapter in adapters:
        try:
            await adapter.stop()
        except Exception:
            pass
    await runner.cleanup()
    if cron_thread and cron_thread.is_alive():
        cron_stop.set()
        cron_thread.join(timeout=5)
    logger.info("Gateway stopped")
