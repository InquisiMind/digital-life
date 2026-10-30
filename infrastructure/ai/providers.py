"""LLM provider adapter — 模型知识的唯一家园，agent.py 不识别具体模型。

每个家族仅声明 `thinking_keep_mode` 决定跨轮 reasoning 拼回策略：
  reuse   把 reasoning_content 字段拼回历史 assistant message，模型继续读。
          适合官方文档明确支持「入站 reasoning_content 字段」的家族：
            GLM-4.5/4.6/5/5.2（本仓 789 实测）、Kimi（thinking.keep=on）、Qwen3。
  drop    完全不拼历史 reasoning，只回传 content。
          适合官方明确要求多轮不接 reasoning_content 的家族：
            DeepSeek-Reasoner（带 reasoning_content 入站会被服务端 400）。
          OpenAI o1/o3/o4 也走 drop —— 官方建议不拼历史 reasoning。
          其它"未经实测"的家族默认走 drop，安全为先。

出站 reasoning 抽取（extract_reasoning）：
  GLM / DeepSeek / Kimi / Qwen 四家 OpenAI 兼容 API 在 message.reasoning_content
  字段上天然同名（各家官方文档明确），一份抽取逻辑覆盖。
  OpenAI o 系列走 message.reasoning.summary，专属子类处理。

思考深度（customize_payload / reasoning_effort）：
  产品只认三档 quick / standard / deep（人的基本配置 agent.reasoning_depth），
  厂商值是各家族声明的 depth_map：GLM/StepFun low/high/xhigh；OpenAI o 系
  low/medium/high；其它家族不识别该字段，payload 不带，避免严格模式 400。
  网关值域与家族不同时（newapi 只认 low/high/max）用配置 model.reasoning_depth_map
  覆盖单档，声明错了就 400 一次改一次——不做运行时自学映射（会把配置错误藏起来）。
  运行时没有任何场景规则：每次调用的深度 = 这个人的基本档（2026-10-20 zhp 定稿，
  旧 think_cycle 状态机 A/B/C 入口与 effort_domain 自学已退役）。

Claude：
  原生 api.anthropic.com 走 /v1/messages 而非 OpenAI /chat/completions，
  tools schema 与 thinking block 结构与本仓 agent.py 的 OpenAI 工具调用协议不兼容。
  目前只能经 LiteLLM 等 OpenAI 兼容代理转译跑（对话+工具可用，但思考逐轮牺牲，
  因 OpenAI 协议层无 thinking_blocks.signature 字段，stateless 多轮会 400）。
  原生 Claude thinking 闭环需要独立 adapter（不在本版本范围）。
"""
from __future__ import annotations

from typing import Any, Mapping, Protocol

# 产品层思考深度三档（人的基本配置 agent.reasoning_depth 的值域）
REASONING_DEPTHS: tuple[str, ...] = ("quick", "standard", "deep")
DEFAULT_REASONING_DEPTH = "standard"
REASONING_DEPTH_LABELS: Mapping[str, str] = {
    "quick": "快答",
    "standard": "标准",
    "deep": "深思",
}


class LLMProvider(Protocol):
    """模型家族适配接口。agent.py 只调接口，不识别当前是哪一家。

    子类只需声明两件事：
      1. name              — 家族标识
      2. thinking_keep_mode — "reuse" | "drop"，决定历史 reasoning 怎么拼回

    reasoning 抽取/注入/思考强度三件套的默认实现见 `_BaseProvider`；
    仅当某家族有真差异化需求（如 OpenAI o1 出站字段不同）时再覆盖个别方法。
    """

    name: str
    thinking_keep_mode: str

    def extract_reasoning(self, message: Mapping[str, Any]) -> str: ...
    def inject_into_messages(
        self,
        messages: list[dict[str, Any]],
        reasonings: list[str],
        *,
        max_rounds: int = 10,
    ) -> list[dict[str, Any]]: ...
    def customize_payload(
        self,
        payload: dict[str, Any],
        *,
        reasoning_config: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]: ...


