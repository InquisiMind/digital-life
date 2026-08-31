"""AudioSenseService —— master 级持续语音感知编排器。

组合所有管道，管理生命周期，提供 HTTP 控制端点。

数据流：
  AudioCapture（/usr/bin/python3 录音）
      │
      ├─ PCM 块 → VADSegmenter（L1-L3：能量→语音→端点）
      │               │
      │               └─ on_segment(audio) → AudioRouter.on_segment
      │                   （dormant 忽略 / dialog ASR+emit / focus ASR+emit+落盘）
      │
      └─ PCM 块 → KeywordSpotter（L4：sherpa-onnx KWS）
                      │
                      └─ on_hit → AudioRouter.on_keyword_hit
                          （dormant → 精转 → emit perception_signal → dialog）

生命周期：随 master 进程启停。在 gateway/master.py 或 server.py 启动路径里创建。
独立于 instance——实例重启不影响音频采集。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from infrastructure.perception.audio_sense.capture import AudioCapture
from infrastructure.perception.audio_sense.router import (
    AudioRouter, RouterConfig, RouterCallbacks, VoiceState,
)

logger = logging.getLogger(__name__)

TICK_INTERVAL_S = 2.0  # 超时检查间隔


@dataclass
class VoiceSenseConfig:
    """voice_sense.yaml 的解析配置。"""

    enabled: bool = False
    kws_model_dir: str = ""
    kws_keywords_file: str = ""
    kws_threshold: float = 0.5
    kws_use_int8: bool = True
    asr_engine: str = "cloud"
    dialog_timeout_s: float = 30.0
    focus_timeout_s: float = 60.0
    retention_days: int = 3
    quiet_hours_start: str = "23:00"
    quiet_hours_end: str = "08:00"
    default_instance: str = ""


def load_voice_sense_config(config_path: str | Path | None = None) -> VoiceSenseConfig:
    """加载 voice_sense.yaml。"""
    if config_path is None:
        config_path = Path(__file__).resolve().parents[3] / "config" / "voice_sense.yaml"
    config_path = Path(config_path)
    if not config_path.exists():
        logger.warning("voice_sense.yaml not found at %s, using defaults", config_path)
        return VoiceSenseConfig()

    try:
        import yaml
        cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.warning("parse voice_sense.yaml failed: %s", exc)
        return VoiceSenseConfig()

    kws = cfg.get("kws") or {}
    asr = cfg.get("asr") or {}
    dialog = cfg.get("dialog") or {}
    focus = cfg.get("focus") or {}
    quiet = cfg.get("quiet_hours") or {}

    return VoiceSenseConfig(
        enabled=bool(cfg.get("enabled", False)),
        kws_model_dir=kws.get("model_dir", ""),
        kws_keywords_file=kws.get("keywords_file", ""),
        kws_threshold=float(kws.get("threshold", 0.5)),
        kws_use_int8=bool(kws.get("use_int8", True)),
        asr_engine=asr.get("engine", "cloud"),
        dialog_timeout_s=float(dialog.get("timeout_seconds", 30)),
        focus_timeout_s=float(focus.get("timeout_seconds", 60)),
        retention_days=int(focus.get("retention_days", 3)),
        quiet_hours_start=quiet.get("start", "23:00"),
        quiet_hours_end=quiet.get("end", "08:00"),
        default_instance=cfg.get("default_instance", ""),
    )


class AudioSenseService:
    """持续语音感知编排器。

    生命周期：
        svc = AudioSenseService(config)
        svc.start()   # 开录音 + VAD + KWS + tick 线程
        ...
        svc.stop()    # 停所有管道

    线程模型：
        - capture 读循环线程（AudioCapture 内部）
        - VAD 在 on_chunk 回调里同步跑（capture 线程）
        - KWS 在 on_chunk 回调里同步跑（capture 线程，和 VAD 并行吃同一块）
        - tick 线程定期检查超时
    """

    def __init__(self, config: VoiceSenseConfig) -> None:
        self._config = config
        self._root = Path(__file__).resolve().parents[3]

        # 组件（延迟初始化，start() 里创建）
        self._capture: AudioCapture | None = None
        self._kws = None           # WakeWordDetector
        self._vad = None           # VADSegmenter
        self._router: AudioRouter | None = None
        self._keyword_map: dict[str, list[str]] = {}
        self._voice_session = None    # VoiceSession（手动会话模式）

        # 状态
        self._running = threading.Event()
        self._tick_thread: threading.Thread | None = None
        self._state_lock = threading.Lock()

    def start(self) -> None:
        """启动语音感知（如果 config.enabled=False 则跳过）。"""
        if not self._config.enabled:
            logger.info("AudioSenseService disabled (voice_sense.yaml enabled=false)")
            return
        if self._running.is_set():
            return
        self._running.set()

        try:
            self._init_components()
        except Exception:
            logger.exception("AudioSenseService init failed, aborting")
            self._running.clear()
            return

        # 启动录音（capture 的 on_chunk 会同时喂 VAD 和 KWS）
        script_dir = self._root / "var" / "run"
        self._capture.start(script_dir=script_dir)

        # tick 线程（检查超时）
        self._tick_thread = threading.Thread(
            target=self._tick_loop, name="audio-sense-tick", daemon=True
        )
        self._tick_thread.start()

        logger.info("AudioSenseService started (dormant mode)")

    def _init_components(self) -> None:
        """初始化 VAD / KWS / Router。"""
        root = self._root

        # ── KWS（sherpa-onnx 唤醒词检测）──
        from infrastructure.perception.audio_sense.kws import SherpaOnnxKWS

        kws_model = root / self._config.kws_model_dir if self._config.kws_model_dir else None
        kws_keywords = root / self._config.kws_keywords_file if self._config.kws_keywords_file else None
        if not kws_model or not kws_model.exists():
            raise FileNotFoundError(f"KWS model not found: {kws_model}")
        if not kws_keywords or not kws_keywords.exists():
            raise FileNotFoundError(f"keywords file not found: {kws_keywords}")

        self._kws = SherpaOnnxKWS(
            model_dir=kws_model,
            keywords_file=kws_keywords,
            use_int8=self._config.kws_use_int8,
            keywords_threshold=self._config.kws_threshold,
        )

        # ── VAD（复用 voice_session.VADSegmenter）──
        from infrastructure.perception.voice_session import VADSegmenter

        self._vad = VADSegmenter(
            on_segment=self._on_vad_segment,
            on_speech_start=self._on_speech_start,
            silence_frames=32,  # ~1s 静默才切段（默认 16=0.5s 太短，说话中途停顿就切了）
            min_speech_frames=3,
        )

        # ── 关键词 map（实例路由用）──
        from infrastructure.perception.voice_router import (
            build_instance_keyword_map, build_keyword_to_instance_map,
        )
        self._keyword_map = build_instance_keyword_map()
        self._keyword_to_instance = build_keyword_to_instance_map(self._keyword_map)
        logger.info("keyword→instance map: %d keywords", len(self._keyword_to_instance))

        # ── Router ──
        router_config = RouterConfig(
            dialog_timeout_s=self._config.dialog_timeout_s,
            focus_timeout_s=self._config.focus_timeout_s,
            default_instance=self._config.default_instance,
        )

        def _lookup_instance(keyword: str) -> str | None:
            from infrastructure.perception.voice_router import lookup_instance_by_keyword
            return lookup_instance_by_keyword(keyword, self._keyword_to_instance)

        router_callbacks = RouterCallbacks(
            transcribe=self._transcribe,
            emit_wake=self._emit_wake,
            emit_dialog=self._emit_dialog,
            persist=self._persist_segment,
            match_instance=self._match_instance,
            lookup_instance=_lookup_instance,
            on_state_change=self._on_state_change,
        )
        self._router = AudioRouter(router_config, router_callbacks)

        # ── Capture ──
        self._capture = AudioCapture(on_chunk=self._on_chunk)

    # ── PCM 块回调（capture 读循环线程）──────────────────────────────────
    def _on_chunk(self, pcm: np.ndarray) -> None:
        """收到一块 PCM(~100ms), feed VAD and KWS."""
        # 喂 VAD（L1-L3：能量→语音→端点）
        if self._vad:
            self._vad.feed(pcm)

        # 喂 KWS（L4：唤醒词检测）—— 只在 dormant 状态跑（省 CPU）
        # 2026-08-27 patch(alpha): 手动会话期间停 KWS——用户话语可能含唤醒词，
        # dormant 下 on_keyword_hit 仍生效，命中会把状态切回 dialog 导致双写注入。
        if (self._kws and self._router
                and self._router.state == VoiceState.DORMANT
                and self._voice_session is None):
            hit = self._kws.feed(pcm)
            if hit:
                self._router.on_keyword_hit(hit.keyword)

    def _on_vad_segment(self, audio: np.ndarray) -> None:
        """VAD 切出一个完整语音段。"""
        logger.info("VAD segment detected! samples=%d max=%d state=%s", len(audio), int(__import__('numpy').abs(audio).max()), self._router.state.value if self._router else "N/A")
        if self._router:
            self._router.on_segment(audio)

    def _on_speech_start(self) -> None:
        """VAD 检测到语音开始（SILENCE→SPEECH）→ 打断 TTS。"""
        try:
            from infrastructure.perception.voice_output import stop_playback
            stop_playback(grace_seconds=2.0)
        except Exception:
            logger.debug("stop_playback on speech start failed", exc_info=True)

    # ── Router 回调实现 ─────────────────────────────────────────────────
    def _transcribe(self, audio: np.ndarray) -> str:
        """L5 精确转写（云端 glm-asr）。"""
        try:
            import tempfile
            import wave
            from infrastructure.perception.asr import transcribe_file
            from infrastructure.perception.config import load_config

            # 写临时 wav（ASR 需要文件路径）
            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False, dir="/tmp")
            tmp.close()
            with wave.open(tmp.name, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(audio.tobytes())

            cfg = load_config(self._config.default_instance or "")
            out = transcribe_file(tmp.name, config=cfg, segment_paths=None)
            import os
            os.unlink(tmp.name)
            return out.get("text", "")
        except Exception:
            logger.exception("transcribe failed")
            return ""

    def _emit_wake(self, transcript: str, target_instance: str) -> None:
        """emit perception_signal（唤醒）。"""
        try:
            from infrastructure.perception.voice_router import emit_segment_to_instance
            from infrastructure.perception.config import load_config
            cfg = load_config(target_instance)
            emit_segment_to_instance(target_instance, transcript, "", cfg)
        except Exception:
            logger.exception("emit_wake failed")

    def _emit_dialog(self, transcript: str, target_instance: str) -> None:
        """emit group_message（对话流，群聊链路）。"""
        try:
            from domain.lifecycle.events import emit_event, set_instance_context
            from infrastructure.config import set_current_instance_id

            set_current_instance_id(target_instance)
            set_instance_context(target_instance)
            emit_event("group_message", {
                "source": "voice_sense",
                "text": transcript,
                "sender_name": "用户（语音）",
                "reply_channel": "voice",
            })
            logger.info("voice dialog emitted to %s: %s",
                        target_instance[:8], transcript[:40])
        except Exception:
            logger.exception("emit_dialog failed")

    def _persist_segment(self, audio: np.ndarray, seg_idx: int) -> str | None:
        """落盘语音段（专注模式）。"""
        try:
            from infrastructure.perception.voice_session import write_wav
            from infrastructure.perception.config import media_dir

            iid = self._config.default_instance or "voice_sense"
            out_dir = media_dir(iid) / f"focus_{int(time.time())}"
            out_dir.mkdir(parents=True, exist_ok=True)
            wav_path = out_dir / f"seg_{seg_idx:03d}.wav"
            write_wav(wav_path, audio)
            return str(wav_path)
        except Exception:
            logger.exception("persist segment failed")
            return None

    def _match_instance(self, transcript: str) -> str | None:
        """关键词匹配 → 实例。"""
        from infrastructure.perception.voice_router import match_instance
        return match_instance(transcript, self._keyword_map)

    def _on_state_change(self, old: VoiceState, new: VoiceState) -> None:
        """状态变化时写 state.json（供前端/调试查看）。"""
        # 2026-08-27 patch(alpha): stop-on-timeout —— 进入 DORMANT 即手动会话必须死。
        # 背景: 16:57 那场会话 17:02 结束后录音子进程 PID 19973 悬挂 1h46m——stop()
        #   只在 HTTP stop_session / service.stop() 两条路径被调，router 的
        #   dialog_timeout→dormant 只清路由状态不收会话（root cause by zero）。
        # 语义: 进 DORMANT = 会话必须死。全路径收编：dialog_timeout tick /
        #   force_dormant HTTP / 未来任何进 DORMANT 的转换。router 保持纯状态机。
        # 安全性（已验证）:
        #   1. _stop_voice_session() 自带 None guard，无会话时安全 no-op
        #   2. VoiceSession.stop() 有 _stop_lock + _running 守卫，幂等可重入
        #   3. router._set_state 的回调有 try/except 包裹，本补丁抛异常不伤状态机
        #   4. 最坏阻塞 ~10s（SIGTERM wait 5s + reader join 5s），tick/HTTP 线程可接受
        #   5. 失败留 WARN——对齐 phase2 v0.2 §5 fire-and-forget 教训
        if new is VoiceState.DORMANT and self._voice_session is not None:
            summary = self._stop_voice_session()
            if not summary.get("ok"):
                logger.warning("DORMANT transition but voice session stop failed: %s",
                               summary.get("error", "unknown"))
        try:
            from infrastructure.perception.config import media_dir
            import json
            iid = self._config.default_instance or "voice_sense"
            state_file = media_dir(iid) / "voice_sense_state.json"
            state_file.write_text(json.dumps({
                "state": new.value,
                "previous": old.value,
                "updated_at": time.time(),
            }, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    # ── tick 循环 ───────────────────────────────────────────────────────
    def _tick_loop(self) -> None:
        """定期检查超时 + 静默时段。"""
        while self._running.is_set():
            time.sleep(TICK_INTERVAL_S)
            try:
                if self._router:
                    self._router.check_timeout()
            except Exception:
                logger.exception("tick check_timeout failed")

    # ── HTTP 控制（实例 / 按键调用）──────────────────────────────────────
    def control(self, action: str, instance_id: str = "") -> dict[str, Any]:
        """HTTP 控制端点：切换状态 / 管理手动会话。

        Args:
            action: "focus" | "dialog" | "dormant" | "status"
                    | "start_session" | "stop_session"
            instance_id: 可选，指定实例（start_session 时必填）
        Returns:
            {"ok": bool, "state": str, "action": str, ...}
        """
        # ── 手动会话模式（Phase 1：快捷键 toggle）──
        if action == "start_session":
            return self._start_voice_session(instance_id)
        elif action == "stop_session":
            return self._stop_voice_session()

        # ── KWS 连续感知模式（dormant/dialog/focus 状态机）──
        if not self._router:
            return {"ok": False, "error": "service not running"}

        with self._state_lock:
            if action == "focus":
                self._router.enter_focus()
            elif action == "dialog":
                # 2026-08-27: 原映射 exit_focus() 有 DORMANT guard——dormant 下
                # control("dialog") 是 no-op，Phase 2 wake 钩子会静默失效。
                # enter_dialog() 无 guard 且覆盖 focus→dialog（exit_focus 超集）。
                # 2026-08-27 patch(alpha): 手动会话活跃时 dialog 请求 no-op——
                # start_session 已 force_dormant，若另一实例经 voice_focus("dialog")
                # （或 Phase 2 wake 钩子）把状态切回 dialog，on_segment 恢复路由，
                # 会话音频将双写注入。会话结束状态留 dormant，KWS 常开，
                # 喊唤醒词即可恢复 dialog——不需要 pending 补偿队列。
                if self._voice_session is not None:
                    logger.info("control(dialog) ignored: voice session active")
                else:
                    self._router.enter_dialog()
            elif action == "dormant":
                self._router.force_dormant()
            elif action == "status":
                pass  # 只读状态
            else:
                return {"ok": False, "error": f"unknown action: {action}"}

            return {
                "ok": True,
                "action": action,
                "state": self._router.state.value,
            }

    # ── 手动会话（VoiceSession）──────────────────────────────────────────
    def _start_voice_session(self, instance_id: str) -> dict[str, Any]:
        """启动手动语音会话：持续听 → VAD 分段 → ASR → 注入实例。

        Phase 1 的核心入口。用户按快捷键触发，VoiceSession 拉起录音子进程，
        VAD 自动分段，每段 ASR 转写后通过 emit_segment_to_instance 注入
        目标实例作为 perception_signal 事件。

        与 KWS 连续感知模式互斥：启动手动会话时自动切到 dormant（暂停 KWS 路由）。
        """
        if not instance_id:
            instance_id = getattr(self._config, 'default_instance', '') if self._config else ''
        if not instance_id:
            return {"ok": False, "error": "instance_id required for start_session"}

        if self._voice_session is not None:
            return {"ok": False, "error": "voice session already active",
                    "session_id": self._voice_session.session_id}

        try:
            from infrastructure.perception.voice_session import VoiceSession
            from infrastructure.perception.config import load_config

            cfg = load_config(instance_id)

            def _on_transcript(wav_path: str, text: str) -> None:
                """每段转写完成 → 注入实例作为 perception_signal。"""
                if not text or not text.strip():
                    logger.debug("empty transcript, skip injection")
                    return
                try:
                    from infrastructure.perception.voice_router import emit_segment_to_instance
                    emit_segment_to_instance(instance_id, text, wav_path, cfg)
                    logger.info("voice segment injected to %s: %s",
                                instance_id[:8], text[:40])
                except Exception:
                    logger.exception("emit_segment_to_instance failed")

            self._voice_session = VoiceSession(
                instance_id,
                config=cfg,
                on_transcript=_on_transcript,
                on_speech_start=self._on_speech_start,
            )
            self._voice_session.start()
            logger.info("voice session started for instance %s", instance_id[:8])

            # 2026-08-27 patch(alpha): 兑现 docstring 承诺的 KWS 互斥——
            # 手动会话期间强制 dormant，服务级 VAD 段不再路由（on_segment 在
            # dormant 下忽略），防止与 VoiceSession 双写注入同一句话。
            if self._router:
                try:
                    self._router.force_dormant()
                except Exception:
                    logger.exception("force_dormant on session start failed (non-fatal)")

            return {
                "ok": True,
                "action": "start_session",
                "state": "session_active",
                "session_id": self._voice_session.session_id,
                "instance_id": instance_id,
            }
        except Exception as exc:
            logger.exception("start_voice_session failed")
            self._voice_session = None
            return {"ok": False, "error": str(exc)}

    def _stop_voice_session(self) -> dict[str, Any]:
        """停止手动语音会话：停录音 → flush 尾段 → 返回摘要。"""
        if self._voice_session is None:
            return {"ok": False, "error": "no active voice session"}

        try:
            summary = self._voice_session.stop()
            logger.info("voice session stopped: %s (%d segments)",
                        summary.get("session_id", "?"), summary.get("segments", 0))
            self._voice_session = None
            return {
                "ok": True,
                "action": "stop_session",
                "state": "session_stopped",
                **summary,
            }
        except Exception as exc:
            logger.exception("stop_voice_session failed")
            self._voice_session = None
            return {"ok": False, "error": str(exc)}

    def stop(self) -> None:
        """停止所有管道。"""
        if not self._running.is_set():
            return
        self._running.clear()

        # 清理活跃的 voice session（Zero 8/18 集成验证发现）
        if self._voice_session is not None:
            try:
                self._stop_voice_session()
            except Exception as e:
                logger.warning("Failed to cleanup voice_session during stop: %s", e)

        if self._capture:
            self._capture.stop()
        if self._tick_thread and self._tick_thread.is_alive():
            self._tick_thread.join(timeout=5)

        # flush VAD 尾段
        if self._vad:
            try:
                self._vad.finish()
            except Exception:
                pass

        logger.info("AudioSenseService stopped")

    @property
    def state(self) -> str:
        """当前状态（dormant/dialog/focus/disabled）。"""
        if not self._running.is_set():
            return "disabled"
        return self._router.state.value if self._router else "disabled"