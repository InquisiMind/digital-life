"""InstanceSupervisor — master 进程管理子实例进程。

职责：
  - 读取 last_active.json 决定要起哪些实例
  - spawn 每个实例的子进程 (gateway/main.py --instance <uuid>)
  - 监视子进程健康，crash 时有限次数内重启
  - 暴露 stop() 优雅关闭所有子进程

last_active.json 格式：
  {
    "instances": ["c2a5c8e8-...", "5052c33a-..."],
    "updated_at": "2026-06-03T17:30:00+08:00"
  }
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("gateway.supervisor")


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _var_run_dir() -> Path:
    """项目根 var/run/，master 和 instance 共享。"""
    return _project_root() / "var" / "run"


def _var_logs_dir() -> Path:
    return _project_root() / "var" / "logs"


def _last_active_path() -> Path:
    return _var_run_dir() / "last_active.json"


def read_last_active() -> list[str]:
    """读活跃实例列表 —— "之前谁跑，现在还谁跑"。

    从 last_active.json 读取上次运行的实例列表；文件不存在 / 首次启动时
    fallback 到 discover_active_instances()（扫 apps/*/config/app.yaml
    里 active=true 的实例）。

    新建实例 → _handle_create_instance 会 write_last_active + 直接 spawn，
    不需要重启 gateway。
    toggle active → _handle_toggle_instance_active 会 write_last_active。
    """
    from infrastructure.config import is_instance_active, discover_instances
    from infrastructure.config import discover_active_instances
    path = _last_active_path()
    raw: list[str] = []
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8")) or {}
            raw = list(data.get("instances") or [])
        except Exception:
            raw = []
    if not raw:
        return list(discover_active_instances())
    registered = set(discover_instances())
    valid = [iid for iid in raw if iid in registered]
    removed = set(raw) - set(valid)
    if removed:
        logger.warning(
            "last_active.json has %d unregistered instance(s), pruned: %s",
            len(removed), [iid[:8] for iid in removed],
        )
    active_only = [iid for iid in valid if is_instance_active(iid)]
    if not active_only:
        discovered = list(discover_active_instances())
        if discovered:
            logger.warning(
                "last_active.json yielded 0 active instances, falling back to registry: %s",
                [iid[:8] for iid in discovered],
            )
            return discovered
        logger.warning(
            "last_active.json has %d instances but none are active: %s",
            len(raw), raw,
        )
    return active_only


def write_last_active(instance_ids: list[str]) -> None:
    path = _last_active_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "instances": list(instance_ids),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


@dataclass
class _InstanceProc:
    instance_id: str
    process: Optional[subprocess.Popen] = None
    restart_count: int = 0
    last_start_at: float = 0.0
    # 5 分钟窗口内的重启计数（用于有限重启策略）
    recent_restarts: list = field(default_factory=list)


class InstanceSupervisor:
    """master 进程的子进程管理器。"""

    # 重启策略：5 分钟内最多 3 次重启，超过即放弃
    RESTART_WINDOW_S = 300
    RESTART_MAX = 3

    def __init__(self) -> None:
        self._procs: dict[str, _InstanceProc] = {}
        self._stop_event = asyncio.Event()
        self._watch_task: Optional[asyncio.Task] = None
        # 子进程 stdout 直接管（仅 print / traceback / 启动早期日志）
        # 主结构化 logging 由 main.py 的 TimedRotatingFileHandler 写到
        # digital-life.log，跟这里分开避免冲突 + 保证 rotate。
        self._log_path = _var_logs_dir() / "digital-life.log"
        self._stdout_log_path = _var_logs_dir() / "stdout.log"

    async def start(self) -> None:
        """读取 last_active → spawn 所有实例。"""
        instance_ids = read_last_active()
        if not instance_ids:
            logger.warning("No instances to start (last_active.json empty)")
            return

        # 持久化（首次启动可能是 discover 出来的）
        write_last_active(instance_ids)

        for iid in instance_ids:
            await self._spawn(iid)

        # 启动 watchdog
        self._watch_task = asyncio.create_task(self._watch_loop())
        logger.info("Supervisor started with %d instance(s)", len(instance_ids))

    async def _spawn(self, instance_id: str) -> bool:
        """启动一个实例子进程。返回是否成功 spawn。"""
        if instance_id in self._procs and self._procs[instance_id].process and self._procs[instance_id].process.poll() is None:
            logger.warning("Instance %s already running", instance_id[:8])
            return False

        if instance_id not in self._procs:
            self._procs[instance_id] = _InstanceProc(instance_id=instance_id)

        proc_info = self._procs[instance_id]
        proc_info.last_start_at = time.time()

        cmd = [sys.executable, str(_project_root() / "gateway" / "main.py"), "--instance", instance_id]
        env = os.environ.copy()
        env["L4_INSTANCE_ID"] = instance_id
        env["PYTHONPATH"] = os.pathsep.join([str(_project_root()), env.get("PYTHONPATH", "")])

        try:
            # 子进程 stdout/stderr 重定向到 raw stdout 日志（仅捕获 print / traceback /
            # 子进程启动早期日志）——主日志 digital-life.log 由 main.py 的
            # TimedRotatingFileHandler 单独写。两个文件分开避免冲突 + 同时具备
            # rotate 能力。
            self._stdout_log_path.parent.mkdir(parents=True, exist_ok=True)
            log_handle = self._stdout_log_path.open("a", encoding="utf-8")
            log_handle.write(f"\n--- instance {instance_id[:8]} start at {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            log_handle.flush()
            process = subprocess.Popen(
                cmd,
                cwd=str(_project_root()),
                env=env,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # 独立进程组，便于 stop 时 kill 整组
            )
            proc_info.process = process
            logger.info("Spawned instance %s pid=%d", instance_id[:8], process.pid)
            return True
        except Exception as exc:
            logger.error("Failed to spawn instance %s: %s", instance_id[:8], exc)
            return False

    async def _refresh_last_active(self) -> list[str]:
        """读取 last_active.json，与 self._procs 对比，返回当前应该运行的实例列表。

        过滤 inactive，always 尊重 is_instance_active。
        """
        return read_last_active()

    async def add_instance(self, instance_id: str) -> bool:
        """前端开新实例时调用：spawn 子进程 + append 到 last_active.json。

        返回是否成功 spawn（已被 is_instance_active 排除/in 已在跑则返回 False）。
        """
        from infrastructure.config import is_instance_active
        if not is_instance_active(instance_id):
            logger.warning("add_instance %s aborted: active=False in config", instance_id[:8])
            return False
        if instance_id in self._procs and self._procs[instance_id].process and self._procs[instance_id].process.poll() is None:
            logger.info("add_instance %s: already running", instance_id[:8])
            return True

        ok = await self._spawn(instance_id)
        if ok:
            cur = read_last_active()
            if instance_id not in cur:
                cur.append(instance_id)
                write_last_active(cur)
            logger.info("add_instance %s done", instance_id[:8])
        return ok

    async def remove_instance(self, instance_id: str) -> bool:
        """前端关实例时调用：stop 子进程 + 从 last_active.json 移除。"""
        stopped = await self.stop_instance(instance_id)
        # 从 self._procs 移除（避免 watch_loop 再次拉起）
        if instance_id in self._procs:
            del self._procs[instance_id]
        cur = read_last_active()
        if instance_id in cur:
            cur = [i for i in cur if i != instance_id]
            write_last_active(cur)
        logger.info("remove_instance %s stopped=%s", instance_id[:8], stopped)
        return stopped

    async def _watch_loop(self) -> None:
        """每 5 秒检查子进程：
        1. 已有 procs 中退出的 → 有限重启
        2. last_active.json 漂移检测：新增的 spawn，消失的 stop（兜底，正常路径靠 add/remove）
        """
        while not self._stop_event.is_set():
            now = time.time()
            # 1) 崩溃恢复
            for iid, proc_info in list(self._procs.items()):
                proc = proc_info.process
                if proc is None:
                    continue
                rc = proc.poll()
                if rc is not None:
                    # 进程已退出
                    logger.warning("Instance %s exited rc=%d", iid[:8], rc)
                    # 检查重启次数
                    proc_info.recent_restarts = [
                        t for t in proc_info.recent_restarts if now - t < self.RESTART_WINDOW_S
                    ]
                    if len(proc_info.recent_restarts) >= self.RESTART_MAX:
                        logger.error(
                            "Instance %s restart limit exceeded (%d in %ds), giving up",
                            iid[:8], self.RESTART_MAX, self.RESTART_WINDOW_S,
                        )
                        proc_info.process = None
                        continue
                    # 若实例已被前端关掉 (不在 last_active.json)，不要拉起
                    target = set(read_last_active())
                    if iid not in target:
                        logger.info("Instance %s removed from last_active, not restarting", iid[:8])
                        proc_info.process = None
                        continue
                    proc_info.recent_restarts.append(now)
                    proc_info.restart_count += 1
                    logger.info("Restarting instance %s (attempt %d)...", iid[:8], proc_info.restart_count)
                    await asyncio.sleep(2)  # 短暂等待，避免快速循环
                    await self._spawn(iid)
            # 2) 漂移检测
            try:
                target = set(read_last_active())
            except Exception:
                target = None
            if target is not None:
                current = set(self._procs.keys())
                # 新激活但未起：spawn
                for iid in target - current:
                    logger.info("watch_loop: discover newly active %s, spawning", iid[:8])
                    await self._spawn(iid)
                # 已不在 last_active 但还在跑：stop（前端刚 toggle 为 inactive）
                for iid in current - target:
                    if self._procs[iid].process and self._procs[iid].process.poll() is None:
                        logger.info("watch_loop: %s no longer active, stopping", iid[:8])
                        await self.stop_instance(iid)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                continue

    def signal_process(self, instance_id: str, signum: int) -> bool:
        """向实例 worker 进程发信号（同步、即时）。

        用途：感知热键单守护的按键打断（master 发 SIGUSR1，worker 的
        事件循环处理器里调 voice_output.stop_playback()）。
        返回 False = 实例不在跑 / 信号发送失败。
        """
        proc_info = self._procs.get(instance_id)
        proc = proc_info.process if proc_info else None
        if proc is None or proc.poll() is not None:
            return False
        try:
            proc.send_signal(signum)
            return True
        except Exception as exc:
            logger.warning("signal %s → instance %s failed: %s",
                           signum, instance_id[:8], exc)
            return False

    async def stop_instance(self, instance_id: str) -> bool:
        """Stop one specific instance (graceful SIGTERM → SIGKILL after 10s)."""
        proc_info = self._procs.get(instance_id)
        if not proc_info or not proc_info.process:
            return False
        proc = proc_info.process
        if proc.poll() is not None:
            return False
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return False

        # 等待最多 10 秒
        for _ in range(40):
            if proc.poll() is not None:
                return True
            await asyncio.sleep(0.25)

        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return True

    async def stop(self) -> None:
        """Stop supervisor: gracefully terminate all instance subprocesses."""
        self._stop_event.set()
        if self._watch_task:
            self._watch_task.cancel()
            try:
                await self._watch_task
            except (asyncio.CancelledError, Exception):
                pass

        # 并发 stop 所有实例
        tasks = [self.stop_instance(iid) for iid in list(self._procs.keys())]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("Supervisor stopped")

    def list_instances(self) -> list[dict]:
        """For status reporting."""
        out = []
        for iid, proc_info in self._procs.items():
            proc = proc_info.process
            alive = bool(proc and proc.poll() is None)
            out.append({
                "instance_id": iid,
                "pid": proc.pid if proc else None,
                "alive": alive,
                "restart_count": proc_info.restart_count,
            })
        return out
