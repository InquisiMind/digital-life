"""L5 语义传递 + 状态机 —— 核心路由模块。

管理三种交互状态的转换，决定每段语音的命运：

  休眠（dormant）：只有 KWS 在听。VAD 段被忽略（不 ASR、不落盘、不传云）。
    KWS 命中 → 精转该段 → emit perception_signal → 切到"对话"

  对话（dialog）：VAD 段每段精转 → emit group_message（群聊链路）。
    实例持续接收，LLM 自主判断是否回应（和群聊一模一样）。
    超时静默（30s）→ 切回"休眠"

  专注（focus）：VAD 段每段精转 + 落盘 wav → emit group_message + 文件。
    超时静默（60s）→ 切回"对话"（不回休眠）

为什么"对话"状态用 group_message 而非 perception_signal：
  perception_signal 是"人类在呼唤你"（高优先级、强制唤醒）。
  对话中的每段话不是呼唤——是连续交流，和群聊消息同构。
  group_message 走已有的 emit → _wake_or_inject → chat_stream 链路，
  实例用 LLM 自主判断"这句话和我有关吗"，正是用户要的"探讨"体验。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

import numpy as np

logger = logging.getLogger(__name__)

# 默认超时（秒）
DIALOG_TIMEOUT_S = 30.0   # 对话状态 30s 无语音 → 回休眠
FOCUS_TIMEOUT_S = 60.0    # 专注状态 60s 无语音 → 回对话


class VoiceState(str, Enum):
    """语音感知状态。"""

    DORMANT = "dormant"   # 休眠：只听唤醒词
    DIALOG = "dialog"     # 对话：每段传给实例（群聊模式）
    FOCUS = "focus"       # 专注：每段传给实例 + 落盘


@dataclass
class RouterConfig:
    """路由配置（从 voice_sense.yaml 加载）。"""

    dialog_timeout_s: float = DIALOG_TIMEOUT_S
    focus_timeout_s: float = FOCUS_TIMEOUT_S
    default_instance: str = ""  # 对话中未命中关键词时的默认实例


@dataclass
class RouterCallbacks:
    """Router 的外部回调（由 service 注入）。"""

    # 精确转写一段音频（云端 ASR）→ 返回文本
    transcribe: Callable[[np.ndarray], str]
    # emit 唤醒事件（休眠→对话）：transcript + target_instance → event_id
    emit_wake: Callable[[str, str], None]
    # emit 对话事件（对话/专注中）：transcript + target_instance → None
    emit_dialog: Callable[[str, str], None]
    # 落盘（专注模式）：audio + segment_idx → wav_path
    persist: Callable[[np.ndarray, int], str | None] = field(default=lambda a, i: None)
    # 关键词匹配：transcript → instance_id | None（ASR 转写后匹配）
    match_instance: Callable[[str], str | None] = field(default=lambda t: None)
    # KWS 关键词直接查实例：keyword → instance_id | None（不需要 ASR）
    lookup_instance: Callable[[str], str | None] = field(default=lambda k: None)
    # 状态变化通知：old_state, new_state → None
    on_state_change: Callable[[VoiceState, VoiceState], None] = field(default=lambda o, n: None)


class AudioRouter:
    """状态机 + 路由。

    两个入口：
      on_keyword_hit(keyword) —— KWS 检测到唤醒词（dormant 状态下有意义）
      on_segment(audio)       —— VAD 切出一个语音段

    一个定时器：
      check_timeout() —— 检查超时转换（由 service 的 tick 定期调）
    """

    def __init__(self, config: RouterConfig, callbacks: RouterCallbacks) -> None:
        self._config = config
        self._cb = callbacks
        self._state = VoiceState.DORMANT
        self._last_speech_time = time.monotonic()
        self._seg_counter = 0
        # 当前对话的目标实例（唤醒时确定，对话中复用）
        self._dialog_instance = ""

    @property
    def state(self) -> VoiceState:
        return self._state

    def _set_state(self, new_state: VoiceState) -> None:
        if new_state == self._state:
            return
        old = self._state
        self._state = new_state
        logger.info("voice state: %s → %s", old.value, new_state.value)
        try:
            self._cb.on_state_change(old, new_state)
        except Exception:
            logger.exception("on_state_change callback failed")

    # ── KWS 命中（dormant → dialog）──────────────────────────────────────
    def on_keyword_hit(self, keyword: str, audio: np.ndarray | None = None) -> None:
        """KWS 检测到唤醒词。

        路由逻辑（优先级）：
          1. lookup_instance(keyword) — KWS 关键词直接查实例（最快，不需要 ASR）
          2. ASR 精转 + match_instance — 转写后用 wake_words 子串匹配
          3. default_instance — fallback

        确定目标后 emit 唤醒事件，切到 dialog。
        """
        if self._state != VoiceState.DORMANT:
            logger.debug("KWS hit ignored in %s state: %s", self._state, keyword)
            return

        logger.info("wake word detected: %s", keyword)

        # 1. KWS 关键词直接查实例（最快路径）
        target = ""
        try:
            target = self._cb.lookup_instance(keyword) or ""
        except Exception:
            pass

        # 2. ASR 精转 + match_instance（如果直接查没命中）
        transcript = ""
        if not target:
            if audio is not None and len(audio) > 0:
                try:
                    transcript = self._cb.transcribe(audio) or ""
                except Exception:
                    logger.exception("transcribe wake segment failed")
            if transcript:
                try:
                    target = self._cb.match_instance(transcript) or ""
                except Exception:
                    pass

        # 3. fallback
        if not target:
            target = self._config.default_instance

        # emit 唤醒事件
        if target:
            try:
                self._cb.emit_wake(transcript or f"（唤醒词：{keyword}）", target)
            except Exception:
                logger.exception("emit_wake failed")
            self._dialog_instance = target
        else:
            logger.warning("wake word hit but no target instance, keyword=%s", keyword)

        # 切到对话状态
        self._set_state(VoiceState.DIALOG)
        self._last_speech_time = time.monotonic()

    # ── VAD 段（dialog/focus 状态下每段都处理）────────────────────────────
    def on_segment(self, audio: np.ndarray) -> None:
        """VAD 切出一个语音段。

        dormant 状态：忽略（KWS 管唤醒，VAD 段不处理）。
        dialog/focus 状态：精转 → emit group_message（+ focus 时落盘）。
        """
        self._last_speech_time = time.monotonic()

        if self._state == VoiceState.DORMANT:
            return  # 休眠期忽略 VAD 段

        # 精转
        transcript = ""
        try:
            transcript = self._cb.transcribe(audio) or ""
        except Exception:
            logger.exception("transcribe segment failed")
            return

        if not transcript.strip():
            logger.debug("empty transcript in %s, skip", self._state)
            return

        # 目标实例：对话中复用唤醒时的实例
        target = self._dialog_instance or self._config.default_instance

        # emit 对话事件
        if target:
            try:
                self._cb.emit_dialog(transcript, target)
            except Exception:
                logger.exception("emit_dialog failed")

        # 专注模式：落盘
        if self._state == VoiceState.FOCUS:
            self._seg_counter += 1
            try:
                self._cb.persist(audio, self._seg_counter)
            except Exception:
                logger.exception("persist segment failed")

    # ── 超时检查 ─────────────────────────────────────────────────────────
    def check_timeout(self) -> None:
        """检查超时转换。由 service 的 tick 定期调。

        dialog 超时 → dormant
        focus 超时 → dialog
        """
        if self._state == VoiceState.DORMANT:
            return

        elapsed = time.monotonic() - self._last_speech_time
        if self._state == VoiceState.DIALOG and elapsed > self._config.dialog_timeout_s:
            logger.info("dialog timeout (%.0fs silent) → dormant", elapsed)
            self._set_state(VoiceState.DORMANT)
            self._dialog_instance = ""
            self._seg_counter = 0
        elif self._state == VoiceState.FOCUS and elapsed > self._config.focus_timeout_s:
            logger.info("focus timeout (%.0fs silent) → dialog", elapsed)
            self._set_state(VoiceState.DIALOG)

    # ── 外部状态控制（HTTP / 按键）─────────────────────────────────────────
    def enter_focus(self) -> None:
        """进入专注模式（外部触发：用户按键 / zero 自己调工具）。"""
        if self._state == VoiceState.FOCUS:
            return
        # 从 dormant 直接进 focus 也行（不一定要先 dialog）
        self._set_state(VoiceState.FOCUS)
        self._last_speech_time = time.monotonic()
        if not self._dialog_instance:
            self._dialog_instance = self._config.default_instance

    def exit_focus(self) -> None:
        """退出专注模式 → 回到对话。"""
        if self._state != VoiceState.FOCUS:
            return
        self._set_state(VoiceState.DIALOG)
        self._last_speech_time = time.monotonic()

    def force_dormant(self) -> None:
        """强制回休眠（外部触发）。"""
        self._set_state(VoiceState.DORMANT)
        self._dialog_instance = ""
        self._seg_counter = 0
