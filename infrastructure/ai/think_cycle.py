"""think effort 状态机 —— 事件触发的推理档位管理。

2026-09-18 v3（设计: zhp 提案 + alpha 整合 + zero 三补充）:
  三层入口决定每次模型调用的 reasoning effort:
    A. 人类消息事件（wake 或 mid-session 注入）→ 首次调用 minimal 快答
    B. think 工具显式升档（模型自判复杂度）→ override, TTL 轮
    C. 卡住兜底（连续基础设施异常）→ 强制 high
  优先级 C > B > A > 默认配置档。

  zero 三条补充（已实装）:
    - 分页豁免: detect_anomaly 对"行数>0"的结果不算异常（行数>0 即成功语义）
    - minimal 快答只做短确认: 首轮 minimal 的引导在 prompt 侧（agent._chat 注释）
    - 通信/生命周期工具豁免卡住判定: express_to_human / rest 结果不参与 anomaly 计数

状态生命周期 = agent 实例生命周期（一次 wake 一个 agent）。
init_for_wake() 在 run_conversation 入口重置; on_human_message() 在
_consume_human_events 渲染每条人类消息后调用; on_tool_result() 在主循环
每个工具结果落地后调用; decide() 在 _chat 每次请求前调用。
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

# ── 常量 ─────────────────────────────────────────────────────────
FAST_EFFORT = "minimal"

DEFAULT_OVERRIDE_TTL = 3          # B 入口 override 默认持续轮数
STUCK_THRESHOLD = 2               # C 入口: 连续基础设施异常达到此数 → 强制 high

# 通信/生命周期类工具: "慢/等待/失败重试"是正常语义, 不参与卡住判定 (zero 补充③)
EXEMPT_TOOLS = frozenset({
    "express_to_human", "rest",
})

# 结果 JSON 里视为"基础设施异常"的信号词 (仅这些触发卡住计数;
# 业务错误——工具正常返回的失败语义——不算)
_ANOMALY_PATTERNS = re.compile(
    r"(timeout|timed?\s*out|connection.*(refused|reset|closed)|traceback|"
    r"maximum\s+recursion|oserror\[errno|memoryerror)",
    re.IGNORECASE,
)

# 分页豁免 (zero 补充①): 行数>0 的结果无论文本长啥样都不算异常
_ROW_HIT = re.compile(r'"(?:rows?|count|total)"\s*:\s*([1-9][0-9]*)')


def default_ttl() -> int:
    v = os.environ.get("THINK_OVERRIDE_TTL", "")
    try:
        return max(1, int(v)) if v else DEFAULT_OVERRIDE_TTL
    except ValueError:
        return DEFAULT_OVERRIDE_TTL


# ── 状态 ─────────────────────────────────────────────────────────
def new_state() -> dict[str, Any]:
    """一个 wake 生命周期的 effort 状态（挂在 agent 实例上）。"""
    return {
        "fast_pending": False,     # A 入口: 待消费的快答
        "call_idx": 0,             # 本 wake 内模型调用序号（A 入口只认 0）
        "override": None,          # B/C 入口: {"effort": "high", "ttl": n} | None
        "stuck_streak": 0,         # C 入口: 连续基础设施异常计数
    }


def init_for_wake(state: dict[str, Any], *, human_message: bool = False) -> None:
    """wake 边界: 全量重置。人类消息类 wake 顺带挂 A 入口快答。"""
    state.update(new_state())
    if human_message:
        state["fast_pending"] = True


def on_human_message(state: dict[str, Any]) -> None:
    """人类消息事件落地后调用（wake 渲染或 mid-session 注入均算）。

    新消息 = 新的快答意图: 挂 A 入口并把 call_idx 归零——
    mid-session 注入场景修复点: 不归零则 call_idx>0 永不触发快答。
    override 保留: 模型刚 think 升档又来一条消息时, 深度意图优先于快答。
    """
    state["fast_pending"] = True
    state["call_idx"] = 0
    state["stuck_streak"] = 0


def on_tool_result(state: dict[str, Any], tool_name: str, result: Any) -> None:
    """主循环每个工具结果落地后调用。维护 C 入口卡住计数。"""
    if tool_name in EXEMPT_TOOLS:
        return                                   # 通信/生命周期豁免
    if detect_anomaly(result):
        state["stuck_streak"] += 1
        if state["stuck_streak"] >= STUCK_THRESHOLD:
            state["override"] = {"effort": "high", "ttl": default_ttl(), "via": "stuck"}
            state["stuck_streak"] = 0
            logger.warning("think: stuck %d 轮 → 强制 high (C 入口兜底)", STUCK_THRESHOLD)
    else:
        state["stuck_streak"] = 0


def detect_anomaly(result: Any) -> bool:
    """基础设施异常判定: 信号词命中且不满足分页豁免。"""
    text = result if isinstance(result, str) else str(result)
    if _ROW_HIT.search(text):
        return False                             # 分页豁免: 行数>0 即成功
    return bool(_ANOMALY_PATTERNS.search(text))


def decide(state: dict[str, Any], config_effort: str) -> tuple[str, str]:
    """_chat 每次模型调用前取档。返回 (effort, reason)。

    优先级 C > B > A > default（C/B 同住 override）。
    消费即推进: override 轮数在此处递减; fast 只在 call_idx==0 时消费。
    """
    state["call_idx"] += 1

    ov = state["override"]
    if ov:                                       # C 或 B
        effort, reason = ov["effort"], "stuck_escalation" if ov.get("via") == "stuck" else "think_override"
        ov["ttl"] -= 1
        if ov["ttl"] <= 0:
            state["override"] = None
        return effort, reason

    if state["fast_pending"]:
        # 消费规则: override (B/C) 缺席后的第一次调用。若 override 在先抢跑,
        # fast_pending 存活到 override 结束后的第一轮 (§6①: 人类快答意图不被
        # 模型升档吞掉)。request_override 显式置 False 属模型自判, 优先于此。
        state["fast_pending"] = False
        return FAST_EFFORT, "fast_first_reply"

    return config_effort, "config"


# ── B 入口: think 工具 → agent 状态桥 ────────────────────────────
# think 工具 handler 与 agent 分属两个模块。桥接不用 session_id——
# registry.dispatch 不透传 session_id 给 handler, 且每个实例独占一个 worker
# 进程, 模块级"当前活跃 state"直连即可, 无并发冲突。
# agent 每次 decide() 前刷新 active_state (见 agent.py 落点②)。
_ACTIVE_STATE: dict[str, Any] | None = None


def set_active_state(state: dict[str, Any] | None) -> None:
    """agent 每次 decide 前调用, 把自己的 state 挂为当前活跃 (None = 会话结束)。"""
    global _ACTIVE_STATE
    _ACTIVE_STATE = state


def _active_state() -> dict[str, Any] | None:
    return _ACTIVE_STATE


def request_override(_session_id: str = "", *, effort: str = "high", ttl: int | None = None) -> dict[str, Any]:
    """think 工具 up 落点: 写入当前活跃状态。无 agent 在跑时幂等失败。
    C 入口优先: stuck 强制升档期间, B 不得覆盖 (降档尤其危险, 升档无害但保持单一来源)。"""
    state = _active_state()
    if state is None:
        return {"ok": False, "error": "no_active_session", "hint": "当前无运行中会话, 下轮自动按默认档"}
    cur = state.get("override")
    if cur and cur.get("via") == "stuck":
        return {"ok": False, "error": "stuck_active", "hint": f"卡住强制 {cur['effort']} 生效中, 不可用 think 工具覆盖"}
    state["override"] = {"effort": effort, "ttl": ttl or default_ttl(), "via": "think_tool"}
    state["fast_pending"] = False
    return {"ok": True, "effort": effort, "ttl": state["override"]["ttl"]}


def clear_override(_session_id: str = "") -> dict[str, Any]:
    state = _active_state()
    if state is None:
        return {"ok": False, "error": "no_active_session"}
    cur = state.get("override")
    if cur and cur.get("via") == "stuck":
        return {"ok": False, "error": "stuck_active", "hint": "卡住强制升档中, 不可手动清除"}
    state["override"] = None
    return {"ok": True}


def status(_session_id: str = "") -> dict[str, Any]:
    state = _active_state()
    if state is None:
        return {"ok": False, "error": "no_active_session"}
    return {
        "ok": True,
        "fast_pending": state["fast_pending"],
        "call_idx": state["call_idx"],
        "override": state["override"],
        "stuck_streak": state["stuck_streak"],
    }


# ── think 工具注册 ────────────────────────────────────────────────
try:
    from interfaces.tools.registry import registry
    import json as _json

    def _handle_think(args: dict[str, Any], **kwargs) -> str:
        session_id = ""  # 桥接已改 active_state 直连, session_id 仅保持签名兼容
        action = str(args.get("action") or "up")
        if action == "up":
            effort = str(args.get("effort") or "high")
            if effort not in ("high", "medium", "low"):
                effort = "high"
            ttl = args.get("rounds")
            try:
                ttl = int(ttl) if ttl is not None else None
            except (TypeError, ValueError):
                ttl = None
            out = request_override(session_id, effort=effort, ttl=ttl)
        elif action == "down":
            out = clear_override(session_id)
        else:
            out = status(session_id)
        return _json.dumps(out, ensure_ascii=False)

    registry.register(
        name="think",
        toolset="actions",
        schema={
            "name": "think",
            "description": (
                "调自己的推理档位。当前问题复杂（架构设计/多步推理/排查根因）时调 "
                "action=up 升到深度推理，答完简单问题可用 down 降回。升档持续约 3 轮，"
                "到期自动回落。human 快答轮无需手动升档除非确实需要深度。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string", "enum": ["up", "down", "status"],
                        "description": "up=升档(默认) down=降回默认 status=查当前状态",
                    },
                    "effort": {
                        "type": "string", "enum": ["high", "medium", "low"],
                        "description": "升档目标档, 默认 high",
                    },
                    "rounds": {
                        "type": "integer",
                        "description": "升档持续轮数, 默认 3",
                    },
                },
                "required": ["action"],
            },
        },
        handler=_handle_think,
        check_fn=lambda: True,
        emoji="🧠",
    )
except ImportError:                              # 单测环境无 registry
    pass
