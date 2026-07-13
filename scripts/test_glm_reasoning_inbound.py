#!/usr/bin/env python3
"""GLM-5 入站 think 行为实验。

测三种把 think 拼回 messages 的方式，看 GLM-5 接受哪个、报错哪个：

  形式A：think 拼进 assistant.content（纯文本）
  形式B：assistant 消息带 reasoning_content 字段（与原生响应格式对称）
  形式C：think 完全不拼回（基准对照）

每种发一次 minimal 请求，复用 GLM-5 真实凭证。
"""
import os
import sys
import json
from pathlib import Path

import httpx

# 加载凭证
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 直接读 secrets.env
secrets_path = ROOT / "config" / "secrets.env"
for line in secrets_path.read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

API_KEY = os.environ["GLM_API_KEY"]
BASE_URL = os.environ.get("GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
MODEL = "glm-5"
URL = f"{BASE_URL.rstrip('/')}/chat/completions"

SAMPLE_THINK = "我要决定是否给用户回复'你好'。从历史看，用户刚才在询问系统状态。"
SAMPLE_CONTENT = "你好！有什么可以帮你的吗？"


def call(messages, label):
    print(f"\n========== {label} ==========")
    print(f"messages:\n{json.dumps(messages, ensure_ascii=False, indent=2)[:800]}")
    payload = {"model": MODEL, "messages": messages}
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=60) as c:
            r = c.post(URL, headers=headers, json=payload)
        print(f"HTTP {r.status_code}")
        data = r.json()
        if r.is_success:
            msg = data["choices"][0]["message"]
            print(f"✅ 接受。返回 message keys: {list(msg.keys())}")
            print(f"返回 reasoning_content 前80: {(msg.get('reasoning_content') or '')[:80]!r}")
            print(f"返回 content: {(msg.get('content') or '')[:120]!r}")
        else:
            err = data.get("error") or data
            print(f"❌ 拒绝。错误: {json.dumps(err, ensure_ascii=False)[:300]}")
    except Exception as e:
        print(f"💥 异常: {e!r}")


# 三轮独立实验对话，每轮用不同形式塞历史 assistant
def trial_form_A():
    """形式A：think 拼进 assistant.content（纯文本）。"""
    messages = [
        {"role": "system", "content": "你是一个测试助手，回答简短。"},
        {
            "role": "assistant",
            "content": f"[内部思路]\n{SAMPLE_THINK}\n[/内部思路]\n{SAMPLE_CONTENT}",
        },
        {"role": "user", "content": "继续：你刚才怎么想的？"},
    ]
    call(messages, "形式A：think 拼进 assistant.content")


def trial_form_B():
    """形式B：assistant 消息带 reasoning_content 字段（与原生响应格式对称）。"""
    messages = [
        {"role": "system", "content": "你是一个测试助手，回答简短。"},
        {
            "role": "assistant",
            "content": SAMPLE_CONTENT,
            "reasoning_content": SAMPLE_THINK,
        },
        {"role": "user", "content": "继续：你刚才怎么想的？"},
    ]
    call(messages, "形式B：assistant 带 reasoning_content 字段")


def trial_form_C():
    """形式C：基准对照，不拼 think。"""
    messages = [
        {"role": "system", "content": "你是一个测试助手，回答简短。"},
        {"role": "assistant", "content": SAMPLE_CONTENT},
        {"role": "user", "content": "继续：你刚才怎么想的？"},
    ]
    call(messages, "形式C：基准对照（不拼 think）")


if __name__ == "__main__":
    trial_form_A()
    trial_form_B()
    trial_form_C()
