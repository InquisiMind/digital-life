"""视觉模型调用（GLM-4.6V，带精简上下文 + 多模态，spec FR-004/FR-006/FR-007）。

基于 ``interfaces/tools/vision_tool.py`` 的 ``_call_vision_llm`` 扩展：
  - 支持历史 messages（精简上下文，来自 :mod:`infrastructure.perception.context`）
  - 支持多张图片（录屏抽帧序列）
  - 支持文本转写（ASR 结果）与图片同请求

不做 react，单轮调用；OpenAI 兼容协议，复用主模型 base_url + key。
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from infrastructure.perception.config import PerceptionConfig, load_config

logger = logging.getLogger(__name__)


def _build_user_content(
    image_data_uris: list[str],
    *,
    transcript: str = "",
    task_hint: str = "",
) -> list[dict[str, Any]]:
    """构建本轮 user message 的多模态 content。

    结构：[ASR 转写（最重要，放最前面）, 若干 image_url, 任务提示]。
    ASR 是人类直接意图——放在第一位让视觉模型优先理解"用户说了什么"，
    再结合画面判断用户在做什么。
    """
    parts: list[dict[str, Any]] = []

    # ASR 转写放在最前面——这是人类最直接的意图表达
    if transcript:
        parts.append({"type": "text", "text": f"【用户说的话（录音转写）】\n{transcript}"})

    if image_data_uris:
        n = len(image_data_uris)
        for uri in image_data_uris:
            parts.append({"type": "image_url", "image_url": {"url": uri}})
        parts.append({"type": "text", "text":
            f"【屏幕画面】以下 {n} 帧截图按时间顺序排列。"
            "请结合上方用户说的话，仔细观察每帧的界面元素、文字内容、变化。"
        })

    if task_hint:
        parts.append({"type": "text", "text": task_hint})

    return parts


def _default_question_prompt() -> str:
    """视觉理解的默认结构化输出指令。

    设计：让视觉模型像"一个聪明的同事路过瞄了一眼你的屏幕"——深度观察，
    不是泛泛描述画面，而是具体读出文字、识别应用、判断行为、发现关注点。
    """
    return (
        "你看了一眼用户屏幕，同时听到了用户说的话。简短描述你看到了什么。\n\n"
        "用 JSON 输出：\n"
        '{"summary": "一句话描述画面内容（结合用户说的话）"}\n'
        "只输出 JSON。简短、具体、不要读菜单栏等无关细节。"
    )


def _extract_content_text(data: dict[str, Any]) -> str:
    """从 chat/completions 响应里取 message.content（兼容 str 与 list[dict]）。"""
    try:
        choices = data.get("choices") or []
        if not choices:
            return ""
        content = choices[0].get("message", {}).get("content", "")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            # 某些视觉模型返回 [{"type":"text","text":"..."}]
            texts = [
                p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
            ]
            return "\n".join(t for t in texts if t).strip()
    except Exception:
        return ""
    return ""


def call_vision(
    *,
    image_data_uris: list[str],
    transcript: str = "",
    history_messages: list[dict[str, Any]] | None = None,
    config: PerceptionConfig | None = None,
    instance_id: str | None = None,
    question_prompt: str | None = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """调视觉模型，返回结构化结果 dict。

    Args:
        image_data_uris: 抽帧后的图片 data URI 列表（spec：抽帧 + base64）。
        transcript: ASR 转写文本（可与图片同请求，spec FR-006 降级逻辑在 pipeline 层）。
        history_messages: 精简上下文（来自 build_slim_context）。
        config: 配置；None 时按 instance_id 加载。
        instance_id: 实例（决定配置/凭据）。
        question_prompt: 自定义指令；None 用默认结构化输出模板。
        timeout: HTTP 超时秒。

    Returns:
        ``{"raw": 原始文本, "parsed": 解析后 dict 或 None, "ok": bool, "error": str}``。
        解析失败时 parsed=None，raw 仍保留供调用方兜底。
    """
    cfg = config or load_config(instance_id)
    if not cfg.api_key:
        return {
            "ok": False,
            "error": "LLM_API_KEY 未配置（感知系统复用主模型 key）",
            "raw": "",
            "parsed": None,
        }
    if not image_data_uris:
        return {
            "ok": False,
            "error": "无图片帧可分析",
            "raw": "",
            "parsed": None,
        }

    messages: list[dict[str, Any]] = []
    if history_messages:
        messages.extend(history_messages)

    user_content = _build_user_content(
        image_data_uris,
        transcript=transcript,
        task_hint=cfg.vision_task_hint,
    )
    if not user_content:
        return {"ok": False, "error": "user content 为空", "raw": "", "parsed": None}
    messages.append({"role": "user", "content": user_content})

    payload = {
        "model": cfg.vision_model,
        "messages": messages,
        "max_tokens": 1000,
        # 禁用 reasoning（GLM-4.6V 的思考会吃光 token 导致 content 为空）
        "thinking": {"type": "disabled"},
    }
    # 结构化输出指令放在 system，避免污染 user 的多模态 content
    sys_text = question_prompt or _default_question_prompt()
    payload["messages"] = [{"role": "system", "content": sys_text}] + payload["messages"]

    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
    }
    url = f"{cfg.base_url}/chat/completions"
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.post(url, json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        logger.warning("call_vision http failed (model=%s): %s", cfg.vision_model, exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "raw": "", "parsed": None}

    raw = _extract_content_text(data)
    if not raw:
        return {"ok": False, "error": "视觉模型返回空 content", "raw": "", "parsed": None}

    parsed = _try_parse_json(raw)
    return {"ok": True, "error": "", "raw": raw, "parsed": parsed}


def _try_parse_json(text: str) -> dict[str, Any] | None:
    """尽力从模型输出里解析 JSON（容忍 ```json 包裹 / 前后噪音）。"""
    if not text:
        return None
    candidates = [text]
    # 去 markdown 代码块
    if "```" in text:
        parts = text.split("```")
        for p in parts:
            stripped = p.strip()
            if stripped.startswith("json"):
                stripped = stripped[4:].strip()
            if stripped.startswith("{"):
                candidates.append(stripped)
    # 截取第一个 {...}
    lo = text.find("{")
    hi = text.rfind("}")
    if 0 <= lo < hi:
        candidates.append(text[lo : hi + 1])

    for c in candidates:
        try:
            obj = json.loads(c)
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    return None
