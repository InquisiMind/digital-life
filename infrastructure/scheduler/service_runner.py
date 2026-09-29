"""服务调度循环 — master 内常驻线程：扫服务队列 → 抢 lease → spawn worker。

为什么存在：实例型的 cron 线程寄生在长驻实例进程里；服务 worker 是
ephemeral 进程，没有宿主跑 tick——由本循环代替（设计文档 v1.1 第七章
C 类"服务调度循环"，v1.1 补扫项）。

语义：
  - 只扫 active 服务（archived = 服务开关关，"归档后再发不拉起"）。
  - due 判断与 legacy_bus.pop_due_events 同一 SQL 语义（未消费 + 到期 +
    channel 前缀），但这里是跨服务只读探测，直连各服务 state.db，
    不切 ContextVar。
  - 同一服务同时只允许一个 worker：spawn 前抢 DB lease（holder=master:pid），
    worker 退出后 reap 释放。master 死亡留下的僵尸 lease 由
    DEFAULT_LEASE_STALE_SECONDS 超时兜底（worker 侧同样过这道闸）。
  - spawn 出来的 worker 继承 L4_SERVICE_LEASE_HOLDER，worker 启动时
    续租校验，防止手工 worker 与循环 worker 并发。
"""

from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

logger = logging.getLogger("digital_life.infrastructure.scheduler.service_runner")

_DEFAULT_SCAN_INTERVAL = 10.0


def service_state_db_path(service_id: str) -> Path:
    from infrastructure.config import resolve_runtime_dir

    return resolve_runtime_dir(service_id) / "data" / "state.db"


def has_due_events(service_id: str, kinds: tuple[str, ...] | None = None) -> bool:
    """只读探测服务的到期未消费事件（SQL 语义对齐 legacy_bus.pop_due_events）。

    kinds: 订阅过滤（特性 3——闹钟等未订阅 kind 到期也不触发拉起）。
    None = 不过滤（订阅 all 或测试直查）。
    """
    db_path = service_state_db_path(service_id)
    if not db_path.exists():
        return False
    kind_clause = ""
    params: list[str] = []
    if kinds is not None:
        if not kinds:
            return False
        kind_clause = " AND kind IN (%s)" % ",".join("?" * len(kinds))
        params.extend(kinds)
    try:
        from domain.lifecycle.clock import now_iso

        conn = sqlite3.connect(str(db_path), timeout=3.0)
        try:
            row = conn.execute(
                "SELECT 1 FROM events WHERE consumed_at IS NULL"
                " AND (fire_at IS NULL OR fire_at <= ?)"
                " AND channel LIKE ?" + kind_clause + " LIMIT 1",
                [now_iso(), f"instance:{service_id}%"] + params,
            ).fetchone()
        finally:
            conn.close()
        return row is not None
    except sqlite3.Error as exc:
        logger.debug("service %s state.db probe failed: %s", service_id[:12], exc)
        return False


def _fire_service_alarms(service_id: str) -> None:
    """把服务的到期 timer 转成事件——对齐实例 cron tick 的 fire_due_alarms 职责。

    服务无常驻 cron，调度循环是它唯一的 tick。上下文与叫醒抑制跟
    emit_to_service 同一姿势：事件落服务私有库，wake 决策归本循环。
    转换出的 kind 是否触发拉起由订阅闸（has_due_events 的 kinds）决定。
    """
    from domain.lifecycle.events import (
        reset_instance_context,
        reset_wake_suppressed,
        set_instance_context,
        set_wake_suppressed,
    )
    from infrastructure.config import (
        reset_current_instance_id,
        set_current_instance_id,
    )

    cfg_token = set_current_instance_id(service_id)
    evt_token = set_instance_context(service_id)
    sup_token = set_wake_suppressed(True)
    try:
        from domain.lifecycle.alarms import fire_due_alarms

        fired = fire_due_alarms()
        if fired:
            logger.info(
                "SERVICE_ALARMS_FIRED service_id=%s count=%d", service_id, len(fired)
            )
    finally:
        reset_wake_suppressed(sup_token)
        reset_instance_context(evt_token)
        reset_current_instance_id(cfg_token)