class _BaseProvider:
    """默认实现：基于 thinking_keep_mode 复用一套 inject 逻辑。

    家庭类只需继承并声明 name + thinking_keep_mode，不再各写注入代码。
    """

    name: str = "base"
    thinking_keep_mode: str = "drop"
    # reuse 模式下最多把几轮历史 reasoning 拼回去。0 = 等价于 drop。
    # 太大会让轻量模型（如 StepFun Flash）把自己的历史推理当成 todo 累积执行，
    # 形成"自我指令循环"—— wake-499 案例:25 轮 LLM 调用全在执行 reasoning 里写的
    # "继续升级 lesson"。每个家族按自身对历史 reasoning 的"服从度"独立设：
    # GLM-5.2 实测可以 5 轮不漂移；StepFun Flash 累积效应更强，限制到 3 轮。
    reuse_max_rounds: int = 10
    # 产品三档 → 本家族 reasoning_effort 值。None = 家族不识别该字段，payload 不带。
    depth_map: Mapping[str, str] | None = None
    # 本家族端点是否支持 SSE 流式 chat/completions（agent._chat 据此选流式路径，
    # 语音会话的"边生成边按句 TTS"依赖它）。默认 True：OpenAI-compatible 端点
    # 基本都有；个别家族（o1 老流式约束）显式关。
    supports_stream: bool = True

    def extract_reasoning(self, message: Mapping[str, Any]) -> str:
        """国内主流模型（GLM/DeepSeek/Qwen/Kimi）出站 reasoning_content 字段同名。

        这是 4 家官方文档逐字确认过的兼容点；本仓 GLM 789 数字实验佐证。
        """
        rc = message.get("reasoning_content")
        if isinstance(rc, str) and rc.strip():
            return rc.strip()
        rc2 = message.get("reasoning")
        if isinstance(rc2, str) and rc2.strip():
            return rc2.strip()
        return ""

    def inject_into_messages(
        self,
        messages: list[dict[str, Any]],
        reasonings: list[str],
        *,
        max_rounds: int | None = None,
    ) -> list[dict[str, Any]]:
        """按 family 声明的 thinking_keep_mode 决定是否拼回历史 reasoning。

        reuse：把 reasoning_content 写回对应 assistant message（GLM/Kimi/Qwen）。
        drop ：直接 return（DeepSeek-Reasoner 服务端要求；OpenAI o 系列官方建议）。

        拼回轮数 = max_rounds 参数 ?? self.reuse_max_rounds（每家族声明自己的上限）。
        单轮 reasoning 截到 600 chars（300 头 + 300 尾），避免无限堆叠。
        """
        if self.thinking_keep_mode != "reuse" or not reasonings:
            return messages
        # max_rounds=None → 用 provider 类声明的默认上限；显式传参则覆盖（用于测试）
        effective_max = max_rounds if max_rounds is not None else self.reuse_max_rounds
        if effective_max <= 0:
            return messages
        recent = reasonings[-effective_max:]
        assistant_idx = [
            i for i, m in enumerate(messages)
            if m.get("role") == "assistant"
            and not m.get("_is_fake")
        ]
        if not assistant_idx:
            return messages
        pair_count = min(len(recent), len(assistant_idx))
        to_patch_assistants = assistant_idx[-pair_count:]
        to_patch_thinks = recent[-pair_count:]
        new_messages = list(messages)
        for assistant_index, think in zip(to_patch_assistants, to_patch_thinks):
            m = dict(new_messages[assistant_index])
            if len(think) <= 600:
                trimmed_think = think
            else:
                trimmed_think = think[:300] + "…（中段省略）…" + think[-300:]
            # 原生 reasoning_content 字段：GLM-5.2 出站时也用这个字段，
            # 入站时会被模型读取进 reasoning context。content 保留不动。
            m["reasoning_content"] = trimmed_think
            new_messages[assistant_index] = m
        return new_messages

    def customize_payload(
        self,
        payload: dict[str, Any],
        *,
        reasoning_config: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """按 depth_map 把产品档位写成本家族的 reasoning_effort；无 depth_map 不带字段。

        reasoning_config = {"depth": quick|standard|deep, "depth_map"?: {...}}，
        depth_map 是配置层对单档的覆盖（网关值域与家族不同时用，如 newapi deep→max）。
        """
        out = dict(payload)
        if self.depth_map is None or not reasoning_config:
            return out
        depth = str(reasoning_config.get("depth") or "").strip()
        if depth not in self.depth_map:
            return out
        override = reasoning_config.get("depth_map")
        mapped = ""
        if isinstance(override, Mapping):
            mapped = str(override.get(depth) or "").strip()
        out["reasoning_effort"] = mapped or self.depth_map[depth]
        return out


class GLMProvider(_BaseProvider):
    """智谱 GLM（GLM-4.5/4.6/5/5.2）家族 —— 当前唯一在生产中验证过的家族。

    出站：message.reasoning_content（4 家国内 OpenAI 兼容 API 同名）
    入站：GLM-5.2 实测读取 reasoning_content 字段并投入下一轮 reasoning context
          （2026-06 "789 数字"实验：含 reasoning_content 的历史 assistant message
          入站后，模型能在多轮里精确回忆）。

    GLM-5 时代入站被静默忽略 → 用 markdown "上一轮思考" 引证块 hack；
    GLM-5.2 已解除限制，用原生 reasoning_content 字段。改回原生是为避免长 context
    中模型 reasoning 卡在早期 phase、循环重复旧 thinking。
    """

    name = "glm"
    thinking_keep_mode = "reuse"
    # GLM-5.2 实测可以维持 5 轮 reasoning 跨轮延续而不漂移；更多轮易让早期思考
    # 在长 context 里反复出现、误导当前决策。
    reuse_max_rounds = 5
    # 原生五档 minimal/low/medium/high/xhigh；产品只用三个：medium 在 newapi
    # 类网关上不存在（曾被 400 后悄悄映射成 high），标准档直接取 high。
    depth_map = {"quick": "low", "standard": "high", "deep": "xhigh"}


class StepFunProvider(_BaseProvider):
    """阶跃星辰 Step 系列（step-3 / step-3.5-flash / step-3.7-flash 等）。

    出站：message.reasoning_content（与 GLM 同名，国内 OpenAI 兼容 API 通用字段）。
    入站：reuse —— 把历史 reasoning_content 拼回下一轮 messages，跨轮延续思路。
          实测 step-3.7-flash 在多轮中能正确读取并延续（2026-07 接入验证）。
    reasoning_effort：原生接受 minimal/low/medium/high/xhigh 五档（实测均不报错，
                      内部如何使用未公开，保守按与 GLM 同样的字段透传）。
    """

    name = "stepfun"
    thinking_keep_mode = "reuse"
    # StepFun Flash 把历史 reasoning 当 todo 累积执行的倾向比 GLM 强（wake-499
    # 案例：10 轮累积导致 25 轮 LLM 调用全在执行"继续升级 lesson"的自我指令）。
    # 收紧到 3 轮：足够延续近期思路，又限制累积效应。
    reuse_max_rounds = 3
    # 与 GLM 同协议透传（五档实测均不报错，内部用法未公开）
    depth_map = {"quick": "low", "standard": "high", "deep": "xhigh"}


class DeepSeekProvider(_BaseProvider):
    """DeepSeek 官方端点家族（deepseek-chat / deepseek-flash / v4 系列）。

    2026-10-28 实测接入（语音小张 deepseek-flash）踩的三个坑全部在此消化：
      1. 历史里 assistant 消息带 reasoning_content → 400（"must be passed
         back"）→ 出站剥掉（与 DeepSeek-Reasoner 官方要求一致）。
      2. V4 系默认思考模式，payload 以 tool 消息结尾（agent 工具循环标准
         形状）时要求回传思考 → 400。语音场景本来就要快 → 固定
         ``thinking: {"type": "disabled"}``。要开思考需另立方案（回传
         reasoning 或换端点），不要在这里偷偷打开。
      3. 不认 reasoning_effort 字段 → 不带。
    流式：官方 SSE 完全 OpenAI 兼容（supports_stream=True，含 usage chunk）。
    """

    name = "deepseek"
    thinking_keep_mode = "drop"
    depth_map = None
    supports_stream = True

    def customize_payload(
        self,
        payload: dict[str, Any],
        *,
        reasoning_config: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = dict(payload)
        payload.pop("reasoning_effort", None)
        payload["thinking"] = {"type": "disabled"}
        # 出站剥历史 reasoning_content（DS 400）。messages 为 list[dict]，
        # 就地去键；非 dict 项（理论不该有）跳过。
        msgs = payload.get("messages")
        if isinstance(msgs, list):
            for m in msgs:
                if isinstance(m, dict):
                    m.pop("reasoning_content", None)
        return payload


class GenericOpenAIProvider(_BaseProvider):
    """通用 OpenAI 兼容家族 —— DeepSeek / Qwen / Kimi / Moonshot / GPT-4o 等。

    出站抽取默认推理字段（4 家国内模型同名 reasoning_content；GPT 系列多数无 reasoning 字段）。
    入站 drop：默认不拼历史 reasoning —— 这是安全默认。
      ⚠️ DeepSeek-Reasoner 服务端明确要求多轮 messages 不带 reasoning_content，
         否则返回 400（官方文档原文），旧版 inject 全量拼回是 latent bug。
      ⚠️ GLM/Kimi/Qwen 这三家**可以**拼回去（思考链跨轮延续），但本仓仅实测过 GLM；
         想为这 3 家启用跨轮 thinking，把对应家族独立成 Provider 类并设
         thinking_keep_mode="reuse"，不要改本类默认。
    请求参数：丢弃 reasoning_config（不透传无效字段，避免 OpenAI o1 严格模式 400）。
    """

    name = "generic_openai"
    thinking_keep_mode = "drop"


class OpenAIReasoningProvider(_BaseProvider):
    """OpenAI o1 / o3 / o4 推理模型家族。

    特点：
      - 支持 reasoning_effort，但值域窄：low / medium / high（不支持 minimal/xhigh）
      - reasoning 出站字段：message.reasoning.summary（不是 reasoning_content）
      - 入站不应拼历史 reasoning（OpenAI 官方建议）
    """

    name = "openai_reasoning"
    thinking_keep_mode = "drop"
    # 值域窄：low / medium / high（不支持 minimal/xhigh）
    depth_map = {"quick": "low", "standard": "medium", "deep": "high"}
    # o 系流式约束多（旧版不支持 stream），保持非流式走已验证路径
    supports_stream = False

    def extract_reasoning(self, message: Mapping[str, Any]) -> str:
        """OpenAI o1: 拿 message.reasoning.summary（字符串）。"""
        rs = message.get("reasoning")
        if isinstance(rs, dict):
            summary = rs.get("summary")
        elif isinstance(rs, str):
            summary = rs
        else:
            summary = ""
        if isinstance(summary, str) and summary.strip():
            return summary.strip()
        # fallback 走 base 的 reasoning_content（应对 OpenAI 兼容代理转译）
        return super().extract_reasoning(message)


# 简易工厂：按 model name 推断家族。
# 接新家族（含给 DeepSeek/Kimi/Qwen 显式启用 thinking_keep="reuse"）在此加分支。
def resolve_provider(model: str) -> LLMProvider:
    """按 model 名选 provider。未识别回落 GenericOpenAIProvider（保守 drop）。

    顺序：先识别需要专属类（o1/o3/o4 → OpenAIReasoningProvider），
    再识别 GLM 家族（GLMProvider，已实测 thinking 闭环），
    其余一律 GenericOpenAIProvider（默认 drop，避免 DeepSeek 等家族 400 风险）。
    """
    m = (model or "").lower()
    if "o1" in m or "o3" in m or "o4" in m:
        return OpenAIReasoningProvider()
    if "glm" in m:
        return GLMProvider()
    if "deepseek" in m or m.startswith("ds-"):
        return DeepSeekProvider()
    if m.startswith("step-") or "stepfun" in m:
        # step-3 / step-3.5-flash / step-3.7-flash / step-2-16k 等。
        # 区分于 OpenAI 兼容代理里可能出现的其它 "step" 子串，用 "step-" 前缀+ "stepfun" 双重匹配。
        # 注意：step-1x-medium / step-tts 等非文本模型本仓不调用，不会被误匹配（无 LLM 调用入口）。
        return StepFunProvider()
    # deepseek / qwen / kimi / moonshot / gpt-* / claude / 其他 OpenAI 兼容
    # 默认走 GenericOpenAIProvider（thinking_keep_mode=drop），不拼历史 reasoning。
    # Claude 经 LiteLLM 代理时也走这条，但无法维持 thinking 闭环（见 file docstring）。
    return GenericOpenAIProvider()


__all__ = [
    "DEFAULT_REASONING_DEPTH",
    "REASONING_DEPTHS",
    "REASONING_DEPTH_LABELS",
    "LLMProvider",
    "GLMProvider",
    "StepFunProvider",
    "DeepSeekProvider",
    "GenericOpenAIProvider",
    "OpenAIReasoningProvider",
    "resolve_provider",
]
