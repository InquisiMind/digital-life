"""OpenAI-compatible /v1/chat/completions endpoint."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any

from aiohttp import web
from infrastructure.config import get_tool_whitelist

logger = logging.getLogger("gateway.openai_api")


async def _handle_chat_completions(request: web.Request) -> web.Response:
    """OpenAI-compatible chat completions endpoint."""
    # Optional API key check
    expected_key = os.getenv("API_SERVER_KEY", "")
    if expected_key:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return web.json_response({"error": {"message": "Missing Authorization header"}}, status=401)
        provided = auth[7:].strip()
        if provided != expected_key:
            return web.json_response({"error": {"message": "Invalid API key"}}, status=401)

    try:
        body = await request.json()
    except Exception as exc:
        return web.json_response({"error": {"message": f"Invalid JSON: {exc}"}}, status=400)

    messages = body.get("messages") or []
    if not messages or not isinstance(messages, list):
        return web.json_response({"error": {"message": "messages must be a non-empty list"}}, status=400)

    model = body.get("model") or os.getenv("API_SERVER_MODEL_NAME") or "digital-life"
    stream = body.get("stream", False)

    if stream:
        return web.json_response(
            {"error": {"message": "Streaming not supported by this endpoint"}},
            status=501,
        )

    # Extract user prompt from messages
    user_messages = [m for m in messages if m.get("role") == "user"]
    if not user_messages:
        return web.json_response({"error": {"message": "No user message found"}}, status=400)
    prompt = str(user_messages[-1].get("content") or "")

    system_message = None
    system_msgs = [m for m in messages if m.get("role") == "system"]
    if system_msgs:
        system_message = "\n\n".join(str(m.get("content") or "") for m in system_msgs)

    history = []
    for msg in messages[:-1]:
        if msg.get("role") in ("user", "assistant"):
            history.append({"role": msg["role"], "content": str(msg.get("content") or "")})

    # Run AIAgent in thread pool
    def _run_agent() -> dict[str, Any]:
        try:
            from infrastructure.ai import (
                AIAgent,
                SessionDB,
                load_runtime_config,
                parse_reasoning_effort,
                resolve_runtime_provider,
            )
            from infrastructure.config import get_runtime_config_path

            cfg = load_runtime_config(get_runtime_config_path())
            runtime = resolve_runtime_provider(
                requested=os.getenv("DIGITAL_LIFE_INFERENCE_PROVIDER"),
                config=cfg,
            )
            effort = str(cfg.get("agent", {}).get("reasoning_effort", "")).strip()
            reasoning_config = parse_reasoning_effort(effort)

            session_id = f"openai_api_{uuid.uuid4().hex[:8]}"
            try:
                session_db = SessionDB()
            except Exception:
                session_db = None

            agent = AIAgent(
                model=model,
                api_key=runtime.get("api_key"),
                base_url=runtime.get("base_url"),
                provider=runtime.get("provider"),
                api_mode=runtime.get("api_mode"),
                max_iterations=20,
                reasoning_config=reasoning_config,
                quiet_mode=True,
                platform="openai_api",
                session_id=session_id,
                session_db=session_db,
                skip_memory=True,
                tool_whitelist=get_tool_whitelist(),
            )
            result = agent.run_conversation(
                prompt,
                system_message=system_message,
                conversation_history=history or None,
            )
            return result
        except Exception as exc:
            logger.exception("OpenAI API agent run failed: %s", exc)
            return {"final_response": f"Error: {exc}", "status": "error", "tool_calls": []}

    result = await asyncio.to_thread(_run_agent)
    final_response = result.get("final_response", "") or ""

    response = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": final_response},
                "finish_reason": "stop" if result.get("status") == "completed" else "length",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
    return web.json_response(response)


async def _handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def _handle_models(request: web.Request) -> web.Response:
    """OpenAI-compatible /v1/models stub."""
    model = os.getenv("API_SERVER_MODEL_NAME") or "digital-life"
    return web.json_response(
        {
            "object": "list",
            "data": [
                {
                    "id": model,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "digital-life",
                }
            ],
        }
    )


def add_openai_routes(app: web.Application) -> None:
    """Register OpenAI-compatible routes on the aiohttp app."""
    app.router.add_post("/v1/chat/completions", _handle_chat_completions)
    app.router.add_get("/v1/models", _handle_models)
    app.router.add_get("/health", _handle_health)
