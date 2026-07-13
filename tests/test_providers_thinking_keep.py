"""LLM Provider — thinking_keep_mode 行为校验。

覆盖三件事：
1. resolve_provider 按模型名路由到正确 family class
2. reuse 家族（GLMProvider）：跨轮 reasoning 拼回 assistant message 的 reasoning_content 字段
3. drop 家族（GenericOpenAIProvider，DeepSeek/Qwen/Kimi/Moonshot 默认走这个）：
   完全不动历史 messages，不写 reasoning_content —— 这是修 DeepSeek-Reasoner 多轮 400 的关键

DeepSeek 服务端明确要求多轮 messages 不带 reasoning_content 字段，否则返回 400。
本测试固化 GenericOpenAIProvider 默认 mode=drop 这一不变量。
"""
from __future__ import annotations

from infrastructure.ai.providers import (
    GLMProvider,
    GenericOpenAIProvider,
    OpenAIReasoningProvider,
    StepFunProvider,
    resolve_provider,
)


# ─────────────────── resolve_provider 路由 ───────────────────


def test_resolve_glm_family():
    assert isinstance(resolve_provider("glm-5.2"), GLMProvider)
    assert isinstance(resolve_provider("GLM-4.6"), GLMProvider)
    assert isinstance(resolve_provider("glm-z1-air"), GLMProvider)


def test_resolve_stepfun_family():
    # step- 前缀的文本模型路由到 StepFunProvider
    for model in ["step-3.7-flash", "step-3.5-flash", "step-2-16k", "step-3"]:
        p = resolve_provider(model)
        assert isinstance(p, StepFunProvider), f"{model} 应路由到 StepFunProvider"
        assert p.thinking_keep_mode == "reuse"


def test_resolve_stepfun_not_misroute_unsupported_models():
    """非 step- 前缀的 step-* 不应误匹配（这里无 LLM 入口，仅防御性）。"""
    # stepfun 字面量也匹配
    assert isinstance(resolve_provider("stepfun-custom"), StepFunProvider)
    # gpt-4o 等不该走 StepFun
    assert not isinstance(resolve_provider("gpt-4o"), StepFunProvider)
    assert not isinstance(resolve_provider("deepseek-reasoner"), StepFunProvider)


def test_resolve_openai_reasoning_family():
    assert isinstance(resolve_provider("o1-preview"), OpenAIReasoningProvider)
    assert isinstance(resolve_provider("o3-mini"), OpenAIReasoningProvider)
    assert isinstance(resolve_provider("o4-mini"), OpenAIReasoningProvider)


def test_resolve_generic_openai_for_domestic_models():
    # DeepSeek / Qwen / Kimi / Moonshot / 裸 GPT 都走 GenericOpenAIProvider（drop）
    for model in ["deepseek-reasoner", "qwen-max", "kimi-k1.5", "moonshot-v1-8k", "gpt-4o", "unknown-model"]:
        p = resolve_provider(model)
        assert isinstance(p, GenericOpenAIProvider), f"{model} 应路由到 GenericOpenAIProvider"
        assert p.thinking_keep_mode == "drop"


# ─────────────────── thinking_keep_mode=reuse（GLM）───────────────────


def test_glm_reuse_injects_reasoning_content_into_assistant():
    """GLM 跨轮延续：历史 assistant 的 reasoning_content 字段被 patch 上本轮 reasoning。"""
    provider = GLMProvider()
    assert provider.thinking_keep_mode == "reuse"

    messages = [
        {"role": "user", "content": "ping"},
        {"role": "assistant", "content": "pong"},
    ]
    reasonings = ["我决定回复 pong 因为用户说 ping"]
    result = provider.inject_into_messages(messages, reasonings)

    # 不动原始 messages list（_BaseProvider 返回 new list）
    assert "reasoning_content" not in messages[1]
    # 新 list 的 assistant 被注入 reasoning_content
    assert result[1]["reasoning_content"] == "我决定回复 pong 因为用户说 ping"
    # 保留 content
    assert result[1]["content"] == "pong"