class ServiceWorkerLoop:
    """单线程扫描循环。spawn 参数可注入（测试用假进程）。"""

    def __init__(
        self,
        scan_interval: float | None = None,
        stale_seconds: float | None = None,
        spawn: Callable[[str], subprocess.Popen] | None = None,
    ) -> None:
        from infrastructure.persistence import services_registry

        self._scan_interval = float(
            scan_interval or os.environ.get("SERVICE_SCAN_INTERVAL") or _DEFAULT_SCAN_INTERVAL
        )
        self._stale_seconds = float(
            stale_seconds
            or os.environ.get("SERVICE_LEASE_STALE_SECONDS")
            or services_registry.DEFAULT_LEASE_STALE_SECONDS
        )
        self._spawn = spawn or self._spawn_worker
        self._procs: dict[str, subprocess.Popen] = {}
        self._holder = f"master:{os.getpid()}"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ── 生命周期 ──────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, name="service-worker-loop", daemon=True
        )
        self._thread.start()
        logger.info(
            "Service worker loop started (interval=%.0fs holder=%s)",
            self._scan_interval, self._holder,
        )

    def stop(self) -> None:
        self._stop.set()
        # master 退出时把还活着的 worker 一并带走，避免孤儿 worker 占着 lease
        for sid, proc in list(self._procs.items()):
            if proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
            self._release(sid)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("service worker loop tick failed")
            self._stop.wait(self._scan_interval)

    # ── 单轮扫描 ──────────────────────────────────────────────────────

    def tick(self) -> list[str]:
        """扫一轮：返回本轮 spawn 的服务 id 列表（测试断言用）。"""
        self._reap()
        from infrastructure.persistence import services_registry

        spawned: list[str] = []
        for svc in services_registry.list_services(
            status=services_registry.SERVICE_STATUS_ACTIVE
        ):
            sid = svc["service_id"]
            running = self._procs.get(sid)
            if running is not None and running.poll() is None:
                continue
            try:
                _fire_service_alarms(sid)
            except Exception as exc:
                logger.warning(
                    "service alarm fire failed service_id=%s: %s", sid, exc
                )
            # 订阅闸（特性 3）：默认只认消息类 kind；订阅 all 则不过滤
            subs = svc.get("subscriptions")
            kinds = None if subs == "all" else tuple(
                {"message", "group_message"} | set(subs or [])
            )
            if not has_due_events(sid, kinds=kinds):
                continue
            if not services_registry.try_acquire_lease(sid, self._holder, self._stale_seconds):
                logger.info(
                    "SERVICE_SKIP service_id=%s reason=lease_held", sid
                )
                continue
            try:
                proc = self._spawn(sid)
            except Exception as exc:
                logger.error("service worker spawn failed service_id=%s: %s", sid, exc)
                services_registry.release_lease(sid, self._holder)
                continue
            self._procs[sid] = proc
            spawned.append(sid)
            logger.info(
                "SERVICE_SPAWNED service_id=%s pid=%s", sid, getattr(proc, "pid", "?")
            )
        return spawned

    def _reap(self) -> None:
        for sid, proc in list(self._procs.items()):
            code = proc.poll()
            if code is not None:
                self._release(sid)
                logger.info(
                    "SERVICE_WORKER_REAPED service_id=%s exit=%s", sid, code
                )

    def _release(self, sid: str) -> None:
        from infrastructure.persistence import services_registry

        services_registry.release_lease(sid, self._holder)
        self._procs.pop(sid, None)

    # ── 真实 spawn ────────────────────────────────────────────────────

    def _spawn_worker(self, service_id: str) -> subprocess.Popen:
        from infrastructure.config import get_project_root

        root = get_project_root()
        cmd = [
            sys.executable,
            str(root / "gateway" / "main.py"),
            "--service",
            service_id,
        ]
        env = os.environ.copy()
        env["DIGITAL_LIFE_INSTANCE_ID"] = service_id
        env["L4_SERVICE_LEASE_HOLDER"] = self._holder
        env["PYTHONPATH"] = os.pathsep.join([str(root), env.get("PYTHONPATH", "")])

        log_path = root / "var" / "logs" / "service-workers.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = log_path.open("a", encoding="utf-8")
        handle.write(
            f"\n--- service {service_id} spawn at {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n"
        )
        handle.flush()
        return subprocess.Popen(
            cmd,
            cwd=str(root),
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


_loop: ServiceWorkerLoop | None = None


def start_service_worker_loop() -> ServiceWorkerLoop:
    """master 启动时挂载全局循环（幂等）。"""
    global _loop
    if _loop is None:
        _loop = ServiceWorkerLoop()
    _loop.start()
    return _loop


def stop_service_worker_loop() -> None:
    global _loop
    if _loop is not None:
        _loop.stop()
        _loop = None
