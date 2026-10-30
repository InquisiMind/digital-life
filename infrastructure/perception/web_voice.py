"""网页全双工语音会话：浏览器 PCM ⇄ RTASR LLM 流 ⇄ 实例事件。

设计（2026-10-28，spec：语音对话入口前端化）：
  - 转写与断句全部交给讯飞 RTASR 大模型版（rtasr_llm.py）：type=1 中间结果
    推给浏览器做实时字幕，type=0 确认句即断句点 → 注入目标实例。
  - 注入复用 voice_router.emit_segment_to_instance（perception_signal +
    reply_channel=voice），额外带 quick_first_reply 标记让唤醒首轮降档速答
    （scheduler/agent 侧消费）。
  - 回声闸门：小张的 TTS 播在本地扬声器，会被浏览器麦克风收回去转写成
    "假句子"。worker 播放时写 var/run/voice/tts_active_<iid> 标记（见
    voice_output._register_playback），本模块在注入前查标记——播放中确认句
    直接丢弃。打断场景（真人压过 TTS 说话）由 barge-in 停播 → 标记立即
    消失 → 句子照样注入。
  - 单槽位：同一时刻只允许一个网页语音会话（对齐 AudioSenseService 的
    _voice_session 惯例）；第二个连接收到 busy 后关闭。

线程模型：feed_pcm 可从 aiohttp event loop 直接调（只入队，不阻塞）；
回调在 RTASR 接收线程里执行，下行推送由调用方注入的 push_client 负责
（routes 层用 loop.call_soon_threadsafe 编排回 event loop）。

下行 JSON 消息类型（voice_ws_routes.py 文档化，前端 voice-console 消费）：
  hello / partial / sentence / barge_in / echo_dropped / other_speaker /
  asr_error / event_sent(+text) / agent_step / thinking_delta / thinking_end /
  agent_reply / tts_segment / stop_tts / error
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from infrastructure.perception.config import PerceptionConfig

logger = logging.getLogger(__name__)

# TTS 标记视为新鲜的窗口（秒）：超过即当作残留（worker 崩溃没清）
TTS_MARKER_MAX_AGE_S = 30.0

# 对话历史环形缓冲容量（hub 级，跨会话存活：页面刷新后对话流可恢复）
HISTORY_MAX_ITEMS = 200


def tts_marker_path(instance_id: str) -> Path:
    """worker 播放 TTS 时写的标记文件路径（var/run/voice/tts_active_<iid>）。"""
    from infrastructure.config import get_project_root

    return get_project_root() / "var" / "run" / "voice" / f"tts_active_{instance_id}"


def is_tts_active(instance_id: str) -> bool:
    """目标实例的本地 TTS 是否正在播（标记存在且新鲜）。"""
    marker = tts_marker_path(instance_id)
    try:
        if not marker.exists():
            return False
        return (time.time() - marker.stat().st_mtime) < TTS_MARKER_MAX_AGE_S
    except OSError:
        return False


class WebVoiceSession:
    """一个浏览器连接 = 一个会话：PCM 上行 → RTASR；字幕/状态下行。"""

    _next_token = 0

    # 能量门控的说话态余晖（秒）：人声过门槛后这么久内的静音帧照常喂讯飞
    # （断句需要句尾静音），持续安静超时才回拦截态挡杂音
    SPEAK_HOLD_S = 8.0

    # 假打断恢复宽限（秒）：覆盖率级 barge-in 停播后，这么长时间内没有
    # 后续人声文本（新 partial 字 / 新确认句）= 误判 → 续播被打断的剩余段。
    # 停止指令（"停"）和白字即停不进入恢复——真人意图明确，续播是灾难。
    RECOVERY_GRACE_S = 2.5

    # 语音模式文案（wake prompt 的 {voice_mode_note}，2026-10-29 挂机收音）
    MODE_NOTE_DIALOG = (
        "注意：用户在通过语音和你交流（可能在等你说）。建议先简短回应一句表示收到，"
        "让用户知道你在，然后再做事，做完再汇报结果。回复要口语化、简短，适合听。"
        "可以分多次表达——每次说一小段，不用一次把话说完，多次表达会自动排队依次播出。"
    )
    MODE_NOTE_AMBIENT = (
        "提示：\n"
        "1. 用户有时候说话可能还没说完。你需要判断一下当前信息是否完整了——"
        "如果完整了，那就可以回复了。如果不完整，调用 get_latest_input 获取看看"
        "有没有新的信息：如果没有新信息，那就可以回复了；如果有新信息但还不完整，"
        "那就再调一下。\n"
        "2. 如果任务明确，但需要等一会才能做完，先跟用户回应一句表示收到，"
        "然后再去做事，最终做完再汇报结果。"
    )

    def __init__(
        self,
        instance_id: str,
        *,
        config: PerceptionConfig,
        push_client: Callable[[dict[str, Any]], None],
        barge_in_enabled: bool = True,
        ambient: bool = False,
    ) -> None:
        # 会话所有权令牌：hub 逐旧接管后，旧连接的 stop 不能误杀新会话
        WebVoiceSession._next_token += 1
        self.token = WebVoiceSession._next_token
        self.instance_id = instance_id
        self.config = config
        self._push_client = push_client
        self.barge_in_enabled = barge_in_enabled
        # 挂机收音模式（zhp 2026-10-29）：回复是判断题不是义务，点名才说
        self.ambient = ambient
        self.started_at = time.time()
        self._sentences = 0
        self._dropped_echo = 0
        self._transcript_lines: list[str] = []
        self._transcript_path = (
            config and _transcript_file(instance_id)
        )
        # 打断（barge-in）状态：最近一次小张语音回复文本（partial 比对用）+
        # 已确认真人打断标记（放行下一句，绕过回声闸门）+ 低覆盖连击计数
        self._agent_recent_text = ""
        self._user_interrupted = False
        self._low_coverage_streak = 0
        # 语末聚合（2026-10-28 连续说话被劈成两条的修复）：成句先扣住，
        # hold 窗口内用户继续出字（新 partial/新句）就继续合并，真正停顿
        # 才把累积的句子合成一条消息注入——模型拿到的一定是完整一段话。
        self.sentence_hold_s = 2.0
        self._pending_sentences: list[str] = []
        self._last_speech_at = 0.0
        self._last_partial_text = ""
        self._last_loud_at = 0.0  # 能量门控：最近一次过门槛人声时刻
        # Silero VAD（2026-10-30 业界标准声学层）：人声帧级判定。
        # 可用时优先于能量门控，并接管语末计时刷新与 endpointing 快路。
        from infrastructure.perception.vad import SpeechVAD
        self._vad = SpeechVAD()
        self._last_vad_speech_at = 0.0
        self._vad_speech_started_at = 0.0  # 本轮人声起点（动态断句窗口用）
        # 假打断恢复（2026-10-30）：_recovery_at>0 = 有待检查的恢复；
        # 基线快照用于判定"停播后用户是否真的继续说话了"
        self._recovery_at = 0.0
        self._recovery_baseline_partial = ""
        self._recovery_baseline_sentences = 0
        self._recovery_baseline_activity = 0
        self._sentence_total = 0
        self._interrupted_at_seq: int | None = None  # 浏览器回报的打断段号
        # 轮次链路计时（2026-10-30 zhp：只算"输入→直接回话出声"，工具干活单列）
        self._turn_timing: dict[str, Any] | None = None
        self._work_s = 0.0            # 本轮工具干活累计（不计入链路）
        self._tool_busy_at: float | None = None
        self._timing_emit_at = 0.0    # 收表延迟：等"实际起播"回报最多 1.5s
        self._flush_thread: threading.Thread | None = None
        # 最近一次浏览器播放信号时间（段间合成间隙防标记闪断，见 _on_sentence）
        self._last_playback_signal_at = 0.0
        # 实时配置（hub 注入）
        self.ambient = False
        self._mode_note_override = ""
        self.tts_rate = "+0%"
        self._sens = dict(SENSITIVITY_PRESETS["mid"])
        self._debug_events: list[dict[str, Any]] = []
        # 已 flush 的完整 utterance 日志（listen_more 工具的等待基准）
        self._utterance_log: list[tuple[int, str]] = []
        self._utterance_seq = 0
        # PCM 环形缓冲（声纹切片用）：RTASR 的 bg/ed 毫秒时间戳 → 环切本句音频
        self._pcm_ring = bytearray()       # 最近 ~120 秒
        self._pcm_dropped = 0              # 环首之前已丢弃的总字节数
        self._pcm_ring_start_ms = 0.0      # 环首对应的会话内毫秒偏移
        self._session_start = time.time()
        # 思考流缓冲（2026-10-30 zhp：执行过程要看到"他在想什么"）：
        # agent.py 的 SSE reasoning 增量逐 token 到达，攒批下行防刷屏
        self._think_buf = ""
        self._think_last_at = 0.0

        from infrastructure.perception.rtasr_llm import RtasrLlmStream

        self._stream = RtasrLlmStream(
            app_id=config.stream_asr_app_id,
            access_key_id=config.stream_asr_api_key,
            access_key_secret=config.stream_asr_api_secret,
            on_partial=self._on_partial,
            on_sentence=self._on_sentence,
            on_error=self._on_stream_error,
        )

    # ── 生命周期 ────────────────────────────────────────────────────────────

    def start(self) -> bool:
        if not self._stream.start():
            return False
        self._last_speech_at = time.time()
        self._flush_thread = threading.Thread(
            target=self._flush_loop, name="voice-utterance-flush", daemon=True
        )
        self._flush_thread.start()
        # 后台预加载声纹模型（71MB ONNX 首次加载 ~1s；不预加载会在
        # 首句成句时阻塞 RTASR 接收线程导致超时断连）
        threading.Thread(
            target=self._prewarm_voiceprint, name="voice-vp-prewarm", daemon=True
        ).start()
        return True

    @staticmethod
    def _prewarm_voiceprint() -> None:
        try:
            from infrastructure.perception.voiceprint import _get_extractor
            _get_extractor()
        except Exception:
            pass

    def set_binary_push(self, fn: Callable[[bytes], None]) -> None:
        """注入二进制下行通道（routes 层提供，音频走 WS binary 帧）。"""
        self._push_binary = fn

    def push_tts_audio(self, wav_bytes: bytes, text: str, seq: int = -1) -> bool:
        """一段 TTS 下发：先发文字标签（前端按句标状态）再发 WAV。

        前端据此把"每次回复调用"渲染成一组：哪些句子真的播了、哪些被打断
        丢弃、哪些静音未播——中断机制可观测（2026-10-28 zhp 要求）。
        ``seq``：流水线段序号——前端收到 stop_tts 时回报正在播的段号，
        假打断恢复据此算"从哪一段续播"。
        """
        push_json = self._push_client
        fn = getattr(self, "_push_binary", None)
        if fn is None:
            return False
        try:
            push_json({"type": "tts_segment", "text": text, "seq": seq})
            fn(wav_bytes)
            # ── 链路计时锚点⑥：首块音频推达浏览器。不立即收表——留 1.5s 等
            # "实际起播"回报（锚点⑦），等不到再出（播放回报丢失的兜底）。
            t = self._turn_timing
            if t is not None and "push" not in t:
                if t.get("wake") is None:
                    t["wake"] = t.get("flush", t.get("asr"))  # prespeak 先于 agent 事件
                t["push"] = time.time()
                self._timing_emit_at = time.time() + 1.5
            return True
        except Exception:
            logger.debug("push_tts_audio failed (client gone?)", exc_info=True)
            return False

    def _push_turn_timing(self) -> None:
        """链路七段分解下发（一轮一次，收表即清）。

        锚点链：speech_end →(ASR)→ asr →(断句聚合)→ flush →(事件写入)→ event
                →(模型消费)→ wake →(首句可解析,扣干活)→ speak →(合成推送)→ push
                →(起播)→ play
        """
        t = self._turn_timing
        self._turn_timing = None
        self._timing_emit_at = 0.0
        self._tool_busy_at = None
        if not t or "push" not in t:
            return

        def seg(a: str, b: str) -> float:
            if a in t and b in t and t[b] >= t[a]:
                return round(t[b] - t[a], 2)
            return 0.0

        work = self._work_s
        self._work_s = 0.0
        gen = seg("wake", "speak")
        payload = {
            "type": "turn_timing",
            "asr": seg("speech_end", "asr"),        # 语音完成→ASR确认句
            "endpoint": seg("asr", "flush"),         # 断句聚合窗
            "event": seg("flush", "event"),          # 事件写入
            "consume": seg("event", "wake"),         # 模型消费/调度
            "gen": round(max(0.0, gen - work), 2),   # 模型输出首条可解析（扣干活）
            "synth": seg("speak", "push"),           # TTS 调用→首块推达
            "play": seg("push", "play"),             # 推达→实际起播
            "work": round(work, 2),                  # 工具干活（未计入 gen）
        }
        end = t.get("play") or t["push"]
        payload["total"] = round(max(0.0, end - t["speech_end"]), 2)
        self._push(payload)
        self.debug_log("turn_timing", **payload)
        logger.info(
            "web-voice turn timing: 说完→起播 %.2fs = ASR %.2f + 断句 %.2f + 事件 %.2f "
            "+ 消费 %.2f + 生成 %.2f + 合成 %.2f + 起播 %.2f ｜干活 %.2fs 未计",
            payload["total"], payload["asr"], payload["endpoint"], payload["event"],
            payload["consume"], payload["gen"], payload["synth"], payload["play"],
            payload["work"],
        )

    _PCM_RING_MAX = 16000 * 2 * 120  # 120 秒 16k/16bit/mono

    def feed_pcm(self, pcm: bytes) -> None:
        # 声纹切片：先写环缓再喂 RTASR
        self._pcm_ring.extend(pcm)
        excess = len(self._pcm_ring) - self._PCM_RING_MAX
        if excess > 0:
            del self._pcm_ring[:excess]
            self._pcm_dropped += excess
            self._pcm_ring_start_ms = self._pcm_dropped / 32.0  # 16000*2/1000 = 32 B/ms
        # ── PCM 门控（2026-10-30 zhp："说完了很久不转写完成"根治）──
        # VAD 优先（业界标准声学层）：silero 帧级判人声——键盘/空调/人声频
        # 杂音都进不了讯飞；人声后 SPEAK_HOLD_S 内全量放行（含句尾静音，
        # 讯飞要听到静音才能断句）。VAD 不可用时降级能量门控（物理阈值）。
        vad_speech = self._vad.feed(pcm)
        if vad_speech is not None:
            if vad_speech:
                if self._vad_speech_started_at <= 0:
                    self._vad_speech_started_at = time.time()  # 本轮人声起点
                self._last_vad_speech_at = time.time()
                # VAD 人声 = 用户还在说话的最直接信号，刷新语末计时
                # （替代"转写出新字才刷新"——更快且不受转写延迟影响）
                self._last_speech_at = time.time()
            allows = vad_speech or (time.time() - self._last_vad_speech_at < self.SPEAK_HOLD_S)
        else:
            allows = self._energy_gate_allows(pcm)
        if allows:
            self._stream.feed(pcm)

    def _energy_gate_dbfs(self, pcm: bytes) -> float:
        """帧能量（dBFS）。16k/16bit/mono。"""
        import numpy as _np

        samples = _np.frombuffer(pcm, dtype=_np.int16)
        if samples.size == 0:
            return -100.0
        rms = float(_np.sqrt(_np.mean((samples.astype(_np.float32) / 32768.0) ** 2)))
        return 20.0 * _np.log10(max(rms, 1e-6))

    def _energy_gate_allows(self, pcm: bytes) -> bool:
        gate_db = float(self._sens.get("energy_gate_db", -45.0))
        if gate_db <= -69.0:
            return True  # 门控关闭
        db = self._energy_gate_dbfs(pcm)
        now = time.time()
        if db >= gate_db:
            self._last_loud_at = now
            return True
        # 说话态余晖：刚说过话的静音帧放行（讯飞 VAD 断句需要句尾静音）
        return now - self._last_loud_at < self.SPEAK_HOLD_S

    def _slice_sentence_pcm(self, bg_ms: int, ed_ms: int) -> bytes | None:
        """按 RTASR bg/ed 时间戳从环缓切出本句的 PCM。不在范围内返回 None。"""
        start_b = int(bg_ms * 32) - self._pcm_dropped
        end_b = int(ed_ms * 32) - self._pcm_dropped
        if start_b < 0 or end_b > len(self._pcm_ring) or end_b - start_b < 16000:
            return None  # 已被环缓淘汰或太短
        return bytes(self._pcm_ring[start_b:end_b])

    def stop(self) -> dict[str, Any]:
        self._stream.finish()
        self._flush_pending(force=True)
        summary = {
            "instance_id": self.instance_id,
            "seconds": round(time.time() - self.started_at, 1),
            "sentences": self._sentences,
            "dropped_echo": self._dropped_echo,
        }
        self._flush_transcript()
        return summary

    # ── RTASR 回调（接收线程）──────────────────────────────────────────────

    def _on_partial(self, text: str) -> None:
        # 杂音空转保护（2026-10-30 zhp：停嘴很久灰字还挂着、事件迟迟不发）：
        # partial 是"当前句累积全量"——真人说话时每次 partial 都带新字；
        # 文本为空/与上次相同 = 没有新语音内容（背景杂音触发的转写抖动），
        # 不刷新语末计时，否则停嘴后杂音会把 hold 窗口无限重置。
        t = (text or "").strip()
        if t and t != self._last_partial_text:
            self._last_speech_at = time.time()
        if t:
            self._last_partial_text = t
        self._push({"type": "partial", "text": text})
        self._maybe_barge_in(text)

    def _maybe_barge_in(self, partial: str) -> None:
        """播报中检测真人打断：partial 与正在播的文案对不上 → 停 TTS。

        为什么不用裸 VAD：扬声器模式下 TTS 回声也是"语音"，VAD 开口即触发
        会把小张自己的声音当成打断，永远说不完一句。用文本相似度判别：
        partial 能在最近播报文本里对上（子串/高相似）= 回声，忽略；
        明显对不上 = 真人在说话 → 停播 + 放行下一句。延迟 ≈ 0.4-0.6s
        （partial 出字 2 字即判定；2026-10-28 从"连续两次低覆盖+4字"收紧）。
        """
        clean = (partial or "").strip()
        # 喊停快路：显式中止指令零门槛（不要求标记活跃——没在播时停也是无害 no-op）
        if _is_stop_command(clean):
            self._user_interrupted = True
            self._cancel_recovery()  # 显式喊停 = 真人意图，绝不续播
            try:
                from infrastructure.perception.voice_output import stop_playback

                stop_playback()
            except Exception:
                pass
            logger.info("web-voice STOP-COMMAND: %r → immediate stop", clean[:20])
            self._push({"type": "barge_in", "partial": clean, "stop_command": True})
            return
        if not self.barge_in_enabled or self._user_interrupted:
            return
        if not is_tts_active(self.instance_id):
            return
        # partial 门槛从 4 字降到 2 字：真人开口"等一下""不对"常只有 2-3 字，
        # 旧门槛下这类打断永远进不了判定（2026-10-28 打断失灵）。2 字仍有
        # 足够的覆盖率区分度（回声碎片也能对上）。
        if len(clean) < 2:
            return
        # 判别见 _text_coverage（滑动窗口字符重集重叠，意译回声容忍）
        coverage = _text_coverage(clean, self._agent_recent_text)
        if coverage >= float(self._sens.get("echo_cov", 0.6)):
            self._low_coverage_streak = 0
            return
        if len(clean) >= int(self._sens.get("imm_len", 6)) and coverage < float(self._sens.get("imm_cov", 0.35)):
            pass  # 长且明显不同 = 真人，立即触发
        else:
            # 短(2-5字)或模糊(0.35-0.6)的 partial：回声碎片与真人开口都长这样，
            # 等下一个 partial 确认（≈+0.3s）。这档是误杀与漏打断的平衡点。
            self._low_coverage_streak += 1
            if self._low_coverage_streak < int(self._sens.get("streak", 2)):
                return
        self._user_interrupted = True
        interrupted_something = False
        try:
            from infrastructure.perception.voice_output import stop_playback

            interrupted_something = stop_playback()
        except Exception:
            logger.debug("barge-in stop_playback failed", exc_info=True)
        # 覆盖率级判定可能误杀（回声碎片恰好对不上）：预约假打断恢复——
        # 2.5s 内没有后续人声文本就续播被打断的剩余段（见 _maybe_fire_recovery）
        if interrupted_something:
            self._schedule_recovery()
        logger.info(
            "web-voice barge-in: user speech detected during TTS "
            "(coverage=%.2f partial=%r) stopped=%s",
            coverage, clean[:30], interrupted_something,
        )
        self._push({"type": "barge_in", "partial": clean})

    # ── 假打断恢复（2026-10-30，LiveKit false_interruption 的文本层版本）──

    def _schedule_recovery(self) -> None:
        """覆盖率级 barge-in 停播后预约恢复检查。

        基线快照：当前 partial 文本 + 确认句计数 + 播放活动计数。到点时
        三者都没动 = 没有真人后续 = 误判 → 续播。动了任何一个（新字、
        白字、回复自己继续播）= 放弃恢复。
        """
        from infrastructure.perception.voice_output import play_activity

        self._recovery_at = time.time() + self.RECOVERY_GRACE_S
        self._recovery_baseline_partial = self._last_partial_text
        self._recovery_baseline_sentences = self._sentence_total
        self._recovery_baseline_activity = play_activity()

    def _cancel_recovery(self) -> None:
        self._recovery_at = 0.0

    def note_interrupted_at_seq(self, seq: int) -> None:
        """浏览器回报：stop_tts 到达时正在播的段序号（恢复续播的起点）。"""
        try:
            self._interrupted_at_seq = int(seq)
        except (TypeError, ValueError):
            pass

    def _maybe_fire_recovery(self) -> None:
        """恢复检查（_flush_loop 每 0.1s 兜）：到点且无人声后续 → 续播。"""
        if self._recovery_at <= 0 or time.time() < self._recovery_at:
            return
        self._recovery_at = 0.0
        if (
            self._sentence_total != self._recovery_baseline_sentences
            or self._last_partial_text != self._recovery_baseline_partial
        ):
            return  # 用户真的在说话（新白字/新 partial 字）
        from infrastructure.perception import voice_output

        try:
            if voice_output.play_activity() != self._recovery_baseline_activity:
                return  # 停播后已有新播放开始（回复自己继续了），不叠音
        except Exception:
            pass
        texts = voice_output.collect_recovery_texts(self._interrupted_at_seq)
        self._interrupted_at_seq = None
        if not texts:
            return
        try:
            from infrastructure.perception.voice_output import unmute_playback

            unmute_playback()
            voice = web_tts_voice(self.instance_id)
            if not voice:
                voice = voice_output.get_tts_voice(self.instance_id)
            rate = web_tts_rate()
            spoke = sum(1 for t in texts if voice_output.speak(t, voice=voice, rate=rate))
        except Exception:
            logger.debug("recovery speak failed", exc_info=True)
            return
        logger.info(
            "web-voice false-interrupt RECOVERY: resume %d/%d segments: %s",
            spoke, len(texts), "".join(texts)[:40],
        )
        self._push({"type": "tts_resume", "count": len(texts)})

    # ── listen_more 工具支撑（2026-10-29：模型"再看一眼手机"原语）──

    def current_utterance_seq(self) -> int:
        return self._utterance_seq

    def latest_input_snapshot(self) -> dict[str, Any]:
        """瞬时快照：最近语音段（带新旧与年龄）、距上次说话秒数、是否正在说。

        get_latest_input 工具的数据源（2026-10-29 zhp：去掉阻塞等待——
        "等"不存在，模型拿一眼信息自己判断，用户再说经事件系统自动叫醒）。
        """
        now = time.time()
        log = getattr(self, "_utterance_log", [])
        recent = [
            {"text": txt, "age_s": round(now - self.started_at, 1), "seq": s}
            for s, txt in log[-8:]
        ]
        # utterance 年龄用 flush 时刻近似（seq 递增即新旧序）
        since = round(now - self._last_speech_at, 1) if self._last_speech_at > 0 else None
        speaking_now = since is not None and since <= 1.2
        return {
            "recent_input": [r["text"] for r in recent],
            "recent_count": len(recent),
            "seconds_since_last_speech": since,
            "speaking_now": speaking_now,
        }

    def apply_settings(self, settings: dict[str, Any]) -> None:
        """hub 下发的实时配置（模式/提示词/灵敏度参数）。"""
        self.ambient = bool(settings.get("ambient", False))
        self._mode_note_override = str(settings.get("mode_note") or "")
        self.tts_rate = str(settings.get("tts_rate") or "+0%")
        name = str(settings.get("barge_sensitivity") or "mid")
        if name == "custom":
            s = settings
            self._sens = {
                "min_chars": float(s.get("sens_min_chars", 2)),
                "imm_len": float(s.get("sens_imm_len", 6)),
                "imm_cov": float(s.get("sens_imm_cov", 0.35)),
                "echo_cov": float(s.get("sens_echo_cov", 0.60)),
                "streak": 2,
                "hold_s": float(s.get("sens_hold_s", 2.0)),
                "energy_gate_db": float(s.get("sens_energy_gate_db", -45.0)),
            }
        else:
            self._sens = SENSITIVITY_PRESETS.get(name, SENSITIVITY_PRESETS["mid"])
        self.sentence_hold_s = float(self._sens.get("hold_s", 2.0))

    # ── 调试事件日志（前端"评估中断是否正常"的判定依据）──

    def debug_log(self, kind: str, **fields: Any) -> None:
        try:
            self._debug_events.append({"t": round(time.time() - self.started_at, 1), "kind": kind, **fields})
            if len(self._debug_events) > 60:
                self._debug_events = self._debug_events[-60:]
        except Exception:
            pass

    def recent_events(self) -> list[dict[str, Any]]:
        return list(getattr(self, "_debug_events", []))

    def note_browser_playback(self, started: bool) -> None:
        """浏览器音频播放起止回报 → 精确维护 TTS 标记（闸门/打断的基准）。"""
        self._last_playback_signal_at = time.time()
        # ── 链路计时锚点⑦：实际起播（浏览器从静到播的跳变）→ 即刻收表出分解 ──
        if started and self._turn_timing is not None and "push" in self._turn_timing \
                and "play" not in self._turn_timing:
            self._turn_timing["play"] = time.time()
            self._push_turn_timing()
        try:
            from infrastructure.perception.voice_output import note_web_playback

            note_web_playback(self.instance_id, started)
        except Exception:
            logger.debug("note_browser_playback failed", exc_info=True)

    def note_turn_anchor(self, key: str) -> None:
        """链路计时通用锚点（voice_output 的 speak/synth 经 hub 打进来）。
        一轮只记第一次；表未开（轮外活动）静默忽略。"""
        t = self._turn_timing
        if t is not None and key not in t:
            t[key] = time.time()

    @staticmethod
    def _brief(value: Any, limit: int = 140) -> str:
        try:
            import json as _json

            s = _json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
            return s[:limit] + ("…" if len(s) > limit else "")
        except Exception:
            return str(value)[:limit]

    def note_agent_step(self, name: str, phase: str, args: Any = None, result: Any = None) -> None:
        """执行过程事件下发 + 调试日志 + 轮次计时锚点。"""
        self._push({
            "type": "agent_step",
            "name": name,
            "phase": phase,
            "args": self._brief(args) if args is not None else "",
            "result": self._brief(result) if result is not None else "",
        })
        self.debug_log(f"step_{phase}", name=name)
        # ── 轮次计时（2026-10-30 zhp：输入→直接回话出声，干活不计）──
        # 工具段（start→end）累计为 work_s，从链路里扣除；express/思考不算活。
        t = self._turn_timing
        if t is not None:
            now = time.time()
            if t.get("wake") is None:
                t["wake"] = now  # 第一个 agent 活动即"已唤醒"信号
            if name in ("express_to_human", "__thinking__"):
                pass  # 直接输出/推理 = 链路的一部分，不算活
            elif phase == "start":
                if self._tool_busy_at is None:
                    self._tool_busy_at = now
            elif phase == "end" and self._tool_busy_at is not None:
                self._work_s += now - self._tool_busy_at
                self._tool_busy_at = None

    def note_thinking_delta(self, text: str) -> None:
        """LLM reasoning 流式增量 → 攒批下行（前端实时"他在想什么"）。

        SSE reasoning 逐 token/短串到达，直接逐条推会把 WS 刷成思考流瀑布；
        攒到一定字数或距上次下行超过窗口才冲一批。零思考的模型不会触发。
        """
        if not text:
            return
        if self._turn_timing is not None and self._turn_timing.get("wake") is None:
            self._turn_timing["wake"] = time.time()  # 思考首 token = 已唤醒
        self._think_buf += text
        now = time.time()
        if len(self._think_buf) >= 24 or now - self._think_last_at >= 0.6:
            chunk, self._think_buf = self._think_buf, ""
            self._think_last_at = now
            self._push({"type": "thinking_delta", "text": chunk})

    def note_thinking_end(self, text: str = "") -> None:
        """一轮思考结束：冲掉残留缓冲 + 全文下发（前端收尾折叠卡）。

        ``text`` 是本轮完整 reasoning（agent.py 从 reasoning_content /
        extract_reasoning 拿）；截 4000 字防极端长思考撑爆前端。
        """
        if self._think_buf:
            self._push({"type": "thinking_delta", "text": self._think_buf})
            self._think_buf = ""
        self._think_last_at = time.time()
        self._push({"type": "thinking_end", "text": (text or "")[:4000]})

    def note_agent_reply(self, text: str) -> None:
        """express_to_human 语音回复落账（partial 比对 + 下行文字显示）。"""
        self._agent_recent_text = (self._agent_recent_text + "\n" + text)[-800:]
        self._low_coverage_streak = 0
        self._push({"type": "agent_reply", "text": text})
        self._record_history("agent", text)

    def _record_history(self, kind: str, text: str) -> None:
        """对话落 hub 历史环形缓冲（页面刷新后对话流恢复的数据源）。"""
        try:
            get_web_voice_hub().record_history({"kind": kind, "text": text})
        except Exception:
            logger.debug("record_history failed", exc_info=True)

    def _on_sentence(self, text: str, bg_ms: int = 0, ed_ms: int = 0) -> None:
        """成句确认（= 前端白字出现）。处理链：声纹 → 白字上屏 → 停播判定 → 聚合注入。

        声纹门禁在最前（2026-10-29 zhp：办公环境同事说话不该到 agent 那里）：
        已注册声纹时，本句音频与注册声纹比对——不是你说的话 → 标灰丢弃全链路短路。
        """
        # ── 渐进式声纹识别（2026-10-30 zhp：自动注册，不需要提前录）──
        # 安全容器：声纹模型不可用/崩溃 → 直接跳过，绝不阻塞成句链路
        self._current_speaker_id = ""
        if os.getenv("DL_VOICEPRINT_OFF"):
            pass  # 声纹禁用 → 跳过
        else:
          try:
            from infrastructure.perception.speaker_registry import get_speaker_registry
            from infrastructure.perception.voiceprint import embed

            sentence_pcm = self._slice_sentence_pcm(bg_ms, ed_ms)
            if sentence_pcm is not None:
                emb = embed(sentence_pcm)
                if emb is not None:
                    registry = get_speaker_registry(self.instance_id)
                    profile, sim = registry.identify(emb)
                    self._current_speaker_id = profile.speaker_id
                    if not registry.should_inject(profile.speaker_id):
                        self._push({"type": "other_speaker", "text": text,
                                    "speaker": profile.label, "sim": round(sim, 2)})
                        self.debug_log("bg_speaker", speaker=profile.speaker_id, text=text[:30])
                        logger.info("web-voice SPEAKER gate: %s(%s) filtered: %s",
                                    profile.speaker_id, profile.role, text[:30])
                        return
                    registry.note_agent_interaction(profile.speaker_id)
                    self.debug_log("speaker_pass", speaker=profile.speaker_id,
                                   role=profile.role, sim=round(sim, 2))
          except Exception:
            logger.debug("speaker gate skipped", exc_info=True)
        # ↓ 以下为原有链路（白字上屏 → 停播 → 聚合注入）
        """成句确认（= 前端白字出现）。zhp 定规则：白字出现 = 用户在说话，
        播报必须停。

        - 停播判定不受 partial 级打断闩锁（``_user_interrupted``）限制：同一轮
          播放中被闩锁挡住的第二次确认句照样停（2026-10-28 "行行行那你先
          停吧"没停的根因之一）。
        - 活跃窗口含段间合成间隙：标记在两段音频之间会闪断（浏览器队列排空
          即报 end），用"最近 2s 内有播放信号"兜底——用户的话不能掉缝里。
        - 覆盖率只决定"注入 or 回声丢弃"，不再决定停不停。
        """
        recently_playing = is_tts_active(self.instance_id) or (
            self._last_playback_signal_at > 0
            and time.time() - self._last_playback_signal_at < 2.0
        )
        if recently_playing:
            coverage = _text_coverage(text, self._agent_recent_text)
            if coverage >= float(self._sens.get("echo_cov", 0.6)):
                # 残余回声（像小张刚说的话）：静默丢弃，不停播（它就是播报本身）
                self._dropped_echo += 1
                logger.info("web-voice echo gate dropped (coverage=%.2f): %s",
                            coverage, text[:40])
                self.debug_log("echo_drop", coverage=round(coverage, 2), text=text[:30])
                self._push({"type": "echo_dropped", "text": text})
                return
            stopped = False
            try:
                from infrastructure.perception.voice_output import (
                    mute_playback, stop_playback,
                )

                stopped = stop_playback()
                if stopped:
                    # 本轮剩余输出静音（含还在合成/流式生成的后续句），注入时解除
                    mute_playback(seconds=20.0)
            except Exception:
                logger.debug("interrupt stop failed", exc_info=True)
            logger.info("web-voice sentence-stop (coverage=%.2f stopped=%s): %s",
                        coverage, stopped, text[:40])
            self.debug_log("interrupt", coverage=round(coverage, 2), stopped=stopped, text=text[:30])
        # 白字 = 用户确凿在说话：取消一切待恢复的续播（真人压过恢复）
        self._cancel_recovery()
        # 白字上屏（显示不等聚合）；注入进聚合窗口
        self._last_speech_at = time.time()
        self._sentence_total += 1
        # ── 链路计时锚点①：ASR 确认（说过回声/他人门禁的才是真人句）──
        t = self._turn_timing
        now = time.time()
        # 新表判定：无表 / 45s 僵尸 / 上一轮已封口派出但 8s 内没等到回声
        # （agent 哑了或被打断没出声——旧表作废，本轮重新开表）
        if (
            t is None
            or ("push" not in t and now - t.get("asr", now) > 45)
            or ("flush" in t and "push" not in t and now - t["flush"] > 8.0)
        ):
            self._turn_timing = {
                "speech_end": self._last_vad_speech_at or self._last_speech_at,
                "asr": now,
            }
            self._work_s = 0.0
            self._tool_busy_at = None
        else:
            # 同一轮的后续句：刷新"最后一帧人声"。TTS 播放中 mic 会收到小张
            # 自己的声音，VAD 时间戳被回声刷高——那不是用户语声，用句子到达
            # 时刻近似（比真实语声结束晚 ~0.5s，可接受）
            if is_tts_active(self.instance_id):
                cand = now
            else:
                cand = self._last_vad_speech_at or now
            self._turn_timing["speech_end"] = max(t.get("speech_end", 0.0), cand)
        self._push({"type": "sentence", "text": text})
        self._pending_sentences.append(text)
        self.debug_log("sentence", text=text[:40])

    def _flush_loop(self) -> None:
        """聚合线程：说话停顿超过 hold 窗口 → 把累积句合成一条注入。"""
        while True:
            time.sleep(0.1)
            try:
                if self._timing_emit_at and time.time() >= self._timing_emit_at:
                    # 收表兜底：首块推达 1.5s 内没等到"实际起播"回报也出分解
                    self._push_turn_timing()
                self._maybe_fire_recovery()
                self._flush_pending(force=False)
            except Exception:
                logger.debug("utterance flush tick failed", exc_info=True)

    def _vad_endpoint_s(self) -> float:
        """动态断句静音窗（业界 min/max 双窗的 min 侧自适应）：

        刚说完的这轮话越长，允许的换气/想措辞停顿越长——短问答 1.0s 封口
        （快对话节奏），长叙述（≥12s）放宽到 2.0s（人长篇说话中途停 1.5s
        很常见，固定 1s 会把一段话劈成两条事件）。上限不超过 hold 慢窗。
        """
        if self._vad_speech_started_at <= 0 or self._last_vad_speech_at <= 0:
            return 1.0
        dur = max(0.0, self._last_vad_speech_at - self._vad_speech_started_at)
        return min(2.0, max(1.0, 0.5 + 0.125 * dur))

    def _flush_pending(self, *, force: bool) -> None:
        pending = self._pending_sentences
        if not pending:
            return
        if not force:
            # VAD endpointing 快路（业界 min/max 双窗：silero 判定"人声已停
            # 超过动态窗口"即可封口，不必等满 hold_s 慢窗——说完到成事件
            # 几乎减半。窗口内讯飞有充分时间吐出最后确认句，不会漏尾句）
            if (
                self._vad.available
                and self._last_vad_speech_at > 0
                and time.time() - self._last_vad_speech_at >= self._vad_endpoint_s()
            ):
                pass  # VAD 已确认静音超时 → 提前 flush（落到下方执行）
            elif time.time() - self._last_speech_at < self.sentence_hold_s:
                return
        self._pending_sentences = []
        self._vad_speech_started_at = 0.0  # 本轮封口，下一轮人声重新计时
        # ── 链路计时锚点②：事件写入（聚合封口→emit→DB）──
        # 表在首句 ASR 确认时已开；此处记 flush（断句判定完成），emit 后记 event。
        t_timing = self._turn_timing
        if t_timing is not None:
            t_timing["flush"] = time.time()
        merged = "".join(pending)
        self._sentences += len(pending)
        self._transcript_lines.append(merged)
        self._utterance_seq += 1
        self._utterance_log.append((self._utterance_seq, merged))
        if len(self._utterance_log) > 50:
            self._utterance_log = self._utterance_log[-50:]
        logger.info("web-voice utterance flush: %d sentences merged → %s",
                    len(pending), merged[:40])
        self.debug_log("flush", n=len(pending), text=merged[:40])
        # 打断后的注入解除静音：小张对这段话的新回复恢复出声
        self._user_interrupted = False
        self._low_coverage_streak = 0
        try:
            from infrastructure.perception.voice_output import unmute_playback

            unmute_playback()
        except Exception:
            pass
        self._emit_to_instance(merged)
        # emit 返回 = 事件已落库（锚点②完成）
        if self._turn_timing is not None and "flush" in self._turn_timing:
            self._turn_timing["event"] = time.time()

    def _on_stream_error(self, message: str) -> None:
        self._push({"type": "asr_error", "message": message})

    # ── 内部 ────────────────────────────────────────────────────────────────

    def _push(self, msg: dict[str, Any]) -> None:
        try:
            self._push_client(msg)
        except Exception:
            logger.debug("push_client failed (client gone?)", exc_info=True)

    def _current_mode_note(self) -> str:
        if self._mode_note_override.strip():
            return self._mode_note_override
        return self.MODE_NOTE_AMBIENT if self.ambient else self.MODE_NOTE_DIALOG

    def _emit_to_instance(self, text: str) -> None:
        """成句 → perception_signal 事件（复用热键语音链路 + 首轮速答标记）。"""
        try:
            from infrastructure.perception.voice_router import emit_segment_to_instance

            event_id = emit_segment_to_instance(
                self.instance_id,
                text,
                "",
                self.config,
                extra_payload={
                    "quick_first_reply": True,
                    "ambient": self.ambient,
                    "voice_mode_note": self._current_mode_note(),
                },
            )
            if event_id is not None:
                # text 随下行（2026-10-30 zhp：前端把这段 utterance 落成用户消息气泡）
                self._push({"type": "event_sent", "event_id": event_id, "text": text})
                self._record_history("user", text)
        except Exception:
            logger.exception("web-voice emit failed: %s", text[:40])

    def _flush_transcript(self) -> None:
        """会话转写落盘（对齐 voice_session 的 perception 落盘惯例）。"""
        if not self._transcript_lines or self._transcript_path is None:
            return
        try:
            self._transcript_path.parent.mkdir(parents=True, exist_ok=True)
            with self._transcript_path.open("a", encoding="utf-8") as f:
                f.write("\n".join(self._transcript_lines) + "\n")
        except Exception:
            logger.debug("flush transcript failed", exc_info=True)

    @property
    def summary_brief(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "sentences": self._sentences,
            "dropped_echo": self._dropped_echo,
        }


# 显式喊停指令（2026-10-28 zhp 场景："我说了停都没打断"——"停"1 个字
# 进不了任何判定门槛）。快路零门槛：不看字数/标记/覆盖率，命中即停。
_STOP_PHRASES = {
    "停", "停一下", "停下", "停下吧", "停了", "快停", "先停", "先停一下",
    "别说了", "先别说了", "别念了", "闭嘴", "打住", "住口", "停下来",
    "你先停", "你先停吧", "那你先停", "那你先停吧", "行了行了", "行了行了你先停吧",
}


def _is_stop_command(partial: str) -> bool:
    c = (partial or "").strip().rstrip("。，！？!?,.、哈吧呀啊 ")
    if not c:
        return False
    if c in _STOP_PHRASES:
        return True
    # ≤6 字且以"停/别说了/闭嘴"收尾的短句（"先停""你先停""帮我停"类）
    return len(c) <= 6 and (c.endswith("停") or c.endswith("别说了") or c.endswith("闭嘴"))


# ── 前端实时配置（2026-10-29 zhp：办公环境吵，敏感度/音色/模式要能随时调）──
# 档位化敏感度：low=办公吵（少误触发，容忍多等半句）/ mid=默认 / high=安静房间
# hold_s = 语末聚合窗口（成事件的停顿判定）：partial/确认句都会刷新计时，
# 停嘴超过窗口才封口成事件。2026-10-30 zhp："一句话还没说完就形成事件"——
# 旧值（0.6-1.2s）挡不住人说长句时的换气/想措辞停顿（1-2s 很常见），
# 全线加长：宁可晚 1 秒成事件，不要把半句话切出去。
SENSITIVITY_PRESETS: dict[str, dict[str, float]] = {
    "low":  dict(min_chars=5, imm_len=8, imm_cov=0.25, echo_cov=0.65, streak=2, hold_s=2.5),
    "mid":  dict(min_chars=2, imm_len=6, imm_cov=0.35, echo_cov=0.60, streak=2, hold_s=2.0),
    "high": dict(min_chars=2, imm_len=4, imm_cov=0.50, echo_cov=0.55, streak=2, hold_s=1.5),
}

# 音色候选（前端下拉框）：kokoro 神经声 / macOS 系统声
TTS_VOICE_CANDIDATES = [
    # 讯飞超拟人（2026-10-30 zhp 指定接入：副语言更自然，xfs: 前缀路由）。
    # 实测：x5_flow RTF≈0.2-1.2 可用；x6 系列 11-28s/块（免费档排队）仅试听用
    {"value": "xfs:x5_lingyuzhao_flow", "label": "聆玉昭·超拟人女声（推荐·快）"},
    {"value": "xfs:x6_lingxiaoxuan_pro", "label": "聆小璇·超拟人女声（音质佳·延迟不稳）"},
    {"value": "xfs:x6_lingfeiyi_pro", "label": "聆飞逸·超拟人男声（延迟不稳）"},
    {"value": "xfs:x6_lingxiaoyue_pro", "label": "聆小玥·超拟人女声（延迟不稳）"},
    # 讯飞 v2 在线合成（2026-10-30 接入：整句 ≈0.13s，稳定快）
    {"value": "xf:xiaoyan", "label": "小燕·经典女声（讯飞在线）"},
    {"value": "xf:xiaoyu", "label": "小宇·男声（讯飞在线）"},
    {"value": "zm_030", "label": "沉稳男声（Kokoro 本地）"},
    {"value": "zm_014", "label": "清朗男声（Kokoro 本地）"},
    {"value": "zf_001", "label": "自然女声（Kokoro 本地）"},
    {"value": "zf_017", "label": "温柔女声（Kokoro 本地）"},
    {"value": "vox:cs_lora_refmix", "label": "Yoolee 参考混声（VoxCPM2 内网 48k）"},
    {"value": "zh-CN-YunxiNeural", "label": "云希（edge 云端 · 当前 403 不可用）"},
    {"value": "Reed", "label": "系统男声（say）"},
    {"value": "Tingting", "label": "系统女声（say）"},
]

TTS_RATE_CANDIDATES = ["+0%", "+15%", "+25%", "-15%", "-30%"]

DEFAULT_SETTINGS: dict[str, Any] = {
    "ambient": True,            # 挂机收音（回复是判断题）
    "tts_voice": "",            # 空=用实例 app.yaml 的配置
    "tts_rate": "+0%",          # 语速（edge/kokoro 生效）
    "barge_sensitivity": "mid", # low/mid/high/custom
    "mode_note": "",            # 空=内置模式提示词
    # 自定义灵敏度（barge_sensitivity=custom 时生效）
    "sens_min_chars": 2,        # partial 参与判定的最小字数
    "sens_imm_len": 6,          # 立即停播的最小字数
    "sens_imm_cov": 0.35,       # 立即停播的覆盖率上限（低于它=明显不是回声）
    "sens_echo_cov": 0.60,      # 回声判定阈值（高于它=当回声丢）
    "sens_hold_s": 2.0,         # 断句聚合窗口（秒）：停嘴超过它才成事件（2026-10-30 0.8→2.0，挡换气/想措辞停顿）
    "sens_energy_gate_db": -45.0,  # 能量门控（dBFS）：低于它的帧不喂讯飞（挡杂音）；-69=关闭。余晖 SPEAK_HOLD_S=8s 内静音照放行保断句
}


# 收尾语（用户说完了的显式信号，listen_until_user_done 提前返回）
_END_PHRASES_SUFFIX = ("好了", "就这样", "说完了", "可以了", "你说吧", "该你了",
                       "轮到你了", "开始吧", "问吧", "就这些")


def _is_end_phrase(utterance: str) -> bool:
    c = (utterance or "").strip().rstrip("。，！？!?,. ")
    return len(c) >= 2 and c.endswith(_END_PHRASES_SUFFIX)


def _text_coverage(partial: str, recent: str) -> float:
    """回声得分：partial 与播报文本的"字符重集重叠"（意译容忍）。

    旧判别（最长连续匹配）对 RTASR 意译过的回声太弱："张总您好，今天北京"
    被转写成"张总您好今天北京"（丢标点）后连续匹配只剩一半 → 覆盖率 0.5
    撞阈值误杀播报。回声碎片即使语序/标点被改写，字符集合几乎不变——
    滑动窗口取 multiset 交并以 partial 长度归一，意译回声稳定 >0.8，
    真人插话（内容不同）<0.5，区分度远好于连续匹配。
    """
    from collections import Counter

    partial = (partial or "").strip()
    recent = (recent or "")[-400:]
    if not partial or not recent:
        return 0.0
    pc = Counter(partial)
    w = min(len(recent), max(8, len(partial) + 4))
    best = 0.0
    i = 0
    while i + w <= len(recent) or (i < len(recent) and w == len(recent)):
        inter = sum((pc & Counter(recent[i:i + w])).values())
        best = max(best, inter / len(partial))
        if best >= 0.9:
            break
        i += 2
    return best


def _transcript_file(instance_id: str) -> Path:
    from infrastructure.perception.config import media_dir

    stamp = time.strftime("%Y%m%d_%H%M%S")
    return media_dir(instance_id) / f"web_session_{stamp}.txt"


class WebVoiceHub:
    """网页语音会话注册表（单槽位）。master 进程持有一个实例。"""

    def __init__(self, barge_in_enabled: bool = True) -> None:
        self._session: WebVoiceSession | None = None
        self._lock = threading.Lock()
        self.barge_in_enabled = barge_in_enabled
        # 实时配置（前端面板）：启动读 voice_sense.yaml，运行期可改+落盘
        self.settings: dict[str, Any] = dict(DEFAULT_SETTINGS)
        self._load_settings()
        # 对话历史环形缓冲（2026-10-30 zhp：页面刷新后对话流要能恢复）。
        # hub 级而非 session 级：session 在页面刷新/重连时会被逐旧重建，
        # 历史必须跨会话存活。kind: user / agent / interrupt。
        self._history: list[dict[str, Any]] = []

    # ── 配置 ──

    def _settings_path(self) -> Path:
        return Path(__file__).resolve().parents[2] / "config" / "voice_sense.yaml"

    def _load_settings(self) -> None:
        try:
            import yaml

            cfg = yaml.safe_load(self._settings_path().read_text(encoding="utf-8")) or {}
            wv = cfg.get("web_voice") or {}
            for k in DEFAULT_SETTINGS:
                if k in wv:
                    self.settings[k] = wv[k]
        except Exception:
            logger.debug("load web_voice settings failed", exc_info=True)

    def _global_asr_creds(self) -> dict[str, str]:
        """voice_sense.yaml web_voice.stream_asr 的全局凭据（实例级缺失时兜底）。"""
        try:
            import yaml

            cfg = yaml.safe_load(self._settings_path().read_text(encoding="utf-8")) or {}
            sa = (cfg.get("web_voice") or {}).get("stream_asr") or {}
            return {
                "app_id": str(sa.get("app_id") or ""),
                "api_key": str(sa.get("api_key") or ""),
                "api_secret": str(sa.get("api_secret") or ""),
            }
        except Exception:
            return {"app_id": "", "api_key": "", "api_secret": ""}

    def _resolve_asr_config(self, instance_id: str) -> "PerceptionConfig":
        """实例 perception 配置 + 凭据兜底（实例 stream_asr 优先，全局补缺）。"""
        from dataclasses import replace as _dc_replace

        from infrastructure.perception.config import load_config

        cfg = load_config(instance_id)
        creds = self._global_asr_creds()
        updates: dict[str, str] = {}
        if not cfg.stream_asr_app_id and creds["app_id"]:
            updates["stream_asr_app_id"] = creds["app_id"]
        if not cfg.stream_asr_api_key and creds["api_key"]:
            updates["stream_asr_api_key"] = creds["api_key"]
        if not cfg.stream_asr_api_secret and creds["api_secret"]:
            updates["stream_asr_api_secret"] = creds["api_secret"]
        return _dc_replace(cfg, **updates) if updates else cfg

    def apply_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        """应用并持久化配置（未知键忽略；voice_sense.yaml web_voice 段回写）。"""
        with self._lock:
            for k, v in (patch or {}).items():
                if k in DEFAULT_SETTINGS and v is not None:
                    self.settings[k] = v
            try:
                import yaml

                path = self._settings_path()
                try:
                    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                except FileNotFoundError:
                    cfg = {}   # 首次落盘：文件不存在从空配置起
                if not isinstance(cfg, dict):
                    cfg = {}
                wv = cfg.get("web_voice") or {}
                wv.update({k: self.settings[k] for k in DEFAULT_SETTINGS})
                cfg["web_voice"] = wv
                path.write_text(yaml.dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
            except Exception:
                logger.warning("persist web_voice settings failed", exc_info=True)
            # 会话级参数实时生效
            session = self._session
            if session is not None:
                session.apply_settings(self.settings)
            return dict(self.settings)

    def sens(self) -> dict[str, float]:
        """当前灵敏度参数：档位 or 自定义数值。"""
        name = str(self.settings.get("barge_sensitivity") or "mid")
        if name == "custom":
            s = self.settings
            return {
                "min_chars": float(s.get("sens_min_chars", 2)),
                "imm_len": float(s.get("sens_imm_len", 6)),
                "imm_cov": float(s.get("sens_imm_cov", 0.35)),
                "echo_cov": float(s.get("sens_echo_cov", 0.60)),
                "streak": 2,
                "hold_s": float(s.get("sens_hold_s", 2.0)),
                "energy_gate_db": float(s.get("sens_energy_gate_db", -45.0)),
            }
        return SENSITIVITY_PRESETS.get(name, SENSITIVITY_PRESETS["mid"])

    def tts_voice_for(self, instance_id: str) -> str:
        """会话音色覆盖（空=用实例配置）。"""
        return str(self.settings.get("tts_voice") or "")

    # ── 对话历史（环形缓冲，跨会话存活）────────────────────────────────────

    def record_history(self, item: dict[str, Any]) -> None:
        """记一条对话项（user/agent/interrupt），带时间戳，环形裁剪。"""
        with self._lock:
            self._history.append({**item, "ts": round(time.time(), 1)})
            if len(self._history) > HISTORY_MAX_ITEMS:
                del self._history[: len(self._history) - HISTORY_MAX_ITEMS]

    def history(self) -> list[dict[str, Any]]:
        """最近对话项（旧→新）。GET /internal/voice/events 的前端消费。"""
        with self._lock:
            return list(self._history)

    # ── TTS 浏览器播放（2026-10-28）────────────────────────────────────────
    # 声音从浏览器出（AudioContext + AEC）：扬声器回声从源头消失、打断变成
    # 浏览器毫秒级停播、部署远端也能听到。voice_output 合成完 WAV 后经这里
    # 下发；本地 afplay 在会话期间静默。

    def push_tts_audio(self, instance_id: str, wav_bytes: bytes, text: str, seq: int = -1) -> bool:
        """把一段 TTS WAV 推给该实例的活跃网页会话。无会话返回 False（调用方本地播）。"""
        session = self._session
        if session is None or session.instance_id != instance_id:
            return False
        return session.push_tts_audio(wav_bytes, text, seq)

    def stop_web_tts(self, instance_id: str = "") -> bool:
        """通知浏览器停播（barge-in）。无活跃会话返回 False。"""
        session = self._session
        if session is None:
            return False
        if instance_id and session.instance_id != instance_id:
            return False
        session._push({"type": "stop_tts"})
        return True

    def note_tts_timing(self, instance_id: str, key: str, seq: int = -1) -> None:
        """voice_output 的 speak/synth 时刻 → 活跃会话的链路计时锚点。"""
        session = self._session
        if session is None or session.instance_id != instance_id:
            return
        try:
            session.note_turn_anchor(key)
        except Exception:
            pass

    def notify_tool_event(self, instance_id: str, name: str, phase: str,
                          args: Any = None, result: Any = None) -> None:
        """agent 执行过程事件（思考/工具调用起止）→ 活跃会话前端实时显示。

        phase: start / end。instance 不匹配 no-op（其他实例的执行不打扰语音会话）。
        """
        session = self._session
        if session is None or session.instance_id != instance_id:
            return
        try:
            session.note_agent_step(name, phase, args, result)
        except Exception:
            logger.debug("notify_tool_event failed", exc_info=True)

    def notify_thinking_delta(self, instance_id: str, text: str) -> None:
        """LLM reasoning 流式增量 → 活跃会话（前端实时思考流）。零侵入 no-op。"""
        session = self._session
        if session is None or session.instance_id != instance_id:
            return
        try:
            session.note_thinking_delta(text)
        except Exception:
            logger.debug("notify_thinking_delta failed", exc_info=True)

    def notify_thinking_end(self, instance_id: str, text: str = "") -> None:
        """一轮 LLM 思考结束 → 活跃会话（思考卡收尾 + 全文）。"""
        session = self._session
        if session is None or session.instance_id != instance_id:
            return
        try:
            session.note_thinking_end(text)
        except Exception:
            logger.debug("notify_thinking_end failed", exc_info=True)

    def notify_agent_voice_reply(self, instance_id: str, text: str) -> None:
        """express_to_human 语音回复 → 活跃会话（文字下发 + 打断比对基准）。

        供 action_tools._send_voice_local 回调；无活跃会话或实例不匹配时 no-op。
        """
        session = self._session
        if session is None or session.instance_id != instance_id:
            return
        try:
            session.note_agent_reply(text)
        except Exception:
            logger.debug("notify_agent_voice_reply failed", exc_info=True)

    def start_session(
        self,
        instance_id: str,
        push_client: Callable[[dict[str, Any]], None],
    ) -> tuple[bool, str, int]:
        """开一个会话；返回 ``(ok, message, token)``。

        逐旧接管（2026-10-28）：已有会话时**新连接赢**——停掉旧会话再接入。
        单人产品里"最新一次点击麦克风"就是用户当前意图；刷新页面/换标签页
        不该被 "already active" 卡死。旧连接随后断开时凭 token 对账，不会
        误杀新会话。
        """
        with self._lock:
            if self._session is not None:
                old = self._session
                logger.info(
                    "web voice session evicted by new connection: old_token=%s "
                    "(started %.0fs ago, sentences=%d)",
                    old.token, time.time() - old.started_at, old._sentences,
                )
                try:
                    old.stop()
                except Exception:
                    logger.debug("evict old session stop failed", exc_info=True)
                self._session = None
            cfg = self._resolve_asr_config(instance_id)
            if not (cfg.stream_asr_app_id and cfg.stream_asr_api_key and cfg.stream_asr_api_secret):
                return False, (
                    "stream ASR not configured: set perception.stream_asr in the "
                    "instance app.yaml or web_voice.stream_asr in config/voice_sense.yaml"
                ), 0
            session = WebVoiceSession(
                instance_id, config=cfg, push_client=push_client,
                barge_in_enabled=self.barge_in_enabled,
            )
            session.apply_settings(self.settings)
            if not session.start():
                return False, "RTASR connect failed (see master logs)", 0
            self._session = session
            logger.info("web voice session started: instance=%s token=%s",
                        instance_id[:8], session.token)
            return True, "ok", session.token

    def feed_pcm(self, pcm: bytes) -> None:
        session = self._session
        if session is not None:
            session.feed_pcm(pcm)

    def stop_session(self, token: int | None = None) -> dict[str, Any]:
        """停当前会话。带 ``token`` 时只停属于该令牌的会话（被逐旧的连接
        断开时调这里，对不上即 no-op，避免误杀接管者）。"""
        with self._lock:
            session = self._session
            if session is None:
                return {"ok": False, "error": "no active session"}
            if token is not None and session.token != token:
                return {"ok": True, "evicted": True, "note": "session already taken over"}
            self._session = None
        summary = session.stop()
        summary["ok"] = True
        logger.info("web voice session stopped: %s", summary)
        return summary

    @property
    def active_instance(self) -> str:
        session = self._session
        return session.instance_id if session else ""


def web_tts_rate() -> str:
    """会话语速覆盖（前端设置）。"""
    try:
        return str(get_web_voice_hub().settings.get("tts_rate") or "+0%")
    except Exception:
        return "+0%"


def web_tts_voice(instance_id: str) -> str:
    """会话音色覆盖（前端设置）；空 = 用实例 app.yaml 配置。发声点统一走这里。"""
    try:
        return get_web_voice_hub().tts_voice_for(instance_id)
    except Exception:
        return ""


_hub: WebVoiceHub | None = None
_hub_lock = threading.Lock()


def get_web_voice_hub() -> WebVoiceHub:
    """master 进程的全局 hub（routes 层用）。配置启动时读 voice_sense.yaml。"""
    global _hub
    with _hub_lock:
        if _hub is None:
            _hub = WebVoiceHub()
        return _hub
