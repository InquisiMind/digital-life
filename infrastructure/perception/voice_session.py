"""实时语音会话（Voice Session）—— 持续听 + VAD 自动分段 + 逐段 ASR。

定位（spec 阶段 1）：用户按一次快捷键进入"持续会话模式"。麦克风持续开着，
Silero VAD 实时检测说话段，每检测到一句完整的话（说话 + 停顿）就：
  1. 把这段音频 flush 成 wav 文件
  2. 调 ASR（glm-asr-2512）转写
  3. 累积追加到 ``apps/<id>/data/perception/session_{ts}.txt``

后续阶段（关键词路由、TTS 打断）在此基础上扩展；本模块只交付阶段 1 的
"听 → 分段 → 转写 → 落盘"，保证端到端可测。

为什么不用 torch hub 加载 Silero：
  沙箱无外网，torch 也未安装。``silero-vad`` pip 包自带 ONNX 权重
  （``data/silero_vad.onnx``，~2MB），我们直接用 onnxruntime 加载，
  零网络依赖、CPU <0.1ms/帧（实测 RTF≈0.003）。

为什么录音走 /usr/bin/python3 子进程：
  macOS TCC 麦克风权限绑定到二进制签名。主解释器（miniconda）拿不到授权；
  /usr/bin/python3 是 Apple 签名的 Xcode shim，天然有麦克风权限。
  和 daemon.py 的单次录制路径一致（参见 ``_audio_loop``）。

数据流：
  /usr/bin/python3 record_stream.py   →   stdout 原始 int16 PCM (16k mono)
        │
  VoiceSession._read_loop 读 stdout    →   按 512 样本切片喂 VAD
        │
  VADSegmenter 检测语音段起止          →   on_segment(bytes) 回调
        │
  VoiceSession._on_segment             →   写 wav → ASR → 追加 session 文档
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import wave
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import numpy as np

from infrastructure.perception.asr import transcribe_file
from infrastructure.perception.config import PerceptionConfig, load_config, media_dir

logger = logging.getLogger(__name__)

# ── Silero VAD 常量（模型固定约束）──────────────────────────────────────────
# Silero VAD v4/v5 只接受 16kHz 单声道；每帧 512 样本（32ms）。
VAD_SAMPLE_RATE = 16000
VAD_CHUNK_SAMPLES = 512
# Silero VAD onnx 权重在 silero-vad pip 包里（不 import 该包顶层——它 import torch）。
# 我们用 importlib.resources 定位；兜底用已知路径。
_SILERO_ONNX_FILENAME = "silero_vad.onnx"


# ── VAD 阈值（经验值，后续可由 config 覆盖）───────────────────────────────
# Silero 输出是 [0,1] 的"这段音频是语音"概率。
DEFAULT_VAD_THRESHOLD = 0.5      # ≥ 此值视为"有人在说话"
# 检测到说话后，连续多少帧低于阈值才算"这句话说完了"。
# 16kHz/512 样本 = 31.25 帧/秒 → 16 帧 ≈ 0.5s 静音收尾。
DEFAULT_SILENCE_FRAMES = 16
# 连续多少帧高于阈值才算"真正开始说话"（滤掉咳嗽/键盘等瞬时噪声）。
DEFAULT_MIN_SPEECH_FRAMES = 3
# 一段话最长多少秒强制截断（防止有人一直说不停，ASR 30s 上限也兜底）。
DEFAULT_MAX_SPEECH_SECONDS = 25.0
# 一段话最短多少帧才送 ASR（太短多半是噪声，丢弃省调用）。
DEFAULT_MIN_SPEECH_FRAMES_TO_KEEP = 6  # ≈ 0.2s


def _find_silero_onnx() -> Path:
    """定位 silero-vad 包自带的 ONNX 权重文件。

    优先 importlib.resources（包升级后路径仍稳）；兜底已知安装路径。
    找不到抛 FileNotFoundError —— VoiceSession 构造时即暴露，不拖到运行期。
    """
    # 1. importlib.resources（推荐路径，跟随包版本）
    try:
        import importlib.resources as _res

        try:  # py3.9+ files() API
            ref = _res.files("silero_vad") / "data" / _SILERO_ONNX_FILENAME
            with _res.as_file(ref) as p:  # type: ignore[arg-type]
                if p.exists():
                    return p.resolve()
        except Exception:
            pass
    except Exception:
        pass

    # 2. 兜底：site-packages 已知结构
    import importlib.util as _ilu

    spec = _ilu.find_spec("silero_vad")
    if spec and spec.submodule_search_locations:
        for base in spec.submodule_search_locations:
            cand = Path(base) / "data" / _SILERO_ONNX_FILENAME
            if cand.exists():
                return cand.resolve()

    raise FileNotFoundError(
        "找不到 silero_vad.onnx —— 请安装 silero-vad 包（pip install silero-vad），"
        "其自带 ONNX 权重，无需联网下载。"
    )


class VADSegmenter:
    """增量 Silero VAD：喂 512 样本的块，输出完整语音段。

    用法：
        seg = VADSegmenter(on_segment=callback)
        for chunk in audio_stream:
            seg.feed(chunk)  # chunk: np.ndarray (N,) int16 或 float32
        seg.finish()  # flush 尾巴

    回调签名：``on_segment(audio_int16: np.ndarray)`` —— 收到的是一句完整话的
    int16 PCM（16k mono），调用方负责写 wav / 送 ASR。

    设计要点：
      - 增量处理，不存整段音频流（长会话内存可控）
      - 状态机：SILENCE → IN_SPEECH → SILENCE（触发 on_segment）
      - 阈值 + 帧数双重判定，滤掉瞬时噪声和过短片段
    """

    def __init__(
        self,
        on_segment: Callable[[np.ndarray], None],
        *,
        threshold: float = DEFAULT_VAD_THRESHOLD,
        silence_frames: int = DEFAULT_SILENCE_FRAMES,
        min_speech_frames: int = DEFAULT_MIN_SPEECH_FRAMES,
        min_speech_frames_to_keep: int = DEFAULT_MIN_SPEECH_FRAMES_TO_KEEP,
        max_speech_seconds: float = DEFAULT_MAX_SPEECH_SECONDS,
        sample_rate: int = VAD_SAMPLE_RATE,
        model_path: Path | str | None = None,
        on_speech_start: Callable[[], None] | None = None,
    ) -> None:
        self._on_segment = on_segment
        self._on_speech_start = on_speech_start  # SILENCE→SPEECH 首次跳变时调（打断 TTS 用）
        self.threshold = threshold
        self.silence_frames = silence_frames
        self.min_speech_frames = min_speech_frames
        self.min_speech_frames_to_keep = min_speech_frames_to_keep
        self.max_speech_samples = int(max_speech_seconds * sample_rate)
        self.sample_rate = sample_rate

        # 加载 ONNX 模型（延迟到构造，便于测试注入 model_path）
        import onnxruntime as ort

        mp = str(model_path) if model_path else str(_find_silero_onnx())
        self._sess = ort.InferenceSession(mp, providers=["CPUExecutionProvider"])
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._sr_arr = np.array(sample_rate, dtype=np.int64)

        # 状态机
        self._in_speech = False
        self._speech_buf: list[np.ndarray] = []   # 累积当前段的 int16 帧
        self._speech_samples = 0
        self._consec_speech = 0                    # 连续高概率帧
        self._consec_silence = 0                   # 连续低概率帧（IN_SPEECH 时计）

    # ── 核心：喂一个 512 样本的块 ──────────────────────────────────────────
    def feed(self, chunk: np.ndarray) -> None:
        """喂一个音频块（int16 或 float32，任意长度，内部按 512 切）。

        超过 512 的块会切成多帧逐个判定；不足 512 的尾块用零填充。
        """
        # 统一成 float32 [-1,1]（模型输入）；int16 → /32768
        if chunk.dtype != np.float32:
            f = chunk.astype(np.float32)
            if chunk.dtype == np.int16:
                f = f / 32768.0
            chunk = f

        i = 0
        n = len(chunk)
        while i < n:
            block = chunk[i:i + VAD_CHUNK_SAMPLES]
            i += VAD_CHUNK_SAMPLES
            if len(block) < VAD_CHUNK_SAMPLES:
                # 尾块不足 512 → 补零（模型要求定长）
                block = np.pad(block, (0, VAD_CHUNK_SAMPLES - len(block)))
            self._process_block(block)

    def _process_block(self, block: np.float32) -> None:
        """判定一帧（512 样本）并更新状态机。"""
        out = self._sess.run(
            ["output", "stateN"],
            {
                "input": block.reshape(1, -1).astype(np.float32),
                "state": self._state,
                "sr": self._sr_arr,
            },
        )
        prob = float(out[0][0].flatten()[0])
        self._state = out[1]  # 状态传递给下一帧（关键：VAD 是有状态的）

        is_speech = prob >= self.threshold
        # 保存原始 int16（用于落盘），从 float 还原（避免精度问题，块都来自 int16 源）
        block_int16 = (block * 32768.0).clip(-32768, 32767).astype(np.int16)

        if not self._in_speech:
            if is_speech:
                self._consec_speech += 1
                if self._consec_speech >= self.min_speech_frames:
                    # 真正开始说话：把之前缓存的"可能属于本段"的前导帧也算上
                    self._in_speech = True
                    self._speech_buf.append(block_int16)
                    self._speech_samples += VAD_CHUNK_SAMPLES
                    # SILENCE→SPEECH 首次跳变 → 通知打断 TTS（用户开口了）
                    if self._on_speech_start:
                        try:
                            self._on_speech_start()
                        except Exception:
                            logger.exception("on_speech_start callback failed")
            else:
                self._consec_speech = 0
        else:
            # IN_SPEECH：持续累积；统计连续静音帧
            self._speech_buf.append(block_int16)
            self._speech_samples += VAD_CHUNK_SAMPLES
            if is_speech:
                self._consec_silence = 0
            else:
                self._consec_silence += 1
                if self._consec_silence >= self.silence_frames:
                    self._emit_segment()
                    self._reset_speech()

            # 超长截断（防止无限说话 + 兜底 ASR 30s 上限）
            if self._speech_samples >= self.max_speech_samples:
                self._emit_segment()
                self._reset_speech()

    def _emit_segment(self) -> None:
        """把当前累积的语音段回调出去（过短的丢弃）。"""
        if self._speech_samples < self.min_speech_frames_to_keep * VAD_CHUNK_SAMPLES:
            return  # 太短，多半是噪声
        audio = np.concatenate(self._speech_buf)
        try:
            self._on_segment(audio)
        except Exception:
            logger.exception("on_segment callback failed")

    def _reset_speech(self) -> None:
        self._in_speech = False
        self._speech_buf = []
        self._speech_samples = 0
        self._consec_speech = 0
        self._consec_silence = 0

    def finish(self) -> None:
        """结束喂料时调用：若正在说话，把尾巴也 flush 出去。"""
        if self._in_speech and self._speech_samples > 0:
            self._emit_segment()
        self._reset_speech()


# ── 录音子进程脚本（写到临时文件执行，避免 f-string 缩进问题）─────────────────
def _write_stream_script(path: Path) -> None:
    """生成录音流脚本：16kHz mono int16，原始 PCM 写 stdout，SIGTERM 优雅停。

    脚本必须独立可读（不用 f-string 拼接，避免缩进/转义坑），和 daemon.py
    的 ``_audio_loop`` 同款写法。
    """
    path.write_text(
        "import sys, time, signal\n"
        "import sounddevice as sd\n"
        "SR = 16000\n"
        "recording = [True]\n"
        "def stop(sig, frame):\n"
        "    recording[0] = False\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "def callback(indata, frames, time_info, status):\n"
        "    if not recording[0]:\n"
        "        raise sd.CallbackStop\n"
        "    sys.stdout.buffer.write(indata.tobytes())\n"
        "    sys.stdout.buffer.flush()\n"
        "try:\n"
        "    with sd.InputStream(samplerate=SR, channels=1, dtype='int16',\n"
        "                        blocksize=0, callback=callback, latency='low'):\n"
        "        while recording[0]:\n"
        "            time.sleep(0.1)\n"
        "except Exception as e:\n"
        "    sys.stderr.write('STREAM_ERROR: ' + str(e) + '\\n')\n",
        encoding="utf-8",
    )


# ── 临时 wav 写入（纯函数，可单测）─────────────────────────────────────────
def write_wav(path: str | Path, audio: np.ndarray, sample_rate: int = VAD_SAMPLE_RATE) -> None:
    """把 int16 PCM 写成 wav 文件（mono）。供 ASR 的 transcribe_file 消费。"""
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(audio.tobytes())


# ── VoiceSession：会话编排器 ───────────────────────────────────────────────
class VoiceSession:
    """一次实时语音会话：持续录音 → VAD 分段 → ASR → 落盘。

    生命周期：
        sess = VoiceSession(instance_id)
        sess.start()        # 开子进程录音 + 读循环线程
        ...
        sess.stop()         # 停子进程，flush 尾段，关闭文档

    线程模型：录音在子进程；主进程一个读循环线程把 PCM 喂 VAD。ASR 在
    读循环线程里同步调（每段一次 httpx，~1s），简单可靠；后续阶段可改线程池。

    落盘文档：``apps/<id>/data/perception/session_{YYYYmmdd_HHMMSS}.txt``，
    每段转写结果以时间戳标记追加，实例可通过 sense_file 读取获取上下文。
    """

    def __init__(
        self,
        instance_id: str,
        *,
        config: PerceptionConfig | None = None,
        segmenter: VADSegmenter | None = None,
        on_transcript: Callable[[str, str], None] | None = None,
        on_speech_start: Callable[[], None] | None = None,
        python_bin: str = "/usr/bin/python3",
    ) -> None:
        self.instance_id = instance_id
        self.config = config or load_config(instance_id)
        self._on_transcript = on_transcript  # 回调：(segment_audio_path, text)
        self._on_speech_start = on_speech_start  # 回调：用户开口（打断 TTS 用）
        self._python_bin = python_bin

        self._media = media_dir(instance_id)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session_id = f"session_{ts}"
        self.doc_path = self._media / f"{self.session_id}.txt"
        # 段音频目录：session_<ts>/seg_xxx.wav
        self._seg_dir = self._media / self.session_id
        self._seg_dir.mkdir(parents=True, exist_ok=True)

        self._seg_counter = 0
        self._doc_lock = threading.Lock()
        self._init_doc()

        # segmenter 注入：测试可传 mock；生产由 VADSegmenter 实例化
        self._segmenter = segmenter or VADSegmenter(
            on_segment=self._on_segment, on_speech_start=self._on_speech_start
        )

        # 子进程 + 读线程
        self._proc: subprocess.Popen | None = None
        self._script_path: Path | None = None
        self._reader_thread: threading.Thread | None = None
        self._running = threading.Event()
        self._stop_lock = threading.Lock()

    # ── 文档初始化 / 追加 ─────────────────────────────────────────────────
    def _init_doc(self) -> None:
        header = (
            f"# 语音会话转录 {self.session_id}\n"
            f"# 实例：{self.instance_id}\n"
            f"# 开始：{datetime.now().isoformat(timespec='seconds')}\n"
            f"# （由 Silero VAD 自动分段 + glm-asr 转写生成）\n\n"
        )
        self.doc_path.write_text(header, encoding="utf-8")

    def _append_doc(self, seg_idx: int, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        block = f"## [{stamp}] 段 {seg_idx}\n{text}\n\n"
        with self._doc_lock:
            with open(self.doc_path, "a", encoding="utf-8") as f:
                f.write(block)

    # ── 段回调：写 wav → ASR → 落盘 ────────────────────────────────────────
    def _on_segment(self, audio: np.ndarray) -> None:
        """VAD 检测到一句完整的话时回调。"""
        self._seg_counter += 1
        seg_idx = self._seg_counter
        wav_path = self._seg_dir / f"seg_{seg_idx:03d}.wav"
        write_wav(wav_path, audio, VAD_SAMPLE_RATE)
        dur = len(audio) / VAD_SAMPLE_RATE
        logger.info("voice segment %d: %.1fs (%d samples)", seg_idx, dur, len(audio))

        # ASR 转写
        text = ""
        try:
            out = transcribe_file(wav_path, config=self.config, segment_paths=None)
            text = (out.get("text") or "").strip()
        except Exception as exc:
            logger.warning("ASR failed for segment %d: %s", seg_idx, exc)
            text = f"（转写失败：{exc}）"

        self._append_doc(seg_idx, text or "（语音转写为空）")
        logger.info("segment %d transcript: %s", seg_idx, (text[:60] + "…") if len(text) > 60 else text)

        # 通知上层（后续阶段：关键词路由用）
        if self._on_transcript:
            try:
                self._on_transcript(str(wav_path), text)
            except Exception:
                logger.exception("on_transcript callback failed")

    # ── 录音 + 读循环 ─────────────────────────────────────────────────────
    def start(self) -> None:
        """启动：拉起录音子进程 + 读循环线程。"""
        if self._running.is_set():
            return
        self._running.set()

        # 写录音脚本
        self._script_path = self._seg_dir / "_record_stream.py"
        _write_stream_script(self._script_path)

        self._proc = subprocess.Popen(
            [self._python_bin, str(self._script_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,  # 不缓冲：PCM 要实时读
        )
        logger.info("voice session started: %s (pid=%d)", self.session_id, self._proc.pid)

        self._reader_thread = threading.Thread(
            target=self._read_loop, name=f"voice-{self.session_id}", daemon=True
        )
        self._reader_thread.start()

    def _read_loop(self) -> None:
        """从子进程 stdout 读 PCM，按 512 样本喂 VAD。

        读循环在子进程 EOF（被 stop）后自然结束；结束后 flush VAD 尾段。
        """
        assert self._proc and self._proc.stdout
        try:
            chunk_bytes = b""
            # PCM int16 → 每样本 2 字节；一次读 VAD_CHUNK_SAMPLES 帧的字节
            read_size = VAD_CHUNK_SAMPLES * 2
            while self._running.is_set():
                raw = self._proc.stdout.read(read_size)
                if not raw:
                    break  # EOF：子进程结束
                # 可能读到非对齐长度（奇数字节），缓存拼齐
                chunk_bytes += raw
                usable = (len(chunk_bytes) // 2) * 2
                if usable < read_size:
                    continue
                block = chunk_bytes[:read_size]
                chunk_bytes = chunk_bytes[read_size:]
                audio = np.frombuffer(block, dtype="<i2")
                self._segmenter.feed(audio)
        except Exception:
            logger.exception("voice read loop crashed")
        finally:
            # flush 尾段
            try:
                self._segmenter.finish()
            except Exception:
                logger.exception("segmenter.finish failed")

    def stop(self) -> dict[str, Any]:
        """停止：SIGTERM 子进程 + 等读循环结束。返回会话摘要。"""
        with self._stop_lock:
            if not self._running.is_set():
                return self._summary()
            self._running.clear()

            # 停子进程（SIGTERM 让 callback 优雅退出，read 自然 EOF）
            if self._proc and self._proc.poll() is None:
                try:
                    os.kill(self._proc.pid, signal.SIGTERM)
                except Exception:
                    pass
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass

            # 等读循环 flush 完
            if self._reader_thread and self._reader_thread.is_alive():
                self._reader_thread.join(timeout=5)

            # 关闭文档
            with self._doc_lock:
                with open(self.doc_path, "a", encoding="utf-8") as f:
                    f.write(
                        f"\n# 会话结束：{datetime.now().isoformat(timespec='seconds')}\n"
                        f"# 共 {self._seg_counter} 段\n"
                    )
            logger.info("voice session stopped: %s (%d segments)", self.session_id, self._seg_counter)
            return self._summary()

    def _summary(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "doc_path": str(self.doc_path),
            "segments": self._seg_counter,
            "seg_dir": str(self._seg_dir),
        }
