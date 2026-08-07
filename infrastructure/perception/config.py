"""感知系统配置读取。

复用主意识的凭据体系（同平台、同 key），读取顺序：
  1. 实例 ``apps/<id>/config/app.yaml`` 的 ``perception`` 段（最高优先级）
  2. 实例 ``app.yaml`` 的 ``model`` 段（vision/base_url/api_key，与 vision_tool 同源）
  3. 环境变量兜底

这样做的好处：感知系统不引入新的凭据来源，开箱即用主意识已有的 GLM key。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── 默认值（与 vision_tool.py 对齐）─────────────────────────────────────────
DEFAULT_VISION_MODEL = "glm-4.6v"
DEFAULT_ASR_MODEL = "glm-asr-2512"
DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
# 录屏抽帧：画面变化慢，2-3fps 足够还原操作流（spec FR-004）
DEFAULT_FRAME_FPS = 2.0
# 空间降采样到 720p 宽（spec FR-004）
DEFAULT_FRAME_MAX_WIDTH = 1280
# 单次视觉调用最多携带的图片帧数（控体积 + token）
DEFAULT_MAX_FRAMES = 12
# ASR 单次硬限 30s（glm-asr-2512 官方约束）
ASR_SEGMENT_SECONDS = 30
# 最大录制时长（秒）—— 超时自动结束（spec US1-AC5 / FR-002）
DEFAULT_MAX_CAPTURE_SECONDS = 120
# 默认全局快捷键（可被实例 app.yaml 的 perception.hotkey 覆盖）
DEFAULT_HOTKEY = "cmd+shift+p"


@dataclass(frozen=True)
class PerceptionConfig:
    """感知系统运行配置（只读快照）。"""

    # 开关 + 快捷键（每实例独立，由 config_center / app.yaml 管理）
    enabled: bool = False
    hotkey: str = DEFAULT_HOTKEY
    vision_model: str = DEFAULT_VISION_MODEL
    asr_model: str = DEFAULT_ASR_MODEL
    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""
    frame_fps: float = DEFAULT_FRAME_FPS
    frame_max_width: int = DEFAULT_FRAME_MAX_WIDTH
    max_frames: int = DEFAULT_MAX_FRAMES
    max_capture_seconds: int = DEFAULT_MAX_CAPTURE_SECONDS
    # 视觉上下文：取最近几轮主对话（spec FR-007）
    context_recent_turns: int = 5
    # ASR 领域热词（提升专有名词识别率）
    asr_hotwords: tuple[str, ...] = field(default_factory=tuple)
    # 额外传递给视觉模型的 task 提示（可选）
    vision_task_hint: str = ""


def _project_root() -> Path:
    """返回仓库根目录（infrastructure/perception/config.py 往上三级）。"""
    return Path(__file__).resolve().parents[2]


def _read_app_yaml(iid: str) -> dict[str, Any]:
    """读实例 app.yaml；不存在/解析失败返回空 dict。"""
    if not iid:
        return {}
    cfg_path = _project_root() / "apps" / iid / "config" / "app.yaml"
    if not cfg_path.exists():
        return {}
    try:
        import yaml

        return yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.debug("read app.yaml failed for %s: %s", iid, exc)
        return {}


def _read_api_key(iid: str) -> str:
    """从实例 secrets.env 或环境变量读 API key（与 vision_tool._get_llm_api_key 同源）。"""
    if iid:
        secrets_path = _project_root() / "apps" / iid / "config" / "secrets.env"
        if secrets_path.exists():
            try:
                for line in secrets_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("LLM_API_KEY=") or line.startswith("GLM_API_KEY="):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
            except Exception:
                pass
    return os.getenv("LLM_API_KEY", "") or os.getenv("GLM_API_KEY", "")


def load_config(instance_id: str | None = None) -> PerceptionConfig:
    """加载感知配置。

    优先级：实例 ``app.yaml`` 的 ``perception`` 段 > ``model`` 段 > 环境变量 > 默认。
    传入 ``instance_id`` 为空时，尝试从 ContextVar/env 解析当前实例。
    """
    iid = instance_id or ""
    if not iid:
        try:
            from infrastructure.config import get_app_instance_id

            iid = get_app_instance_id() or ""
        except Exception:
            iid = ""

    app_cfg = _read_app_yaml(iid)
    model_cfg = app_cfg.get("model") or {}
    perc_cfg = app_cfg.get("perception") or {}

    base_url = (
        (perc_cfg.get("base_url") or "").strip()
        or (model_cfg.get("base_url") or "").strip()
        or DEFAULT_BASE_URL
    ).rstrip("/")

    api_key = _read_api_key(iid)

    vision_model = (perc_cfg.get("vision_model") or model_cfg.get("vision") or "").strip()
    asr_model = (perc_cfg.get("asr_model") or "").strip()

    hotwords = perc_cfg.get("asr_hotwords") or []
    if not isinstance(hotwords, (list, tuple)):
        hotwords = []

    return PerceptionConfig(
        enabled=bool(perc_cfg.get("enabled", False)),
        hotkey=(perc_cfg.get("hotkey") or DEFAULT_HOTKEY).strip() or DEFAULT_HOTKEY,
        vision_model=vision_model or DEFAULT_VISION_MODEL,
        asr_model=asr_model or DEFAULT_ASR_MODEL,
        base_url=base_url,
        api_key=api_key,
        frame_fps=float(perc_cfg.get("frame_fps", DEFAULT_FRAME_FPS)),
        frame_max_width=int(perc_cfg.get("frame_max_width", DEFAULT_FRAME_MAX_WIDTH)),
        max_frames=int(perc_cfg.get("max_frames", DEFAULT_MAX_FRAMES)),
        max_capture_seconds=int(perc_cfg.get("max_capture_seconds", DEFAULT_MAX_CAPTURE_SECONDS)),
        context_recent_turns=int(perc_cfg.get("context_recent_turns", 5)),
        asr_hotwords=tuple(str(h) for h in hotwords),
        vision_task_hint=str(perc_cfg.get("vision_task_hint") or ""),
    )


def media_dir(instance_id: str | None = None) -> Path:
    """返回实例的感知媒体落盘目录 ``apps/<id>/data/perception/``。

    原始录屏/录音文件保留在这里，供主意识事后回看（spec FR-003 / FR-013 media_path）。
    """
    iid = instance_id or ""
    if not iid:
        try:
            from infrastructure.config import get_app_instance_id

            iid = get_app_instance_id() or "zero"
        except Exception:
            iid = "zero"
    d = _project_root() / "apps" / iid / "data" / "perception"
    d.mkdir(parents=True, exist_ok=True)
    return d
