"""感知系统（Perception System）—— 独立模块，为数字生命增加视觉/音频感知。

定位（参见 specs/003-perception/spec.md）：
  感知系统是一种新的事件来源。视觉/音频素材经过独立的视觉模型"消化"成
  结构化描述后，通过现有事件机制（emit_event → _wake_or_inject）注入主意识。
  对主意识而言，一条感知信号和一条飞书消息没有区别——都是它要响应的事件。

本包**只读**消费主意识数据（session/audit），绝不写主 session；所有视觉/ASR
模型调用、媒体预处理、上下文精简都在这里独立完成。对外只暴露：

  - :func:`build_slim_context` — 构建"精简视觉上下文"（只读投影）
  - :func:`run_pipeline` — 端到端编排：媒体 → 预处理 → 视觉理解 → 结构化结果

模块清单：
  config    — 配置读取（模型名、抽帧、ASR、endpoint）
  frames    — 视频抽帧 + 图片 base64 编码（纯函数）
  asr       — ASR 转写（glm-asr-2512，分段 + 上下文连贯）
  vision    — 视觉模型调用（GLM-4.6V，带精简上下文，多模态）
  context   — 视觉上下文精简器（只读 audit → 精简 messages）
  pipeline  — 端到端编排
"""
from __future__ import annotations

from infrastructure.perception.context import build_slim_context
from infrastructure.perception.pipeline import PipelineResult, run_pipeline

__all__ = ["build_slim_context", "run_pipeline", "PipelineResult"]
