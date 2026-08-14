"""语音事件快答策略（think 按需关闭一轮）。

语义（2026-08-14 定稿）：
  - 默认：think 模式 —— 维持实例配置的 reasoning effort，完全不变
  - 语音事件 wake（event_platform == "voice"）：**第一次**模型调用关 think
    （effort→minimal）——用户在等第一声回应，快答优先
  - "后面 think 再打开"：同一 wake 的后续调用（做事、汇报）自动恢复原 effort

计数器在 agent 实例上（每 wake 新建 agent → 天然 wake 级边界），
非语音场景（文字消息/timer/routine/initiative）零干预。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 快答档位（GLM reasoning_effort 五档的最低档）
FAST_EFFORT = "minimal"


def is_fast_first_call(
    *,
    event_platform: str,
    call_idx: int,
    enabled: bool = True,
) -> bool:
    """本 wake 第 ``call_idx`` 次（0-based）模型调用是否快答（关 think）。

    纯函数：语音场景且是第一次调用 → True。其余 False（维持原 effort）。
    """
    if not enabled or event_platform != "voice":
        return False
    return call_idx == 0
