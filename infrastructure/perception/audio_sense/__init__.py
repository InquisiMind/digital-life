"""持续语音感知系统（Audio Sense）—— 分层过滤的语音交互。

架构（5 层信号过滤）::

    持续声波（16kHz）
        │
        ├─ L1 能量门控 ──── 振幅 < 阈值 → 丢弃
        ├─ L2 VAD 语音检测 ── Silero ONNX，非人声 → 丢弃
        ├─ L3 端点检测 ────── 静默≥0.5s → 切出"语音段"
        ├─ L4 唤醒词门控 ──── sherpa-onnx KWS 检测 "zero"/"alpha"
        └─ L5 语义传递 ────── 云端 ASR → emit 事件 → 实例

模块清单：
  capture ─ 录音管道（/usr/bin/python3 子进程，TCC 麦克风权限）
  vad     ─ VAD 分段（复用 voice_session.VADSegmenter）
  kws     ─ 唤醒词检测（sherpa-onnx KeywordSpotter）
  router  ─ 状态机 + 路由（休眠↔对话↔专注）
  service ─ 编排器（master 级守护，组合所有管道）
"""
from __future__ import annotations
