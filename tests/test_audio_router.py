"""AudioRouter 状态机测试。

这是整个语音感知系统的核心逻辑——状态转换决定每段语音的命运。
纯逻辑测试（不依赖音频/ASR/KWS），mock 所有回调。
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import numpy as np
import pytest

from infrastructure.perception.audio_sense.router import (
    AudioRouter, RouterConfig, VoiceState,
)


@pytest.fixture
def mock_callbacks():
    """构造 mock 回调。"""
    cb = MagicMock()
    cb.transcribe.return_value = "测试文本"
    cb.emit_wake = MagicMock()
    cb.emit_dialog = MagicMock()
    cb.persist = MagicMock(return_value="/tmp/test.wav")
    cb.match_instance.return_value = "test-iid"
    cb.on_state_change = MagicMock()
    return cb


@pytest.fixture
def router(mock_callbacks):
    """构造一个默认配置的 router。"""
    config = RouterConfig(
        dialog_timeout_s=0.2,   # 短超时便于测试
        focus_timeout_s=0.3,
        default_instance="default-iid",
    )
    return AudioRouter(config, mock_callbacks)


def _silence(duration_s: float = 1.0) -> np.ndarray:
    return np.zeros(int(16000 * duration_s), dtype=np.int16)


# ── 初始状态 ─────────────────────────────────────────────────────────────


def test_initial_state_is_dormant(router):
    assert router.state == VoiceState.DORMANT


# ── dormant：KWS 命中 → dialog ──────────────────────────────────────────


def test_keyword_hit_transitions_to_dialog(router, mock_callbacks):
    """KWS 命中 → 精转 → emit_wake → 切到 dialog。"""
    audio = _silence(1.0)
    router.on_keyword_hit("塞罗", audio)

    assert router.state == VoiceState.DIALOG
    mock_callbacks.transcribe.assert_called_once_with(audio)
    mock_callbacks.emit_wake.assert_called_once()
    mock_callbacks.on_state_change.assert_called_once_with(VoiceState.DORMANT, VoiceState.DIALOG)


def test_keyword_hit_ignored_in_dialog(router, mock_callbacks):
    """dialog 状态下重复 KWS 命中 → 忽略。"""
    router.on_keyword_hit("塞罗", _silence())  # → dialog
    mock_callbacks.reset_mock()

    router.on_keyword_hit("塞罗", _silence())  # 重复命中

    assert router.state == VoiceState.DIALOG
    mock_callbacks.transcribe.assert_not_called()  # 不再重复转写
    mock_callbacks.emit_wake.assert_not_called()


def test_keyword_hit_uses_default_instance_when_no_match(router, mock_callbacks):
    """match_instance 返回 None → 用 default_instance。"""
    mock_callbacks.match_instance.return_value = None
    router.on_keyword_hit("塞罗", _silence())

    mock_callbacks.emit_wake.assert_called_once()
    args = mock_callbacks.emit_wake.call_args[0]
    assert args[1] == "default-iid"  # 用了 default


def test_keyword_hit_without_audio(router, mock_callbacks):
    """KWS 命中但没提供音频 → 不调 transcribe，仍 emit + 切 dialog。"""
    router.on_keyword_hit("塞罗", None)

    assert router.state == VoiceState.DIALOG
    mock_callbacks.transcribe.assert_not_called()
    mock_callbacks.emit_wake.assert_called_once()


# ── dormant：VAD 段被忽略 ───────────────────────────────────────────────


def test_segment_ignored_in_dormant(router, mock_callbacks):
    """dormant 状态下 VAD 段 → 不转写、不 emit。"""
    router.on_segment(_silence(2.0))

    mock_callbacks.transcribe.assert_not_called()
    mock_callbacks.emit_dialog.assert_not_called()


# ── dialog：VAD 段 → ASR + emit ─────────────────────────────────────────


def test_segment_processed_in_dialog(router, mock_callbacks):
    """dialog 状态下 VAD 段 → 精转 + emit_dialog。"""
    router.on_keyword_hit("塞罗", _silence())  # → dialog (match_instance returns test-iid)
    mock_callbacks.reset_mock()
    mock_callbacks.transcribe.return_value = "帮我查个东西"

    audio = _silence(2.0)
    router.on_segment(audio)

    mock_callbacks.transcribe.assert_called_once_with(audio)
    mock_callbacks.emit_dialog.assert_called_once_with("帮我查个东西", "test-iid")
    mock_callbacks.persist.assert_not_called()  # dialog 不落盘


def test_empty_transcript_skipped_in_dialog(router, mock_callbacks):
    """dialog 状态下 ASR 返回空 → 跳过 emit。"""
    router.on_keyword_hit("塞罗", _silence())  # → dialog
    mock_callbacks.reset_mock()
    mock_callbacks.transcribe.return_value = ""

    router.on_segment(_silence())
    mock_callbacks.emit_dialog.assert_not_called()


# ── 超时转换 ─────────────────────────────────────────────────────────────


def test_dialog_timeout_to_dormant(router, mock_callbacks):
    """dialog 状态超时 → dormant。"""
    router.on_keyword_hit("塞罗", _silence())  # → dialog
    assert router.state == VoiceState.DIALOG

    time.sleep(0.3)  # 超过 dialog_timeout_s=0.2
    router.check_timeout()

    assert router.state == VoiceState.DORMANT
    mock_callbacks.on_state_change.assert_called_with(VoiceState.DIALOG, VoiceState.DORMANT)


def test_dialog_no_timeout_if_recent_speech(router, mock_callbacks):
    """dialog 状态有语音活动 → 不超时。"""
    router.on_keyword_hit("塞罗", _silence())  # → dialog

    time.sleep(0.15)  # 没超过 timeout
    router.on_segment(_silence())  # 刷新 last_speech_time
    time.sleep(0.1)
    router.check_timeout()

    assert router.state == VoiceState.DIALOG


def test_dormant_no_timeout(router, mock_callbacks):
    """dormant 状态 check_timeout 不做任何事。"""
    time.sleep(0.3)
    router.check_timeout()
    assert router.state == VoiceState.DORMANT


def test_focus_timeout_to_dialog(router, mock_callbacks):
    """focus 状态超时 → dialog（不回 dormant）。"""
    router.on_keyword_hit("塞罗", _silence())  # → dialog
    router.enter_focus()
    assert router.state == VoiceState.FOCUS

    time.sleep(0.4)  # 超过 focus_timeout_s=0.3
    router.check_timeout()

    assert router.state == VoiceState.DIALOG


# ── 外部状态控制 ─────────────────────────────────────────────────────────


def test_enter_focus_from_dormant(router, mock_callbacks):
    """从 dormant 直接进 focus（不需要先 dialog）。"""
    router.enter_focus()
    assert router.state == VoiceState.FOCUS


def test_enter_focus_from_dialog(router, mock_callbacks):
    """从 dialog 进 focus。"""
    router.on_keyword_hit("塞罗", _silence())
    router.enter_focus()
    assert router.state == VoiceState.FOCUS


def test_exit_focus_to_dialog(router, mock_callbacks):
    """退出 focus → 回 dialog。"""
    router.enter_focus()
    router.exit_focus()
    assert router.state == VoiceState.DIALOG


def test_force_dormant(router, mock_callbacks):
    """强制回 dormant。"""
    router.on_keyword_hit("塞罗", _silence())
    assert router.state == VoiceState.DIALOG

    router.force_dormant()
    assert router.state == VoiceState.DORMANT


# ── focus 模式落盘 ──────────────────────────────────────────────────────


def test_focus_mode_persists(router, mock_callbacks):
    """focus 状态下 VAD 段 → emit + 落盘。"""
    router.enter_focus()
    mock_callbacks.transcribe.return_value = "重要内容"

    router.on_segment(_silence(1.0))
    router.on_segment(_silence(2.0))

    assert mock_callbacks.persist.call_count == 2
    mock_callbacks.emit_dialog.assert_called()


def test_focus_uses_dialog_instance(router, mock_callbacks):
    """focus 模式复用 dialog 的目标实例（match_instance 返回 test-iid）。"""
    router.on_keyword_hit("塞罗", _silence())  # → dialog, instance=test-iid
    router.enter_focus()
    mock_callbacks.reset_mock()
    mock_callbacks.transcribe.return_value = "测试"

    router.on_segment(_silence())

    args = mock_callbacks.emit_dialog.call_args[0]
    assert args[1] == "test-iid"


# ── control 方法（HTTP 接口的底层）───────────────────────────────────────


def test_control_focus(router):
    router.enter_focus()
    # enter_focus 返回 None（void），但状态变了
    assert router.state == VoiceState.FOCUS
