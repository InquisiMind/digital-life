"""录音中实时转写（低延迟路径）。

目标：快捷键按停 → 事件触发 < 1s。原理：

  传统路径：停止 → 上传整个 wav → 云端推理（延迟 ∝ 音频时长）
  本路径：  录音子进程流式写 wav → tail 线程增量读 PCM → VAD 找停顿
            → 每积累 ≥ min_segment_seconds 在停顿处切一段 → 后台立刻调
            云端 ASR（带上文 prompt）→ 停止时只剩最后一段没转
            → finalize 尾段 + 拼接 → 调用方直接 emit

切点全部落在 ≥0.8s 的静音处（VADSegmenter 保证），不会劈开词语；
延迟 ≈ 尾段（通常 2-5s）的 ASR 耗时，与录音总时长无关。

线程模型（3 个）：
  tail 线程    读增长中的 wav（跳过 44 字节头），喂 VAD，切段入队
  worker 线程  串行调云端 ASR（同一时刻最多 1 个请求，prompt 逐段续链）
  调用方线程   stop_and_finalize()：flush 尾巴 + 等 worker 排空 + 拼接

任何环节失败 → 返回空串，调用方 fallback 到整文件转写（daemon 兜底）。
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable

import numpy as np

from infrastructure.perception.config import PerceptionConfig

logger = logging.getLogger(__name__)

# wav 头长度（PCM mono 16bit 标准 44 字节）。tail 读原始 PCM 用，不解析头。
WAV_DATA_OFFSET = 44
SAMPLE_RATE = 16000
# 每次 tail 读取的字节数（6400B = 3200 样本 = 0.2s，2 字节对齐）
TAIL_CHUNK_BYTES = 6400


class LiveTranscriber:
    """对一个"正在增长中的 wav"做停顿切割 + 后台逐段转写。

    用法::

        lt = LiveTranscriber(audio_path, config=cfg)
        lt.start()                    # 录音开始后调用
        ...
        text = lt.stop_and_finalize() # 录音结束后调用（阻塞到全部段转完）
    """

    def __init__(
        self,
        audio_path: str | Path,
        *,
        config: PerceptionConfig,
        min_segment_seconds: float = 3.0,
        silence_frames: int = 25,
        poll_interval: float = 0.1,
        transcribe_fn: Callable[..., str] | None = None,
        vad: Any = None,
    ) -> None:
        self.audio_path = Path(audio_path)
        self.config = config
        self.min_segment_seconds = min_segment_seconds
        self.poll_interval = poll_interval
        self._transcribe_fn = transcribe_fn  # 测试注入；None → 云端
        self._vad_ext = vad  # 测试注入；None → VADSegmenter

        self._silence_frames = silence_frames
        self._seg_dir = self.audio_path.parent / (self.audio_path.stem + "_live")
        self._seg_dir.mkdir(parents=True, exist_ok=True)

        # 切割状态（tail 线程独占写，finalize 前调用方不碰）
        self._buffer: list[np.ndarray] = []      # 已结束的 VAD 语音段（未满一批）
        self._buffered_samples = 0

        # worker 队列：[(seg_idx, seg_path), ...]，None 为结束哨兵
        self._queue: queue.Queue = queue.Queue()
        self._seg_idx = 0
        self._results: dict[int, str] = {}
        self._prev_text = ""  # 上一段结果 → 下一段 prompt（跨段连贯）

        self._stopping = False
        self._tail_thread: threading.Thread | None = None
        self._worker_thread: threading.Thread | None = None
        self._started = False

    # ── 生命周期 ────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()
        self._tail_thread = threading.Thread(target=self._tail_loop, daemon=True)
        self._tail_thread.start()

    def stop_and_finalize(self, *, timeout: float = 30.0) -> str:
        """录音结束后调用：flush 尾段 → 等 worker 转完 → 按序拼接全文。

        失败/无语音返回 ""（调用方 fallback 整文件路径）。
        """
        self._stopping = True
        if self._tail_thread:
            self._tail_thread.join(timeout=10)

        # VAD flush：还在说的尾巴也是一段（尾部切割 → 停止后只转这一段）
        try:
            if self._vad is not None:
                self._vad.finish()
        except Exception as exc:
            logger.warning("live VAD finish failed: %s", exc)
        self._flush_buffer()

        self._queue.put(None)  # worker 哨兵
        if self._worker_thread:
            self._worker_thread.join(timeout=timeout)

        if not self._results:
            return ""
        text = "\n".join(t for _, t in sorted(self._results.items()) if t).strip()
        return text

    # ── 内部：tail 读增长中的 wav → VAD ────────────────────────────────────

    @property
    def _vad(self) -> Any:
        """惰性建 VAD（避免在 __init__ 里加载 ONNX，测试可不触发）。"""
        if self._vad_ext is not None:
            return self._vad_ext
        from infrastructure.perception.voice_session import VADSegmenter

        self._vad_ext = VADSegmenter(
            on_segment=self._on_vad_segment,
            silence_frames=self._silence_frames,
        )
        return self._vad_ext

    def _tail_loop(self) -> None:
        """轮询读增长中的 wav：跳过 44 字节头，增量 PCM 喂 VAD。"""
        import wave as _wave

        # 等录音子进程创建文件（最多 5s）
        deadline = time.time() + 5.0
        while not self.audio_path.exists() and time.time() < deadline:
            time.sleep(0.05)
        if not self.audio_path.exists():
            logger.warning("live transcribe: audio file never appeared: %s", self.audio_path)
            return

        try:
            f = open(self.audio_path, "rb")
        except Exception as exc:
            logger.warning("live transcribe: open failed: %s", exc)
            return

        with f:
            f.seek(WAV_DATA_OFFSET)
            while True:
                raw = f.read(TAIL_CHUNK_BYTES)
                if raw:
                    # 奇数字节（理论不会发生，wav 数据 2 字节对齐）丢弃防错位
                    if len(raw) % 2:
                        raw = raw[:-1]
                    if raw:
                        pcm = np.frombuffer(raw, dtype=np.int16)
                        try:
                            self._vad.feed(pcm)
                        except Exception:
                            logger.exception("live VAD feed failed, abort tail")
                            return
                elif self._stopping:
                    break
                else:
                    time.sleep(self.poll_interval)

    # ── 内部：切割决策 + 派发 ──────────────────────────────────────────────

    def _on_vad_segment(self, audio: np.ndarray) -> None:
        """VAD 切出一段完整的话（前置 ≥0.8s 静音保证切点安全）。

        积累 ≥ min_segment_seconds 才派发一批——避免每句一请求；
        不足则继续攒，等下一个停顿。
        """
        self._buffer.append(audio)
        self._buffered_samples += len(audio)
        if self._buffered_samples >= self.min_segment_seconds * SAMPLE_RATE:
            self._flush_buffer()

    def _flush_buffer(self) -> None:
        """把攒的语音段合成一批 → 写 seg wav → 入队转写。"""
        if not self._buffer:
            return
        audio = np.concatenate(self._buffer)
        self._buffer = []
        self._buffered_samples = 0

        idx = self._seg_idx
        self._seg_idx += 1
        seg_path = self._seg_dir / f"seg_{idx:03d}.wav"
        try:
            from infrastructure.perception.voice_session import write_wav

            write_wav(seg_path, audio)
        except Exception as exc:
            logger.warning("live transcribe: write seg %d failed: %s", idx, exc)
            return
        self._queue.put((idx, str(seg_path)))

    # ── 内部：串行 ASR worker ──────────────────────────────────────────────

    def _worker_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            idx, seg_path = item
            try:
                text = self._transcribe_one(seg_path)
            except Exception as exc:
                logger.warning("live ASR seg %d failed: %s", idx, exc)
                text = ""
            if text:
                self._results[idx] = text
                self._prev_text = text  # 下一段 prompt

    def _transcribe_one(self, seg_path: str) -> str:
        audio_bytes = Path(seg_path).read_bytes()
        if self._transcribe_fn is not None:
            return self._transcribe_fn(
                audio_bytes, filename=Path(seg_path).name,
                config=self.config, prompt=self._prev_text,
            )
        from infrastructure.perception.asr import transcribe_segment

        return transcribe_segment(
            audio_bytes, filename=Path(seg_path).name,
            config=self.config, prompt=self._prev_text,
        )
