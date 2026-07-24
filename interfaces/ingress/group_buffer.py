"""群消息合并窗口 — 双 timer 设计。

慢 timer: 从首条消息进来 → 20s + random(0,10) = 20~30s。累积窗口,所有消息都吃。
快 timer: 从 @ / 关键词消息进来 → random(0,5)。仅优先消息触发。
两条并行,先到先 flush 全部,窗口归零,下一条消息重启循环。

每实例 fast timer 值 random(0,5) 各不同 → 多实例自然错峰。

flush 在 daemon thread + asyncio.run,不依赖 lark WS blocking loop。
"""
from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
from typing import Any, Awaitable, Callable, List, Optional

logger = logging.getLogger(__name__)

# 慢 timer: 固定窗 + random
SLOW_BASE_S = 20
SLOW_JITTER_S = 10

# 快 timer: 纯 random
# 快 timer jitter 缩短到 0 → fast timer = 固定值, 便于多条 @ 合入同一窗口
# 但也不宜太长(用户等不起)。实测 8s 是平衡点: 同窗口内几秒内连发多条@不会分批
FAST_BASE_S = 8
FAST_JITTER_S = 0


class GroupMessageBuffer:
    """pipeline: 收 → buffer → 双 timer 先到先 flush。"""

    def __init__(
        self,
        handler: Callable[[Any], Awaitable[Any]],
        *,
        slow_base: int = SLOW_BASE_S,
        slow_jitter: int = SLOW_JITTER_S,
        fast_jitter: int = FAST_JITTER_S,
        fast_base: int = FAST_BASE_S,
        is_priority: Optional[Callable[[Any], bool]] = None,
    ) -> None:
        self._handler = handler
        self._slow_base = slow_base
        self._slow_jitter = slow_jitter
        self._fast_jitter = fast_jitter
        self._fast_base = fast_base
        self._is_priority = is_priority or (lambda m: bool(getattr(m, "mentions_bot", False)))

        self._buffer: List[Any] = []
        self._lock = threading.Lock()
        self._stop_flag = threading.Event()

        # 窗口状态
        self._window_deadline: float = 0.0    # 慢 timer 到期时间
        self._fast_deadline: Optional[float] = None  # 快 timer 到期时间 (None=未触发)
        self._flush_signal = threading.Event()  # add 唤醒 daemon

        self._flush_thread: Optional[threading.Thread] = None
        self._started = False

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """兼容接口。no-op。"""
        pass

    def add(self, msg: Any) -> None:
        """加一条群消息。同步、线程安全。首条消息启动慢 timer,
        如果是 @/关键词,同时启动快 timer (random(0, fast_jitter))。
        """
        with self._lock:
            if not self._buffer:
                # 窗口刚开:启动慢 timer
                self._window_deadline = time.time() + self._slow_base + random.uniform(0, self._slow_jitter)
                logger.debug(
                    "GroupMessageBuffer window start, slow deadline in %.1fs",
                    self._window_deadline - time.time(),
                )

            self._buffer.append(msg)

            # @ / 关键词 → fast timer = now + fast_base(8s) 固定, 不 random
            # 旧 bug: random(0,5) 导致 alpha @zero 2s 就 flush, zhp 3s 后来的 @zero 分成新 batch
            # 新逻辑: 固定 8s 缓冲, 同窗口内几秒连发的多条 @ 都合入同一 batch
            # 若 fast_deadline 已存在且> now(还没到), 不覆盖(取 max 保留更晚的)
            try:
                if self._is_priority(msg):
                    now_t = time.time()
                    proposed = now_t + self._fast_base
                    if self._fast_deadline is None:
                        # 第一次设 fast timer
                        self._fast_deadline = proposed
                    else:
                        # 已有 fast timer: 取 min — 新消息的 fast 可能更早
                        # 场景: buffer 里已有普通消息(slow timer 20s), 然后 @ 消息进来
                        # → fast 8s 更短, 应该提前到 8s 而不是等完 20s
                        self._fast_deadline = min(self._fast_deadline, proposed)
                    logger.debug(
                        "GroupMessageBuffer priority msg, fast deadline in %.2fs",
                        max(0, (self._fast_deadline or 0) - now_t),
                    )
            except Exception:
                pass

        # 唤醒 daemon thread
        self._flush_signal.set()
        self._ensure_started()

    def _ensure_started(self) -> None:
        if self._started:
            return
        self._started = True
        self._flush_thread = threading.Thread(
            target=self._flush_run,
            daemon=True,
            name="group-buffer-flush",
        )
        self._flush_thread.start()

    def _flush_run(self) -> None:
        """daemon thread 主循环。窗口内 wait 到任意 timer 到期或 stop。"""
        while not self._stop_flag.is_set():
            # 等首条消息进入窗口
            if self._stop_flag.wait(0.5):
                break
            if not self._flush_signal.is_set():
                continue
            self._flush_signal.clear()

            # 窗口内循环
            while not self._stop_flag.is_set():
                now = time.time()
                with self._lock:
                    slow_remaining = max(0.0, self._window_deadline - now)
                    fast_dl = self._fast_deadline

                # 没有窗口 (首条消息可能未到) 或 已 flush 后窗口归零
                if slow_remaining <= 0:
                    break

                fast_remaining = max(0.0, fast_dl - now) if fast_dl else float("inf")
                next_timeout = min(slow_remaining, fast_remaining)

                if next_timeout <= 0:
                    break

                # 等 add 唤醒 (新消息可能刷新 fast deadline) 或超时
                triggered = self._flush_signal.wait(timeout=next_timeout)
                if triggered:
                    self._flush_signal.clear()
                    # 新消息进来,循环重新读 fast deadline
                    continue
                else:
                    # 超时 = 到期 = flush
                    break

            if self._stop_flag.is_set():
                break

            # flush
            try:
                asyncio.run(self._flush_once())
            except Exception as exc:
                logger.warning("GroupMessageBuffer flush failed: %s", exc)

            # 窗口归零
            with self._lock:
                self._window_deadline = 0.0
                self._fast_deadline = None
            self._flush_signal.clear()

    async def _flush_once(self) -> None:
        """flush 全部 buffer 成单个 NormalizedMessage。"""
        with self._lock:
            if not self._buffer:
                return
            batch = list(self._buffer)
            self._buffer.clear()

        if not batch:
            return

        base = batch[0]
        if len(batch) == 1:
            await self._handler(base)
            return

        # 合并:所有条目以 {sender, text} 收集, base 字段换成最新一条
        merged_texts = []
        for m in batch:
            sender = getattr(m, "sender_name", "") or getattr(m, "sender_id", "") or "?"
            text = getattr(m, "content", "") or getattr(m, "text", "")
            msg_id = getattr(m, "message_id", "") or ""
            if text:
                merged_texts.append({"sender": sender, "text": text[:500], "msg_id": msg_id})
        try:
            latest = batch[-1]
            for field in ("content", "sender_name", "sender_id", "message_id"):
                if hasattr(latest, field):
                    setattr(base, field, getattr(latest, field))
            base.merged_texts = merged_texts
        except Exception as exc:
            logger.warning("GroupMessageBuffer merge failed, fallback per-msg: %s", exc)
            for m in batch:
                await self._handler(m)
            return

        logger.info(
            "GroupMessageBuffer flushed: %d msgs (priority=%s, merged=%d)",
            len(batch),
            any(self._is_priority(m) for m in batch),
            len(merged_texts),
        )
        await self._handler(base)

    def stop(self) -> None:
        """adapter stop 时停止 daemon thread + 吐残留。
        
        注意: stop() 可能在 async context (aiohttp shutdown) 里被调,
        不能用 asyncio.run() — 会 "cannot be called from a running event loop"。
        改成: 唤醒 daemon thread 让它自然 flush + 退出, stop 本身不 flush。
        daemon 线程关闭前会 flush 残留(如果窗口内有积压)。
        """
        self._stop_flag.set()
        self._flush_signal.set()  # 唤醒 daemon 让它处理残留 + 退出
        if self._flush_thread and self._flush_thread.is_alive():
            self._flush_thread.join(timeout=3.0)  # 给 daemon 3s flush 残留
