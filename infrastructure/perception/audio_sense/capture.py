"""录音管道 —— 通过 .app bundle 启动录音，FIFO 传 PCM。

核心设计：录音通过 AudioCaptureHelper.app 启动（`open` 命令 → LaunchServices）。
.app bundle 有独立的 TCC 身份 + NSMicrophoneUsageDescription，
macOS 会给它麦克风权限——解决了 master daemon（setsid）子进程拿不到麦克风的问题。

PCM 传输：录音脚本写到命名管道（FIFO），master 从 FIFO 读。
open 启动的进程没有 stdout pipe，所以用 FIFO。

为什么用 sd.rec（阻塞式）而非 sd.InputStream（回调式）：
  在某些进程环境下 InputStream 的 callback 会停止触发。
  sd.rec 预分配缓冲 + 阻塞 wait 更稳定。
"""
from __future__ import annotations

import logging
import os
import select
import subprocess
import threading
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000


class AudioCapture:
    """持续录音器：通过 .app bundle + FIFO 采集 PCM。

    用法：
        cap = AudioCapture(on_chunk=callback)
        cap.start()    # 创建 FIFO + open 启动 app + 读循环
        ...
        cap.stop()     # 停 app + 清理 FIFO
    """

    def __init__(
        self,
        on_chunk,
        *,
        chunk_read_size: int = 1600,
        app_bundle_path: str | Path | None = None,
    ) -> None:
        self._on_chunk = on_chunk
        self._chunk_read_size = chunk_read_size

        # .app bundle 路径（默认在 scripts/AudioCaptureHelper.app）
        if app_bundle_path is None:
            self._app_path = Path(__file__).resolve().parents[3] / "scripts" / "AudioCaptureHelper.app"
        else:
            self._app_path = Path(app_bundle_path)

        self._fifo_path: str | None = None
        self._reader_thread: threading.Thread | None = None
        self._running = threading.Event()
        self._opened_app = False

    def start(self, script_dir: Path | str = "/tmp") -> None:
        """启动：创建 FIFO → open 启动 app bundle → 读循环。"""
        if self._running.is_set():
            return
        self._running.set()

        # FIFO 用固定路径（app bundle 的默认路径，不依赖 --env 传递）
        self._fifo_path = "/tmp/_audio_sense_fifo"
        try:
            os.unlink(self._fifo_path)
        except FileNotFoundError:
            pass
        os.mkfifo(self._fifo_path)

        # 先杀掉可能残留的旧 app
        try:
            subprocess.run(["pkill", "-f", "audio_capture_helper"],
                          capture_output=True, timeout=3)
        except Exception:
            pass
        import time as _t
        _t.sleep(0.5)

        # 用 open 启动 .app bundle（LaunchServices → TCC 麦克风授权）
        if not self._app_path.exists():
            logger.error("AudioCaptureHelper.app not found: %s", self._app_path)
            self._running.clear()
            return

        try:
            subprocess.Popen(
                ["open", str(self._app_path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self._opened_app = True
            logger.info("AudioCaptureHelper.app launched (fifo=%s)", self._fifo_path)
        except Exception as exc:
            logger.error("open AudioCaptureHelper.app failed: %s", exc)
            self._running.clear()
            return

        # 读循环线程
        self._reader_thread = threading.Thread(
            target=self._read_loop, name="audio-sense-capture", daemon=True
        )
        self._reader_thread.start()

    def _read_loop(self) -> None:
        """从 FIFO 读 PCM，按 chunk_read_size 切块回调。"""
        assert self._fifo_path
        fd = None
        try:
            # 打开 FIFO 读端（阻塞直到 app 打开写端）
            fd = os.open(self._fifo_path, os.O_RDONLY)
            read_bytes = self._chunk_read_size * 2  # int16 → 每样本 2 字节
            leftover = b""

            while self._running.is_set():
                ready, _, _ = select.select([fd], [], [], 2.0)
                if not ready:
                    continue
                try:
                    raw = os.read(fd, read_bytes * 4)
                except OSError:
                    break
                if not raw:
                    break
                leftover += raw
                while len(leftover) >= read_bytes:
                    block = leftover[:read_bytes]
                    leftover = leftover[read_bytes:]
                    pcm = np.frombuffer(block, dtype="<i2")
                    try:
                        self._on_chunk(pcm)
                    except Exception:
                        logger.exception("on_chunk callback failed")
        except Exception:
            logger.exception("AudioCapture read loop crashed")
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except Exception:
                    pass

    def stop(self) -> None:
        """停止：杀 app + 清理 FIFO。"""
        if not self._running.is_set():
            return
        self._running.clear()

        # 权 app（用 osascript quit 或 pkill）
        if self._opened_app:
            try:
                subprocess.run(
                    ["osascript", "-e",
                     'tell application "AudioCaptureHelper" to quit'],
                    capture_output=True, timeout=5,
                )
            except Exception:
                pass
            try:
                subprocess.run(
                    ["pkill", "-f", "audio_capture_helper"],
                    capture_output=True, timeout=5,
                )
            except Exception:
                pass

        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=5)

        if self._fifo_path:
            try:
                os.unlink(self._fifo_path)
            except Exception:
                pass

        self._opened_app = False
        logger.info("AudioCapture stopped")

    @property
    def is_running(self) -> bool:
        return self._running.is_set()