def test_glm_reuse_skips_fake_assistant():
    """_is_fake 占位的 assistant 不应被注入。"""
    provider = GLMProvider()
    messages = [
        {"role": "assistant", "content": "占位", "_is_fake": True},
        {"role": "assistant", "content": "真回复"},
    ]
    result = provider.inject_into_messages(messages, ["上一轮思考"])
    assert "reasoning_content" not in result[0]  # fake 不动
    assert result[1]["reasoning_content"] == "上一轮思考"  # 真的注入


def test_glm_reuse_truncates_long_reasoning():
    provider = GLMProvider()
    messages = [{"role": "assistant", "content": "ok"}]
    long_think = "x" * 1000
    result = provider.inject_into_messages(messages, [long_think])
    trimmed = result[0]["reasoning_content"]
    # 300 头 + "…（中段省略）…"（8 字符）+ 300 尾 = 608
    assert len(trimmed) == 608
    assert trimmed.startswith("x" * 300)
    assert trimmed.endswith("x" * 300)
    assert "…" in trimmed


# ─────────────────── thinking_keep_mode=reuse（StepFun）───────────────────


def test_stepfun_reuse_injects_reasoning_content():
    """StepFun 与 GLM 同字段（reasoning_content），reuse 行为一致：拼回 assistant。"""
    provider = StepFunProvider()
    assert provider.thinking_keep_mode == "reuse"

    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "你好"},
    ]
    result = provider.inject_into_messages(messages, ["判断用户在打招呼"])
    # 不动原 messages
    assert "reasoning_content" not in messages[1]
    # 新 list 注入了
    assert result[1]["reasoning_content"] == "判断用户在打招呼"
    assert result[1]["content"] == "你好"


def test_stepfun_passes_reasoning_effort():
    """StepFun 实测接受 reasoning_effort 字段，与 GLM 同协议透传。"""
    out = StepFunProvider().customize_payload({}, reasoning_config={"effort": "high"})
    assert out["reasoning_effort"] == "high"


# ─────────────────── reuse_max_rounds（per-provider 上限）───────────────────


def test_reuse_max_rounds_per_provider():
    """每个 provider 类声明自己的 reuse 累积上限。

    GLM=5（实测可维持不漂移）；StepFun=3（轻量模型累积效应强，需收紧）。
    历史背景：原硬编码 max_rounds=10，StepFun Flash 在 wake-499 把 10 轮 reasoning
    累积成"自我指令循环"，跑了 25 轮 add_lesson。
    """
    assert GLMProvider().reuse_max_rounds == 5
    assert StepFunProvider().reuse_max_rounds == 3
    # drop 模式的 provider 不参与 reuse，reuse_max_rounds 值不影响行为，但保持合理默认
    assert GenericOpenAIProvider().reuse_max_rounds == 10


def test_inject_uses_provider_default_when_max_rounds_none():
    """不传 max_rounds → 用 provider 的 reuse_max_rounds 默认值。

    这是 agent.py 调用 inject_into_messages 的新形态（不再 hardcode 10）。
    """
    provider = StepFunProvider()  # reuse_max_rounds=3
    # 5 个 assistant 槽位 + 5 段 reasoning，但只应 patch 最近 3 个
    messages = [{"role": "assistant", "content": f"r{i}"} for i in range(5)]
    reasonings = [f"思考{i}" for i in range(5)]
    result = provider.inject_into_messages(messages, reasonings)  # 不传 max_rounds
    patched = [m for m in result if "reasoning_content" in m]
    assert len(patched) == 3
    # 最近 3 段被注入：思考2/3/4
    assert result[-1]["reasoning_content"] == "思考4"
    assert result[-3]["reasoning_content"] == "思考2"
    # 更早的不动
    assert "reasoning_content" not in result[-4]


