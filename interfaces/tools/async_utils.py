"""Async helpers used by project-owned tool integrations.

sync→async 的唯一桥（单一来源）：同步调用方等 async 结果一律走 run_async。

平台的两类进程（master / service worker）都常驻事件循环，所以桥必须在两种
上下文下都对：
- 裸线程（无运行中 loop）→ asyncio.run 一次性跑完；
- loop 线程内 → 提交给进程级常驻后台 loop 线程执行，本线程阻塞等结果。
  同步调用方阻塞即平台既有模型（wake 本身就是长阻塞操作）；后台线程独立
  运行协程，不会死锁。

历史教训（2026-10-19）：旧实现对"loop 线程内"的场景用
run_coroutine_threadsafe 指回自身 loop 再阻塞等结果——协程永远排不上队，
纯死锁；MCP 装载期枚举（asyncio.run 则直接 RuntimeError）是第一个踩中的。
"""

from __future__ import annotations

import asyncio
import threading
from typing import Awaitable, TypeVar

T = TypeVar("T")

_shared_loop: asyncio.AbstractEventLoop | None = None
_shared_thread: threading.Thread | None = None
_lock = threading.Lock()


def _shared_loop_thread() -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
    """进程级常驻后台 loop（懒启动；daemon 线程 run_forever）。"""
    global _shared_loop, _shared_thread
    if _shared_loop is None or _shared_loop.is_closed():
        with _lock:
            if _shared_loop is None or _shared_loop.is_closed():
                loop = asyncio.new_event_loop()
                th = threading.Thread(
                    target=loop.run_forever, name="async-bridge", daemon=True
                )
                th.start()
                _shared_loop, _shared_thread = loop, th
    return _shared_loop, _shared_thread


def run_async(awaitable: Awaitable[T], timeout: float | None = None) -> T:
    """同步上下文里等 async 结果（平台唯一 sync→async 桥）。

    在桥自己的线程内同步调用是编程错误（自我等待必死锁），显式拒绝——
    那种场景应该直接 await 协程。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)

    loop, th = _shared_loop_thread()
    if threading.current_thread() is th:
        raise RuntimeError(
            "run_async 不能在 async 桥线程内同步调用（自我等待必死锁）——请直接 await"
        )
    fut = asyncio.run_coroutine_threadsafe(awaitable, loop)
    return fut.result(timeout)
