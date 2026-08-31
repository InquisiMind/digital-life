"""Auxiliary LLM helper for memory consolidation and lightweight workflows."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from infrastructure.ai.config import load_runtime_config, resolve_runtime_provider

logger = logging.getLogger(__name__)


def call_llm(
    prompt: str,
    *,
    system_prompt: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    timeout: float = 120,
    **_: Any,
) -> str:
    config = load_runtime_config()
    runtime = resolve_runtime_provider(provider, config=config)
    model_name = model or runtime.get("model") or ""
    resolved_base_url = (base_url or runtime.get("base_url") or "https://api.openai.com/v1").rstrip("/")
    resolved_api_key = api_key or runtime.get("api_key") or ""
    if not model_name:
        return ""

    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    url = resolved_base_url if resolved_base_url.endswith("/chat/completions") else f"{resolved_base_url}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if resolved_api_key:
        headers["Authorization"] = f"Bearer {resolved_api_key}"
    with httpx.Client(timeout=timeout) as client:
        response = client.post(url, headers=headers, json={"model": model_name, "messages": messages})
        response.raise_for_status()
        data = response.json()
    _record_summary_token_usage(data)
    return str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")


def _record_summary_token_usage(raw_response: dict[str, Any]) -> None:
    """把摘要调用的 token 用量记进 budget_log，kind=session_summary。

    历史盲区：call_llm 走独立路径（不经 agent._chat），token 从未记录，
    导致"今日总消耗"长期漏算摘要成本。这里补回——与 _record_token_usage
    同口径，仅 kind 不同（前端图表可拆"摘要花了多少"）。失败吞掉不阻断摘要。
    """
    if not raw_response:
        return
    usage = raw_response.get("usage") or {}
    if not isinstance(usage, dict):
        return
    try:
        in_t = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        out_t = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    except (ValueError, TypeError):
        return
    if in_t <= 0 and out_t <= 0:
        return
    try:
        from infrastructure.config import get_app_instance_id
        from infrastructure.budget import get_token_tracker
        try:
            details = usage.get("prompt_tokens_details") or {}
            cached_t = int(details.get("cached_tokens") or 0) if isinstance(details, dict) else 0
        except (ValueError, TypeError):
            cached_t = 0
        get_token_tracker().record(
            instance_id=get_app_instance_id() or "",
            input_tokens=in_t,
            output_tokens=out_t,
            kind="session_summary",
            model=str(raw_response.get("model") or ""),
            cached=cached_t,
        )
    except Exception:
        logger.debug("record summary token usage failed", exc_info=True)