def test_explicit_max_rounds_param_still_works():
    """显式传 max_rounds 参数仍然生效（覆盖 provider 默认，用于测试或特殊场景）。"""
    provider = GLMProvider()  # 默认 5
    messages = [{"role": "assistant", "content": f"r{i}"} for i in range(5)]
    reasonings = [f"思考{i}" for i in range(5)]
    # 显式只取最近 1 轮
    result = provider.inject_into_messages(messages, reasonings, max_rounds=1)
    patched = [m for m in result if "reasoning_content" in m]
    assert len(patched) == 1
    assert result[-1]["reasoning_content"] == "思考4"


# ─────────────────── thinking_keep_mode=drop（DeepSeek 等）───────────────────


def test_generic_drop_does_not_inject_reasoning_content():
    """修 DeepSeek-Reasoner 400 bug：GenericOpenAIProvider 不能写 reasoning_content。

    DeepSeek 官方文档明确：多轮 messages 不允许带 reasoning_content 字段，否则 400。
    """
    provider = GenericOpenAIProvider()
    assert provider.thinking_keep_mode == "drop"

    messages = [
        {"role": "user", "content": "ping"},
        {"role": "assistant", "content": "pong"},
    ]
    reasonings = ["上一轮思考"]
    result = provider.inject_into_messages(messages, reasonings)

    # 关键不变量：assistant message 上**没有** reasoning_content 字段
    assert "reasoning_content" not in result[1]
    # 原始 messages 不变
    assert messages == result


def test_openai_reasoning_provider_also_drops():
    """OpenAI o1/o3/o4 官方建议不拼历史 reasoning。"""
    provider = OpenAIReasoningProvider()
    assert provider.thinking_keep_mode == "drop"
    messages = [{"role": "assistant", "content": "ok"}]
    result = provider.inject_into_messages(messages, ["思考"])
    assert "reasoning_content" not in result[0]


# ─────────────────── extract_reasoning 同字段名兼容 ───────────────────


def test_extract_reasoning_via_uniform_field_name():
    """GLM/StepFun/DeepSeek/Qwen/Kimi 出站都用 message.reasoning_content —— 一份抽取逻辑覆盖。"""
    for provider_cls in [GLMProvider, StepFunProvider, GenericOpenAIProvider]:
        p = provider_cls()
        assert p.extract_reasoning({"reasoning_content": "想了一遍"}) == "想了一遍"
        assert p.extract_reasoning({"reasoning": "fallback"}) == "fallback"
        assert p.extract_reasoning({}) == ""
        assert p.extract_reasoning({"reasoning_content": "  "}) == ""


def test_openai_reasoning_extracts_from_reasoning_summary():
    """o1: 走 message.reasoning.summary，不复用 reasoning_content 字段。"""
    p = OpenAIReasoningProvider()
    assert p.extract_reasoning({"reasoning": {"summary": "推理摘要"}}) == "推理摘要"
    assert p.extract_reasoning({"reasoning": "字符串形态的 summary"}) == "字符串形态的 summary"


# ─────────────────── customize_payload reasoning_effort ───────────────────


def test_glm_passes_5_level_effort():
    out = GLMProvider().customize_payload({}, reasoning_config={"effort": "xhigh"})
    assert out["reasoning_effort"] == "xhigh"


def test_generic_drops_effort():
    out = GenericOpenAIProvider().customize_payload({}, reasoning_config={"effort": "xhigh"})
    assert "reasoning_effort" not in out


def test_openai_reasoning_remaps_5_to_3_levels():
    p = OpenAIReasoningProvider()
    assert p.customize_payload({}, reasoning_config={"effort": "minimal"})["reasoning_effort"] == "low"
    assert p.customize_payload({}, reasoning_config={"effort": "xhigh"})["reasoning_effort"] == "high"
    assert p.customize_payload({}, reasoning_config={"effort": "medium"})["reasoning_effort"] == "medium"


def test_payload_not_mutated():
    """customize_payload 返回副本，不修改入参。"""
    src = {"model": "glm"}
    GLMProvider().customize_payload(src, reasoning_config={"effort": "high"})
    assert src == {"model": "glm"}
