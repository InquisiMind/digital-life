#!/usr/bin/env python3
"""重复发送 - 模型行为重现实验

目的：复现 7/14 10:35 alpha 重复发送"哈哈明白了"第 2 次的场景。

方法：
  1. 取 alpha messages[:182] 真实快照（即模型生成 messages[182] 重复发送那次 input）
  2. 直接调 GLM-5.2 5 次，每次独立采样
  3. 统计：
     - 模型多少次生成 express_to_human（即"决定要回")
     - 多少次是"沉默" Decide（调 rest / record_thought / 不调 express）
     - 如果决定回，内容是否和 messages[168] (text_length=541) 重复
  4. 输出每次响应摘要 + 统计

结论判据：
  - 5 次中 >=3 次重复 → 模型在此上下文下稳定反复回（系统性 bug）
  - 1-2 次 → 模型偶发误判
  - 0 次 → 那次 1179 可能是被我之前注意的别的 wake 影响产生的（不算 bug）
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
SECRETS = ROOT / "apps/5052c33a-e700-44dd-aea3-00e04a661ab1/config/secrets.env"
SESSION_LOG = ROOT / "apps/5052c33a-e700-44dd-aea3-00e04a661ab1/data/sessions/tx_task_todo_due_0714_1013_b9430a.json"
# 关键修正：用 call_8 dump 而非 session log，因为 session log 丢了 reasoning_content——
# 缺它模型就缺历史推理上下文，无法复现当时的 decision。
INPUT_SRC = ROOT / "apps/5052c33a-e700-44dd-aea3-00e04a661ab1/data/sessions_dumps/tx_task_todo_due_0714_1013_b9430a__call_8.json"
# session log 取 #1959 wake_signal 接在 dump 末尾后做最终 input
SESSION_LOG_FILE = ROOT / "apps/5052c33a-e700-44dd-aea3-00e04a661ab1/data/sessions/tx_task_todo_due_0714_1013_b9430a.json"
# wake_signal 在 session_log 的 idx（即 messages[181]）
WAKE_SIGNAL_IDX = 181

MODEL = "glm-4-plus"  # 先用 glm-4-plus，实际配置 glm-5.2（API 端 mirror 可能不一样）
URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"

# 实际配置的 model name
ACTUAL_MODEL = "GLM-5.2"

N_RUNS = 5


def load_api_key() -> str:
    for line in SECRETS.read_text(encoding="utf-8").splitlines():
        if line.startswith(("LLM_API_KEY=", "GLM_API_KEY=")):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("API key not found")


def load_input_messages() -> list[dict]:
    """加载真正的 LLM input：
       1) prefix: call_8 dump messages[:198]（含 reasoning_content）— 这是 rest sentinel result 的完整 input
       2) append: session log messages[181]（即 #1959 wake_signal）
    这是真实 rest 被撤销后 _chat 内部 `_inject_signalled_events` 注入 #1959 的最终 LLM 看到的 input。
    """
    prefix = json.loads(INPUT_SRC.read_text(encoding="utf-8"))["messages"][:198]
    session = json.loads(SESSION_LOG_FILE.read_text(encoding="utf-8"))["messages"]
    wake_signal = session[WAKE_SIGNAL_IDX]
    return prefix + [wake_signal]


def call_glm(api_key: str, messages: list[dict]) -> dict:
    """调一次 GLM API 返回 assistant message + tool_calls."""
    payload = {
        "model": ACTUAL_MODEL,
        "messages": messages,
        "temperature": 1.0,  # 真实跑的生产温度（agent 默认 1.0）
        "tools": [{
            "type": "function",
            "function": {
                "name": "express_to_human",
                "description": "回复人类消息（群里 @ 别人或私聊）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "chat_id": {"type": "string"},
                        "kind": {"type": "string"},
                        "text": {"type": "string"},
                    },
                },
            },
        }, {
            "type": "function",
            "function": {
                "name": "rest",
                "description": "进入休息（设闹钟+结束 session）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reuse": {"type": "integer"},
                        "confirm": {"type": "boolean"},
                    },
                },
            },
        }, {
            "type": "function",
            "function": {
                "name": "record_thought",
                "description": "记录思绪。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string"},
                        "text": {"type": "string"},
                    },
                },
            },
        }],
        "tool_choice": "auto",
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    r = httpx.post(URL, json=payload, headers=headers, timeout=120)
    r.raise_for_status()
    data = r.json()
    return data.get("choices", [{}])[0].get("message", {})


def parse_response(msg: dict) -> dict:
    """解析响应：是否调 express_to_human + 第一个 tool_call name + reasoning_text head"""
    tool_calls = msg.get("tool_calls") or []
    out = {
        "tool_calls": [tc["function"]["name"] for tc in tool_calls],
        "content_head": (msg.get("content") or "")[:200],
    }
    # 看是否调 express_to_human，如果是——记录 text 前 100 字
    for tc in tool_calls:
        if tc["function"]["name"] == "express_to_human":
            try:
                args = json.loads(tc["function"]["arguments"])
                out["express_text_head"] = (args.get("text") or "")[:200]
                out["text_length"] = len(args.get("text") or "")
            except Exception:
                pass
            break
    return out


def main():
    api_key = load_api_key()
    input_messages = load_input_messages()
    print(f"loaded {len(input_messages)} messages, last 3:")
    for i in range(len(input_messages) - 3, len(input_messages)):
        m = input_messages[i]
        role = m.get("role", "?")
        head = (m.get("content") or "")[:80] if role != "assistant" else json.dumps(m.get("tool_calls", [{}])[0]["function"]["name"], ensure_ascii=False)
        print(f"  [{i}] {role}: {head}")
    print(f"调用 GLM API {N_RUNS} 次...")
    print()

    results: list[dict] = []
    for run_idx in range(N_RUNS):
        t0 = time.time()
        try:
            response = call_glm(api_key, input_messages)
            elapsed = time.time() - t0
            summary = parse_response(response)
            summary["elapsed_s"] = round(elapsed, 1)
        except Exception as exc:
            summary = {"error": str(exc)}
        results.append(summary)

        tool_str = ", ".join(summary.get("tool_calls", [])) or "(无 tool_call)"
        content_brief = summary.get("content_head", "")[:120]
        express_brief = ""
        if "express_text_head" in summary:
            express_brief = f"\n     express_text[:{min(120, len(summary['express_text_head']))}]: {summary['express_text_head'][:120]}"
        print(f"  Run {run_idx + 1}/{N_RUNS}: tools=[{tool_str}] text_len={summary.get('text_length', '?')} elapsed={summary.get('elapsed_s', '?')}s")
        if content_brief:
            print(f"     reasoning[:120]: {content_brief}{express_brief}")

    print()
    print("=" * 70)
    express_count = sum(1 for r in results if "express_to_human" in r.get("tool_calls", []))
    rest_count = sum(1 for r in results if "rest" in r.get("tool_calls", []))
    thought_count = sum(1 for r in results if "record_thought" in r.get("tool_calls", []))
    print(f"统计 {N_RUNS} 次：")
    print(f"  - express_to_human 调用 {express_count}/{N_RUNS}（决定回复 = {express_count}/{N_RUNS}）")
    print(f"  - rest 直接休息 {rest_count}/{N_RUNS}")
    print(f"  - record_thought 只记思绪 {thought_count}/{N_RUNS}")

    if express_count >= 3:
        verdict = "✅ 复现：模型在此上下文下稳定决定再发一条（系统性 bug）"
    elif express_count >= 1:
        verdict = f"⚠️  偶发：5 次里 {express_count} 次决定回（部分概率行为）"
    else:
        verdict = "❌ 没复现：5 次模型都选择沉默/rest/thought（系统 bug 嫌疑更大）"
    print(f"\n结论：{verdict}")


if __name__ == "__main__":
    main()
