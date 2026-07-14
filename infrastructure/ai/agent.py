"""Minimal project-owned agent loop with OpenAI-compatible tool calling."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import re
from pathlib import Path
from typing import Any, Mapping

import httpx

from infrastructure.config import get_runtime_home
from interfaces.tools.registry import registry

logger = logging.getLogger(__name__)


@dataclass
class AIAgent:
    model: str = ""
    api_key: str | None = None
    base_url: str | None = None
    provider: str | None = None
    api_mode: str = "chat_completions"
    max_iterations: int = 20
    reasoning_config: Mapping[str, Any] | None = None
    quiet_mode: bool = True
    platform: str = "l4"
    session_id: str = ""
    session_db: Any = None
    audit_ctx: Any = None  # WakeContext | None — dual-write sink for the new audit DB
    # 实例 ID（账号级熔断 trip 时记录是谁触发的；不参与推理）。
    instance_id: str = ""
    # 本 session 累计 token 用量（用于精力-token 耦合 + 写回 sessions 表）。
    # 每次 _chat() 返回后由 _record_token_usage() 累加。
    session_input_tokens: int = 0
    session_output_tokens: int = 0
    enabled_toolsets: list[str] | tuple[str, ...] | None = None
    skip_memory: bool = True
    logs_dir: Path = field(default_factory=lambda: get_runtime_home() / "sessions")

    def __post_init__(self) -> None:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.session_log_file = self.logs_dir / f"{self.session_id or 'session'}.json"
        # 字面 LLM input dump 目录：每次调 _chat 之前的 _messages_for_call
        # 必须达 "字面相等"。前端展示 / sessions JSON 都可能跟实际不一致
        # （chat_id 展开成群名、reasoning 注入、compact 等），这套 dump
        # 是唯一 ground truth。
        # 文件名 <session_id>__call_<n>.json，每 wake 的每次 LLM call 一份。
        self._dumps_dir = self.logs_dir.parent / "sessions_dumps"
        self._dumps_dir.mkdir(parents=True, exist_ok=True)
        self._call_seq = 0
        self._ensure_tools_loaded()
        # LLM provider（模型知识唯一家园）：按 model 名 resolve，
        # 负责出站 reasoning 提取 + 入站 reasoning 拼接格式。agent.py 只调 provider。
        from infrastructure.ai.providers import resolve_provider
        self._provider = resolve_provider(self.model)
        if self.session_db and self.session_id:
            self.session_db.create_session(
                self.session_id,
                source=self.platform,
                model=self.model,
                model_config={"provider": self.provider, "base_url": self.base_url, "api_mode": self.api_mode},
            )
        # Mid-session entity recall tracking
        self._last_scanned_msg_count: int = 0
        self._injected_entities: set[str] = set()
        self._injected_memory_ids: set[str] = set()
        # Track recall injection message indices so we only keep the LAST round
        self._recall_injection_indices: list[int] = []
        # Mid-session event injection tracking
        self._injected_signal_event_ids: set[int] = set()
        # Counter for synthetic tool_call IDs (system context injection)
        self._sys_tool_counter: int = 0
        # Audit dual-write bookkeeping (populated when audit_ctx attached)
        self._audit_pending_tool_count: int = 0
        self._audit_assistant_had_calls: bool = False
        # 最近若干轮 reasoning 历史（同 wake 多 LLM call 间跨轮延续思路）。最多保留 12 条
        # （capacity 比注入的默认 10 轮大一点，给 segment narr 叙事等额外用途留余量）。
        # ← 已废弃：reasoning_content 现在直接在 assistant msg dict 里保留，
        #   _strip_old_reasoning 按轮次摘除超出 max_rounds 的。不再需要这个 list。
        # self._reasoning_history: list[str] = []

    def run_conversation(
        self,
        prompt: str,
        *,
        system_message: str | None = None,
        conversation_history: list[dict[str, Any]] | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        session_id = self.session_id or task_id or "adhoc"
        # 每个 wake 推进一次段号。设计语义：segment_index = wake 序号，单调递增。
        # 同一 wake 内的所有消息（system/user/assistant/tool/sys_tool 注入）共享同一段号。
        # 这是新段启始的唯一入口；append_message 自身不自增。
        # 第一次 wake：create_session 设 0 → advance 到 1 → 写消息段号 1。
        # 续接 wake：get_messages/_restore_segment_index 恢复到 MAX(seg) → create_session
        # 看见 session 已存在不重置 → advance 到 MAX+1 → 写消息段号 MAX+1。
        if self.session_db and (self.session_id or task_id):
            try:
                self.session_db.advance_segment(session_id)
            except Exception:
                logger.debug("advance_segment failed", exc_info=True)
        messages: list[dict[str, Any]] = []
        if system_message:
            messages.append({"role": "system", "content": system_message})
            self._append_message(session_id, "system", system_message)
            # Persist the full system prompt to audit so render_input_for_call()
            # can replay the *exact* message the model saw (not just a ref).
            if self.audit_ctx is not None:
                try:
                    self.audit_ctx.record_system_prompt(system_message)
                except Exception:
                    logger.debug("Failed to record system prompt in audit", exc_info=True)
        if conversation_history:
            # 通用 dispatcher：含 _sys_tool tag 的 user msg 转 tool_call pair，
            # 无 tag 的 user（如 continuation 中拉回的旧 action_prompt 裸 user）保留原样。
            # 不再区分"新 session / 续用 session"路径，行为一致。
            messages.extend(self._convert_user_to_tool(conversation_history))
        # action_prompt as user message (GLM requires at least one user message)
        messages.append({"role": "user", "content": prompt})
        self._append_message(session_id, "user", prompt)
        # New session: load prior JSON content for merge.
        # Continuation: conversation_history already includes prior messages → no base needed.
        try:
            if system_message and self.session_log_file.is_file() and self.session_log_file.stat().st_size > 0:
                existing = json.loads(self.session_log_file.read_text(encoding="utf-8"))
                self._log_base_messages = existing.get("messages", [])
            else:
                self._log_base_messages = []
        except Exception:
            self._log_base_messages = []

        tool_calls_seen: list[dict[str, Any]] = []
        self._write_log(messages)
        if not self._can_call_llm():
            final = "本地 L4 运行时已启动，但当前未配置可用的 OpenAI-compatible 模型接口。"
            self._append_message(session_id, "assistant", final)
            self._write_log(messages + [{"role": "assistant", "content": final}])
            return {"final_response": final, "tool_calls": tool_calls_seen, "status": "blocked"}

        # 防瞎忙："仅调 instrument 类工具（sense_* / record_thought / sense_schedule 之类）
        # 连续 N 轮没有 action 类工具调用 → 注入提示要求 rest。
        # 防止 LLM 在没真正工作时反复 sense_time → sense_vitals → record_thought → ...
        SENSE_ONLY_TOOLS = {
            "sense_time", "sense_vitals", "sense_event_queue",
            "sense_schedule", "sense_todos", "sense_scratchpad",
            "sense_entity", "sense_memory", "sense_daily",
            "sense_wake_reason", "sense_users", "sense_memory", "sense_conversation",
            "record_thought", "record_lesson",
        }
        MAX_SENSE_ONLY_ROUNDS = 10
        sense_only_streak = 0

        for _ in range(max(1, int(self.max_iterations or 1))):
            # Layer 2（拼）：进新一轮 _chat 前，控制历史 assistant 消息的
            # reasoning_content 可见范围——只保留最近 N 轮的推理，更早的摘掉。
            # 这取代了旧的 _reasoning_history 平面 list + inject 拼接方案——
            # 后者用 zip 配对 assistant msg 会错位，已被实证导致重复发消息 bug。
            # 现在每条 assistant msg 自带 reasoning_content（append 时直接放进 dict），
            # 只需要"摘掉超出 max_rounds 的旧 msg 的 reasoning_content"即可。
            _messages_for_call = self._strip_old_reasoning(messages)

            # ── 字面 dump：每次 LLM 调用前保存 _messages_for_call 的字面副本 ──
            # 包括 system / history / 注入的 reasoning / compact 后的 segment。
            # 这一份是模型实际看到的，与前端渲染可能不一致——它是 ground truth。
            self._dump_llm_input(_messages_for_call)

            response = self._chat(_messages_for_call)
            # 接通 LLM token usage（写 budget_log + 累加 session_*_tokens；预算闸门和
            # 精力-token 耦合都依赖它）。放在最小解析前，避免后续处理异常时丢这笔账。
            self._record_token_usage(response.get("raw"))
            assistant = response.get("message") or {}
            content = assistant.get("content") or ""
            tool_calls = assistant.get("tool_calls") or []
            reasoning = assistant.get("reasoning") or ""
            # assistant message 自带 reasoning_content（GLM 原生返回），直接放进去——
            # 下一轮 LLM 从 messages 列表读就能看到自己上轮的推理，天然有序、无需拼接。
            # 这取代了原来 _reasoning_history + inject_into_messages 的拷回逻辑——
            # 后者用扁平 list zip 配对 assistant msg 会错位、误把别的 reasoning 挂到不相干
            # 的 message 上，已被实证导致重复发消息 bug（wake-1973）。
            msg_dict: dict[str, Any] = {"role": "assistant", "content": content, "tool_calls": tool_calls or None}
            if reasoning:
                msg_dict["reasoning_content"] = reasoning
            messages.append(msg_dict)
            # Layer 1 落库：reasoning 透传到 messages 表的 reasoning 列（schema 已经有，之前没填）
            self._append_message(
                session_id, "assistant", content,
                tool_calls=tool_calls or None,
                reasoning=reasoning or None,
            )
            # 维护最近 N 轮 reasoning 历史给下一轮注入用
            # ← 已废弃：reasoning_content 现在直接写进 messages 列表的 assistant msg 里，
            #   _strip_old_reasoning 按轮次截断。不再需要 _reasoning_history 平面 list。
            self._write_log(messages)
            if not tool_calls:
                # 中途信号触发"延续 turn"——模型自然结束本轮但事件队列里有新到的
                # fan_out 消息（跨实例来源），强制再开一轮让模型处理。
                #
                # 注意：原 session_events.peek 是同进程内存，**多实例 subprocess 部署下
                # 不共享**。改成扫 events 表（DB 持久化），所有进程都能看到。
                pending_new: list[dict] = []
                try:
                    from domain.lifecycle.events import pop_due_events
                    due = pop_due_events(limit=10)
                    pending_new = [
                        e for e in due
                        if e.get("event_id") not in self._injected_signal_event_ids
                        and e.get("kind") in ("message", "group_message")
                    ]
                except Exception:
                    pass

                if pending_new:
                    # 把 due 队列里的新消息事件转成 signal 入内存（让 _inject_signalled_events
                    # 在下一轮 _chat 调用时把它们组装成 wake_signal 注入对话）。
                    try:
                        from domain.lifecycle.session_events import signal_new_events
                        from domain.lifecycle.event_registry import get_event_type
                        summaries = []
                        for ev in pending_new:
                            kind = ev.get("kind", "")
                            td = get_event_type(kind)
                            summaries.append({
                                "event_id": ev.get("event_id"),
                                "kind": kind,
                                "display_name": td.display_name if td else kind,
                                "description": td.description if td else "",
                                "payload": ev.get("payload", {}),
                            })
                        signal_new_events(summaries)
                    except Exception:
                        pass
                    continue
                return {"final_response": content, "tool_calls": tool_calls_seen, "status": "completed"}

            # 防瞎忙：检查本轮所有 tool_call 是否都是 sense-only 类
            call_names = []
            for call in tool_calls:
                function = call.get("function") or {}
                name = function.get("name") or ""
                call_names.append(name)
            has_action = any(n not in SENSE_ONLY_TOOLS for n in call_names)
            if has_action:
                sense_only_streak = 0
            else:
                sense_only_streak += 1
                if sense_only_streak >= MAX_SENSE_ONLY_ROUNDS:
                    # 连续 N 轮 instrument-only，强制注入"必须 rest"提示
                    warning = (
                        f"⚠️ 你已连续 {MAX_SENSE_ONLY_ROUNDS} 轮只调用了观测/记录类工具，没有任何实质动作。"
                        "你正在原地打转消耗精力。**现在必须**：(1) 调 rest() 休息；"
                        "或 (2) 调一个真正有副作用的工具（execute_code / terminal / "
                        "express_to_human / task / todo 等）。"
                        "**不要再调 sense_*/record_thought**。"
                    )
                    messages.append({"role": "user", "content": warning})
                    self._append_message(session_id, "user", warning, chat_id=getattr(self, "_current_event_chat_id", "") or "")
                    self._write_log(messages)
                    sense_only_streak = 0  # 重置计数；下一轮如果还是 sense-only → 直接停

            session_blocked = False
            for call in tool_calls:
                function = call.get("function") or {}
                name = function.get("name") or ""
                arguments = self._parse_arguments(function.get("arguments"))
                result = registry.dispatch(name, arguments, session_id=session_id)
                tool_calls_seen.append({"name": name, "arguments": arguments, "result": result})
                messages.append({"role": "tool", "tool_call_id": call.get("id"), "name": name, "content": result})
                self._append_message(session_id, "tool", result, tool_name=name, tool_call_id=call.get("id"))
                self._write_log(messages)
                # rest() returns __l4_block__ — stop the loop immediately
                if '"__l4_block__": true' in result or '"__l4_block__":true' in result:
                    session_blocked = True
            if session_blocked:
                # rest-boundary 消息保留：rest() 完成后检查内存池里是否有未在本 wake
                # 注入过的新事件。若有 → 撤销 rest 副作用（回滚 affair → RUNNING、清
                # WaitIntent、cancel 本次新建的闹钟），让 for 循环进入下一次 _chat；
                # 下一次 _chat 开头的 _inject_signalled_events 会把事件注入给模型，
                # 模型重新决定是否真的休息。
                #
                # 历史 bug（wake-469 案例 #256）：消息恰好在 rest()→l4_block→return
                # 这个窗口内到达，注入内存池但当前 wake 直接 return 不再 _chat，事件
                # 只能等下一个 wake 才被捡到（延迟数分钟到数小时）；若进程崩溃则永久
                # 丢失（DB 已 consumed、内存池蒸发）。详见 events.py:_inject_to_running_session
                # + 本函数 _inject_signalled_events。
                if self._has_uninjected_signalled_events():
                    rest_result = next(
                        (r for r in tool_calls_seen if '"__l4_block__"' in (r.get("result") or "")),
                        None,
                    )
                    if rest_result and self._revoke_rest_and_resume(rest_result):
                        uninjected = [
                            e for e in self._peek_signalled()
                            if e.get("event_id") not in self._injected_signal_event_ids
                        ]
                        logger.info(
                            "agent: rest revoked — %d uninjected event(s) pending; "
                            "resuming loop instead of blocking",
                            len(uninjected),
                        )
                        # 不 return —— for 循环自然进入下一次 _chat，
                        # _inject_signalled_events 会把事件注入给模型
                        continue
                final = content or "已进入休息。"
                return {"final_response": final, "tool_calls": tool_calls_seen, "status": "blocked"}
        final = "达到最大迭代次数，已停止本轮执行。"
        self._append_message(session_id, "assistant", final)
        messages.append({"role": "assistant", "content": final})
        self._write_log(messages)
        return {"final_response": final, "tool_calls": tool_calls_seen, "status": "blocked"}

    def _strip_old_reasoning(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """摘掉超出 max_rounds 的旧 assistant 消息的 reasoning_content。

        设计：每条 assistant msg 自带 reasoning_content（append 时由 GLM 原生返回直接写入），
        顺序天然有序。这里只做一件事——保留最后 N 条（N = provider.reuse_max_rounds）的
        reasoning_content，更早的 strip 掉 key——相当于"模型已经忘了 N 轮前的具体推理，
        只记得自己做过什么（tool_calls / content 留着）"。

        这取代了旧的 _reasoning_history + inject_into_messages 方案。
        后者用扁平 list zip 配对 assistant msg 会错位——把别的轮的推理挂到不相干的
        message 上，导致模型以为"上轮说了要做 X 但没做" → 补行动 → 重复发消息（wake-1973）。

        幂等：不修改入参 messages，返回新 list（与旧 inject 返回新 list 行为一致）。
        """
        max_rounds = getattr(self._provider, "reuse_max_rounds", 5)
        if max_rounds <= 0:
            # drop 模式或 reuse_max_rounds=0 → 全部摘掉
            return [
                {k: v for k, v in m.items() if k != "reasoning_content"}
                if m.get("role") == "assistant"
                else m
                for m in messages
            ]

        # 从后往前找含 reasoning_content 的 assistant msg，保留最后 max_rounds 条
        reasoning_indices: list[int] = []
        for i in range(len(messages) - 1, -1, -1):
            m = messages[i]
            if m.get("role") == "assistant" and m.get("reasoning_content"):
                reasoning_indices.append(i)
            if len(reasoning_indices) >= max_rounds:
                break
        keep_set = set(reasoning_indices)

        # 构建新 list：不在 keep_set 的 assistant msg 摘掉 reasoning_content
        new_messages: list[dict[str, Any]] = []
        for i, m in enumerate(messages):
            if m.get("role") == "assistant" and "reasoning_content" in m and i not in keep_set:
                nm = dict(m)
                nm.pop("reasoning_content", None)
                new_messages.append(nm)
            else:
                new_messages.append(m)
        return new_messages

    def _chat(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        self._inject_signalled_events(messages)
        self._inject_entity_recall(messages)
        url = self._chat_url()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload: dict[str, Any] = {
            "model": self.model,
            # 两层压缩串联：
            #   1. _maybe_compress_messages — segment 级叙事化(用 narrative_xxx 替换整段)
            #   2. _compact_old_tool_messages — 同段/跨段真实 tool 行就地压成指针
            # 两者都只改 payload、DB 不动。
            "messages": self._compact_old_tool_messages(
                self._maybe_compress_messages(messages, system_prompt=None, ref_context=None),
            ),
            "tools": registry.get_definitions(set(self._enabled_tool_names()), quiet=True),
        }
        # provider 是模型知识的唯一家园 —— reasoning_effort / tools 格式
        # / extra_body 这些"按家族差异"的字段全部由 provider.customize_payload 决定，
        # agent.py 不识别"现在是 GLM 还是 o1 还是 Claude"。
        # 之前硬编码 `payload["reasoning_effort"] = effort` 被 cargo 推进来——对
        # o1 会 400 Bad Request，对 Claude/DSt 不识别但被静默忽略。
        payload = self._provider.customize_payload(
            payload,
            reasoning_config=self.reasoning_config,
        )
        if not payload["tools"]:
            payload.pop("tools")
        # ── 在 HTTP POST 之前 dump payload —— 这是"模型实际收到的"原始 input ──
        # 关键修正：旧 _dump_llm_input 在 agent.py L170 调用（_chat 之前），
        # 漏掉了 _chat 内部的两步——
        #   1) _inject_signalled_events（mid-session wake_signal 注入）
        #   2) _inject_entity_recall（实体记忆召回注入）
        # 也漏掉了 _maybe_compress_messages / _compact_old_tool_messages 压缩结果，
        # 以及 _provider.customize_payload 加的 reasoning_effort 等模型特定字段。
        # 这些都对模型决策有实质影响——dump 必须在 POST 之前、payload 完整成型后做。
        # 这一份是 ground truth：任何"模型为什么这样做"的问题都能从这里溯因。
        self._dump_llm_payload(payload)

        # timeout 拆分 — 历史 BUG: scalar timeout=300 在所有生命周期(connect/read/pool)
        # 都给 5 分钟,导致一次 GLM 推理卡死会阻塞整个 wake 几十分钟。现拆细:
        # - connect/pool: 5s(GLM TLS 握手本身 50ms,5s 足够发现网络断裂)
        # - write: 10s(payload 上传不会超过几 MB,够用)
        # - read: 180s(GLM reasoning_model 长推理可达数分钟;此前 90s 在长链场景
        #   几乎必触发 read timeout 重试,3 次重试 + 退避≈340s,又正好撞 cron 的
        #   stale-RUNNING 回退阈值,制造"wake 还在跑却被判 stale"的连锁误判。
        #   提到 180s 给长推理足够余量,把"时间太少"这条根因从源头切断。)
        os_env = __import__("os").getenv
        http_timeout = httpx.Timeout(
            connect=float(os_env("DIGITAL_LIFE_API_CONNECT_TIMEOUT", "5")),
            read=float(os_env("DIGITAL_LIFE_API_READ_TIMEOUT", "180")),
            write=float(os_env("DIGITAL_LIFE_API_WRITE_TIMEOUT", "10")),
            pool=float(os_env("DIGITAL_LIFE_API_POOL_TIMEOUT", "5")),
        )
        import time as _time
        last_error = None
        # 网络错误（timeout/conn reset）与 429 限流用两套独立的退避序列：
        # - 网络错误：10s/20s/40s（缩短；GLM 推理超时多半是单次抖动，不需要长退避）
        # - 429：5s/10s/20s，且优先尊重 Retry-After 头。GLM 账号级 QPM 限流是瞬时的，
        #   长退避会让一次正常唤醒变成 60s 起跳；而 429 退避几秒后重试基本就放行。
        # 历史 BUG：429 落到 `except Exception: raise` 立即抛出 → 整轮唤醒判失败 →
        # scheduler 标 BLOCKED "retry in 5 min" → 用户感觉消息发出去后"好久才响应"。
        # 历史 BUG2：网络错误 60s/120s/120s 退避 + read timeout 300s = 一次抖动让 wake
        # 卡 500 秒以上 → 僵尸 wake 占 instance lock，后续 wake 全部 skipped。
        # 历史 BUG3 (alpha #1181/#1182)：read=90s × 3 次重试 + 10/20/40s 退避 ≈ 340s，
        #   与 cron 硬编码的 300s stale 阈值同量级,长推理必然边对边相撞。修法是
        #   ① read 提到 180s(见上) ② 重试上限改 env ③ 本函数加单 call wall-clock 总
        #   预算,结构上保证单次 LLM 调用链在 240s 内自我终结,永远低于 stale 阈值。
        MAX_NET_RETRIES = int(os_env("DIGITAL_LIFE_LLM_MAX_NET_RETRIES", "3"))
        MAX_429_RETRIES = int(os_env("DIGITAL_LIFE_LLM_MAX_429_RETRIES", "3"))
        # 单次 _chat(含全部重试)的 wall-clock 总预算。
        # 默认 240s:远高于单次 read(180s),又远低于 cron stale(1800s)/zombie(600s),
        # 让重试链先于状态机兜底自我终结。GLM 健康 9 成 < 60s 返回,这只拦截灾难性
        # 网络/限流场景,正常推理不受影响。
        LLM_CALL_MAX_DURATION_S = float(os_env("DIGITAL_LIFE_LLM_CALL_MAX_DURATION", "240"))
        net_attempts = 0
        retry_429_attempts = 0
        _call_start = _time.time()
        while True:
            try:
                with httpx.Client(timeout=http_timeout) as client:
                    response = client.post(url, headers=headers, json=payload)
                    response.raise_for_status()
                    data = response.json()
                choice = (data.get("choices") or [{}])[0]
                _msg = choice.get("message") or {}
                # Layer 1（存）：provider 提取 reasoning 文本，挂回 message dict。
                # 下游 _append_message 透传它写入 messages.reasoning 列。
                try:
                    _reasoning = self._provider.extract_reasoning(_msg)
                    if _reasoning:
                        _msg = dict(_msg)
                        _msg["reasoning"] = _reasoning
                except Exception:
                    pass
                return {"message": _msg, "raw": data}
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    # 账号级熔断：收到 429 立即 trip（按 api_key 分区，跨实例共享）。
                    # 用本次响应的 Retry-After 解析熔断时长。trip 的 upsert 保护保证
                    # 只有更长的退避才覆盖，所以即使每次重试都 trip 也不会错误缩短恢复时间。
                    # 这让同 key 的其它实例子进程（cron & _wake_or_inject）下次能读到熔断
                    # 状态而停止打 API，把账号级限流从"每实例各自撞墙"变成"一起暂停"。
                    self._trip_circuit_breaker(e.response)
                    # 记一笔 429 时序点（token 不计，前端按 kind=llm_call_429 计次数）。
                    # 每次撞墙都记（连同重试），反映真实限流压力。失败吞掉不阻断重试链。
                    try:
                        from infrastructure.budget.token_tracker import get_token_tracker
                        from infrastructure.config import get_app_instance_id
                        get_token_tracker().record(
                            instance_id=get_app_instance_id() or "",
                            input_tokens=0, output_tokens=0, total_tokens=0,
                            session_id=self.session_id or "",
                            kind="llm_call_429",
                        )
                    except Exception:
                        pass
                    if retry_429_attempts < MAX_429_RETRIES:
                        retry_429_attempts += 1
                        # 优先尊重 Retry-After 头（秒）。GLM 实际不一定带，给默认值兜底。
                        delay = 5 * (2 ** (retry_429_attempts - 1))  # 5 / 10 / 20
                        ra = e.response.headers.get("Retry-After") or e.response.headers.get("retry-after")
                        if ra:
                            try:
                                delay = max(2, min(float(ra), 30.0))
                            except ValueError:
                                pass  # 也可能是 HTTP date 格式，忽略走默认退避
                        # Wall-clock 兜底:总预算耗尽就不再 sleep 重试,直接抛出,
                        # 让 scheduler 的事件级退避(delay_pending_events)接管 — 避免
                        # 单次 wake 卡在 LLM 重试链里逼近 cron stale 阈值。
                        elapsed = _time.time() - _call_start
                        if elapsed + delay > LLM_CALL_MAX_DURATION_S:
                            logger.warning(
                                "LLM API 429 wall-clock budget exhausted "
                                "(elapsed=%.1fs + delay=%.1fs > %.0fs, net=%d/429=%d) — raising",
                                elapsed, delay, LLM_CALL_MAX_DURATION_S,
                                net_attempts, retry_429_attempts,
                            )
                            raise
                        logger.warning(
                            "LLM API 429 Too Many Requests (try %d/%d), backing off %.1fs",
                            retry_429_attempts, MAX_429_RETRIES, delay,
                        )
                        _time.sleep(delay)
                        continue
                    logger.warning(
                        "LLM API 429 retry budget exhausted (%d/%d) — raising",
                        retry_429_attempts, MAX_429_RETRIES,
                    )
                # 非 429 的 HTTPStatusError（5xx / 4xx）以前直接抛出，保持原行为。
                raise
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
                last_error = e
                net_attempts += 1
                if net_attempts < MAX_NET_RETRIES:
                    delay = 10 * (2 ** (net_attempts - 1))  # 10 / 20 / 40
                    # Wall-clock 兜底,同 429 分支语义。
                    elapsed = _time.time() - _call_start
                    if elapsed + delay > LLM_CALL_MAX_DURATION_S:
                        logger.warning(
                            "LLM API network error wall-clock budget exhausted "
                            "(elapsed=%.1fs + delay=%.1fs > %.0fs, net=%d) — raising: %s",
                            elapsed, delay, LLM_CALL_MAX_DURATION_S,
                            net_attempts, e,
                        )
                        raise last_error  # type: ignore[misc]
                    logger.warning(
                        "LLM API network error (attempt %d/%d), retrying in %ds: %s",
                        net_attempts, MAX_NET_RETRIES, delay, e,
                    )
                    _time.sleep(delay)
                    continue
                raise last_error  # type: ignore[misc]
        # unreachable

    def _trip_circuit_breaker(self, response: "httpx.Response") -> None:
        """收到 429 时触发账号级熔断（按 api_key 分区，跨实例共享）。

        从 429 响应头读 Retry-After 并按 resolve_retry_after 解析出熔断时长，
        写入共享 circuit_breaker.db（WAL，跨进程可见）。同 key 的其它实例
        下次在 cron / _wake_or_inject / _chat 入口读到熔断即停止打 API。

        自身故障必须 swallow——熔断是保护机制，不能让它的 DB 写失败把一次
        正常的 429 重试链改成抛异常（同 _record_token_usage 的 swallow 策略）。
        """
        try:
            from infrastructure.budget.circuit_breaker import trip, resolve_retry_after
            ra = response.headers.get("Retry-After") or response.headers.get("retry-after")
            trip(
                self.api_key or "",
                retry_after_sec=resolve_retry_after(ra),
                instance_id=self.instance_id or "",
            )
        except Exception as exc:
            logger.debug("circuit breaker trip failed: %s", exc)

    def _record_token_usage(self, raw_response: dict[str, Any] | None) -> None:
        """从 LLM API 返回的 usage 字段累加 token 用量。

        - self.session_input_tokens / session_output_tokens：本 session 累计
          （结束后由 scheduler 读走写回 sessions.input_tokens/output_tokens）
        - 顺手 record 到 TokenUsageTracker —— 预算闸门和前端展示都依赖它

        历史背景：response.usage 一直被 `_chat` 调用方丢弃，导致
        sessions 表的 token 列长期为 NULL，预算闸门和精力-token 耦合无从
        落地。这里接线回来，保留兼容性（usage 缺失只 debug 日志，不抛错）。
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
        self.session_input_tokens += in_t
        self.session_output_tokens += out_t
        # 写入持久化预算追踪器（不抛错——记录失败不能阻断 LLM 流程）
        try:
            from infrastructure.config import get_app_instance_id
            from infrastructure.budget import get_token_tracker
            iid = get_app_instance_id() or ""
            get_token_tracker().record(
                instance_id=iid,
                input_tokens=in_t,
                output_tokens=out_t,
                session_id=self.session_id or "",
                kind="llm_call",
            )
        except Exception as exc:
            logger.debug("record token usage failed: %s", exc)

        # 精力-token 耦合（设计文档 15.4）：LLM call 按真实 token usage 折算消耗，
        # 不走固定 ENERGY_COST_PER_CALL。1k input = 0.05 精力；1k output = 0.5 精力。
        # 模型自身的工作成本（terminal / sense / todo 等）独立扣，互不影响。
        try:
            from domain.vital.simulation.engine import (
                ENERGY_PER_KTOKEN_INPUT, ENERGY_PER_KTOKEN_OUTPUT,
            )
            amount = (in_t / 1000.0) * ENERGY_PER_KTOKEN_INPUT + \
                     (out_t / 1000.0) * ENERGY_PER_KTOKEN_OUTPUT
            if amount > 0:
                from domain.vital.state import consume_energy
                consume_energy(amount, reason="llm_call")
        except Exception as exc:
            logger.debug("consume_energy for token usage failed: %s", exc)

    def _chat_url(self) -> str:
        base = (self.base_url or "https://api.openai.com/v1").rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def _can_call_llm(self) -> bool:
        return bool(self.model and (self.base_url or self.provider == "openai"))

    def _convert_user_to_tool(self, messages: list[dict]) -> list[dict]:
        """Convert role:user messages in conversation_history to assistant tool_call → tool result pairs.

        The action_prompt is appended separately as role:user after this conversion,
        so all user messages here are system context that should become tool results.
        """
        if not messages:
            return messages

        result: list[dict] = []
        # R1 重构：slow_ctx 不再持久化到 messages 表。
        # 原因：
        #   1) messages 表语义 = "模型真实对话历史"（user action / assistant output / tool call）
        #   2) slow_ctx 是"每次 wake 临时拼接的背景"，每次内容可能不同
        #   3) 持久化导致：续用 wake 时 history 里有上轮 slow_ctx 污染 thinking 记忆
        #      token 膨胀、debug 看不清真实 turn flow
        # slow_ctx: fake tool calls injected as user-role messages by the
        # scheduler. The result list goes into the in-memory prompt; we don't
        # persist these old-style — the audit ctx records them via slow_ctx().
        slow_ctx_kinds = {"system_context", "session_digest", "consciousness", "task_board", "social_context", "task_skill", "my_context", "chat_stream", "schedule"}
        for m in messages:
            role = m.get("role")
            if role != "user":
                result.append(m)
                continue
            tool_name = m.get("_sys_tool")
            if not tool_name:
                result.append(m)
                continue
            content = m.get("content") or ""
            assistant_msg, tool_msg = self._sys_tool_call(tool_name, content)

            # 写新 audit DB（slow_ctx_Kinds 的注入）；老 session_injections 已废。
            if self.audit_ctx is not None and tool_name in slow_ctx_kinds:
                try:
                    self.audit_ctx.slow_ctx(tool_name, content)
                except Exception:
                    logger.debug("Failed to dual-write slow_ctx to audit DB", exc_info=True)
            result.append(assistant_msg)
            result.append(tool_msg)

        return result

    def _sys_tool_call(self, name: str, content: str) -> tuple[dict, dict]:
        """Generate a fake assistant tool_call + tool result pair for system context."""
        self._sys_tool_counter += 1
        tid = f"sys_{self._sys_tool_counter:03d}"
        return (
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": tid,
                    "type": "function",
                    "function": {"name": name, "arguments": "{}"},
                }],
            },
            {
                "role": "tool",
                "tool_call_id": tid,
                "name": name,
                "content": content,
            },
        )

    def _enabled_tool_names(self) -> list[str]:
        all_names = registry.get_all_tool_names()
        if not self.enabled_toolsets:
            return all_names
        selected: list[str] = []
        toolsets = set(self.enabled_toolsets)
        for name in all_names:
            if registry.get_toolset_for_tool(name) in toolsets or name in toolsets:
                selected.append(name)
        return selected

    def _ensure_tools_loaded(self) -> None:
        for module_name in (
            "interfaces.tools.sense_tools",
            "interfaces.tools.action_tools",
            "interfaces.tools.skills_tool",
            "interfaces.tools.capability_tools",
            "interfaces.tools.vision_tool",
            "domain.todos.tools",
            "domain.project.tools",
            "interfaces.tools.terminal_tool",
            "interfaces.tools.code_execution_tool",
        ):
            try:
                __import__(module_name)
            except Exception:
                logger.warning("Tool module load failed: %s", module_name, exc_info=True)

    def _append_message(self, session_id: str, role: str, content: str | None, **kwargs: Any) -> None:
        if not self.session_db:
            return
        # 自动注入当前事件 chat_id（wake 时由 scheduler 设置 ContextVar）
        # messages 表用此列做按 chat 检索 + prompt metadata 标注
        if "chat_id" not in kwargs or not kwargs.get("chat_id"):
            try:
                from domain.lifecycle.runtime_context import get_current_event_chat_id
                kwargs["chat_id"] = get_current_event_chat_id() or ""
            except Exception:
                kwargs.setdefault("chat_id", "")
        try:
            self.session_db.append_message(session_id, role, content=content, **kwargs)
        except Exception:
            logger.debug("Failed to append session message", exc_info=True)

        # Dual-write to new audit DB if a WakeContext is attached.
        if self.audit_ctx is not None:
            try:
                self._audit_write_turn(role, content, **kwargs)
            except Exception:
                logger.debug("Failed to dual-write turn to audit DB", exc_info=True)

    def _audit_write_turn(self, role: str, content: str | None, **kwargs: Any) -> None:
        """Mirror a turn into WakeContext.

        ``llm_call_seq`` boundaries are derived locally: after an assistant
        message with tool_calls has had all its results written, the next
        assistant message marks the next call. We track that with
        ``_audit_pending_tool_count``: set when an assistant's tool_calls is
        seen, decremented on each tool result.
        """
        ctx = self.audit_ctx
        chat_id = kwargs.get("chat_id") or None
        if role == "system":
            # system prompt captured in wake meta, not as a turn.
            return
        if role == "user":
            ctx.action(content or "", chat_id=chat_id)
            return
        if role == "assistant":
            tool_calls = kwargs.get("tool_calls")
            # If a previous assistant's tool calls are still pending, that's a
            # bookkeeping error in the agent loop (shouldn't happen given the
            # strictly sequential dispatch). Defensive: clear state on new assistant.
            tc_list = list(tool_calls) if tool_calls else []
            ctx.assistant(
                content=content,
                tool_calls=tc_list or None,
                reasoning=kwargs.get("reasoning"),
                finish_reason=kwargs.get("finish_reason"),
            )
            # Track pending tool result count for this call.
            self._audit_pending_tool_count = len(tc_list)
            self._audit_assistant_had_calls = bool(tc_list)
            return
        if role == "tool":
            ctx.tool_result(
                tool_name=kwargs.get("tool_name") or "",
                tool_call_id=kwargs.get("tool_call_id") or "",
                content=content or "",
                error=kwargs.get("error"),
            )
            # When all tool results for this assistant are in, advance to next call.
            remaining = max(int(getattr(self, "_audit_pending_tool_count", 0)) - 1, 0)
            self._audit_pending_tool_count = remaining
            if remaining == 0 and getattr(self, "_audit_assistant_had_calls", False):
                ctx.next_call()
                self._audit_assistant_had_calls = False

    def _dump_llm_payload(self, payload: dict[str, Any]) -> None:
        """真实发给 LLM 的字面 payload，在 HTTP POST 之前写一份完整 JSON。

        区别于 _dump_llm_input（调用在 _chat 之前、缺 signalled_events/entity_recall
        注入、缺 provider custom payload）—— 这一份是 **HTTP 字节流的 JSON 包装**。
        任何关于"模型为什么这样做"的问题都能从这份 dump 完整溯因——无需考古重建。

        保留策略：12 小时（半天），覆盖"出问题后再回看"的常态窗口。
        半天 = ~10-15 wake × 每个 wake 数十 LLM call = 数千 dump 文件，单文件 ~200KB，
        总量估算每实例 50-200MB（按 messages 长度浮动）。3 实例 ~ 600MB max，可接受。

        文件位置：apps/<id>/data/llm_payload_dumps/<session_id>__call_<n>__<unix_ms>.json
        """
        try:
            import datetime as _dt
            import time as _t
            call_seq = self._call_seq - 1  # 注意：_call_seq 在 _dump_llm_input 已自增过；这里复用同一序号
            wake_id = getattr(self, "wake_id", None)
            dump = {
                "session_id": self.session_id or "(adhoc)",
                "wake_id": wake_id,
                "call_seq": call_seq,
                "ts_unix_ms": int(_t.time() * 1000),
                "timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds"),
                "url": self._chat_url(),
                "model": payload.get("model"),
                "payload": payload,  # 含 messages / tools / reasoning_config 等模型真实看的所有字段
            }
            # 多套一个子目录避免和老的 sessions_dumps/ 混在一起
            dump_dir = self._dumps_dir.parent / "llm_payload_dumps"
            dump_dir.mkdir(parents=True, exist_ok=True)
            file_path = dump_dir / (
                f"{self.session_id or 'adhoc'}__call_{call_seq}__{dump['ts_unix_ms']}.json"
            )
            file_path.write_text(
                json.dumps(dump, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            # 顺手扫过期
            self._purge_old_payload_dumps(dump_dir, max_hours=12)
        except Exception:
            logger.debug("Failed to dump LLM payload", exc_info=True)

    def _purge_old_payload_dumps(self, dump_dir: "Path", max_hours: int = 12) -> None:
        """清理 llm_payload_dumps/ 超过 max_hours 小时的 JSON 文件。

        按文件 mtime 清理；保留 12 小时窗口（覆盖常见 debug/调试反馈时间）。
        """
        import time as _t
        from pathlib import Path as _P
        try:
            threshold = _t.time() - max_hours * 3600
            for p in _P(dump_dir).glob("*.json"):
                try:
                    if p.stat().st_mtime < threshold:
                        p.unlink()
                except OSError:
                    continue
        except Exception:
            # GC 失败绝不影响 LLM call（这是诊断工具，不是核心路径）
            pass

    def _dump_llm_input(self, messages: list[dict[str, Any]]) -> None:
        """每次 LLM 调用前保存字面 messages + 调用元数据到 JSON。

        目标：让"模型看到了什么"跟"人类查的"完全一致——不再走前端渲染
        或 sessions.db mirror（两者都可能跟实际有 render-id 对名字等差异）。

        文件位置：apps/<id>/data/sessions_dumps/<session_id>__call_<n>.json
        文件内容（不可变格式）：
          {
            "session_id": "...",
            "wake_id": int,                # 由 scheduler 注入到 agent
            "model": "glm-5.2",
            "call_seq": 2,                # 本 session 第几次 LLM call
            "timestamp": "...",
            "messages": [...]             # 字面 LLM input（含 reasoning 注入）
          }

        清理策略：含 2 天内的 dump（最近 ~48h），更老的自动删除。每次写新文件
        时顺手扫过期，不依赖外部 cron。
        """
        try:
            import datetime as _dt
            call_seq = self._call_seq
            self._call_seq += 1
            wake_id = getattr(self, "wake_id", None)
            dump = {
                "session_id": self.session_id or "(adhoc)",
                "wake_id": wake_id,
                "model": self.model,
                "call_seq": call_seq,
                "timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
                "messages": messages,
            }
            file_path = self._dumps_dir / f"{self.session_id or 'adhoc'}__call_{call_seq}.json"
            file_path.write_text(
                json.dumps(dump, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            # 顺手扫过期：清理 2 天前的 dump 文件
            self._purge_old_dumps(max_days=1)  # 保留 1 天；旧的"前注入"快照，作为对比备份
        except Exception:
            logger.debug("Failed to dump LLM input", exc_info=True)

    def _purge_old_dumps(self, max_days: int = 2) -> None:
        """删除 sessions_dumps/ 里超过 max_days 天的 JSON 文件。

        语义对齐「保留 2 天内数据」：超过 2 天的（前天及更早）自动删除。
        机制：检查文件 mtime，差值 > max_days*86400 秒就删。
        trigger：每次 _dump_llm_input 写新文件时调用——频率足够，避免外部 cron 依赖。
        """
        try:
            import time as _time
            threshold = _time.time() - max_days * 86400
            count = 0
            for f in self._dumps_dir.glob("*.json"):
                try:
                    if f.stat().st_mtime < threshold:
                        f.unlink()
                        count += 1
                except Exception:
                    continue
            if count > 0:
                logger.debug("Purged %d old dump files (>%d days)", count, max_days)
        except Exception:
            pass

    def _write_log(self, messages: list[dict[str, Any]]) -> None:
        """写 session JSON 日志。

        JSON 文件是 messages 表的镜像快照 — 单一真实源是 DB。
        每次写时先从 DB 重新拉本 session 的所有 messages（按时间顺序），
        再写入 JSON。这样：
        - DB 的 replace_sys_tool_messages DELETE 操作能正确反映到 JSON
          （不会出现 DELETE 后 JSON 还残留旧条目）
        - 不再依赖历史快照 merge（_log_base_messages 路径）
        """
        try:
            db_messages: list[dict[str, Any]] = []
            if self.session_db and self.session_id:
                try:
                    rows = self.session_db.get_messages(self.session_id)
                    for r in rows:
                        role = r.get("role") or "user"
                        m: dict[str, Any] = {"role": role}
                        if r.get("content") is not None:
                            m["content"] = r["content"]
                        if r.get("tool_name"):
                            m["name"] = r["tool_name"]
                        if r.get("tool_calls"):
                            tc = r["tool_calls"]
                            if isinstance(tc, str):
                                try:
                                    import json as _j
                                    tc = _j.loads(tc)
                                except Exception:
                                    tc = []
                            m["tool_calls"] = tc
                        if r.get("tool_call_id"):
                            m["tool_call_id"] = r["tool_call_id"]
                        # 镜像 reasoning(GLM reasoning_content)到 session JSON,
                        # 让前端会话视图能渲染模型"内心独白"、人审时看清思路连续性。
                        if r.get("reasoning"):
                            m["reasoning"] = r["reasoning"]
                        m["timestamp"] = r.get("timestamp")
                        db_messages.append(m)
                except Exception:
                    pass
            payload = db_messages or messages
            self.session_log_file.write_text(
                json.dumps({"messages": payload}, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception:
            logger.debug("Failed to write session log", exc_info=True)

    @staticmethod
    def _parse_arguments(raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict):
            return raw
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _compact_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Legacy: just remove None values. Real compression is _maybe_compress_messages."""
        compacted: list[dict[str, Any]] = []
        for message in messages:
            item = {key: value for key, value in message.items() if value is not None}
            compacted.append(item)
        return compacted

    def _maybe_compress_messages(
        self,
        messages: list[dict[str, Any]],
        system_prompt: str | None = None,
        ref_context: str | None = None,
    ) -> list[dict[str, Any]]:
        """检查 token 数，超阈值则用叙事替换历史段。

        当前段永远不压缩，只对历史段做叙事替换。
        """
        # Token 估算：中文为主时 ~1.5-1.6 chars/token，英文 ~4 chars/token。
        # 旧的 total_chars/3 严重高估所需 token 数（中文被当成英文算），
        # 导致实际已经超 context 但 estimated_tokens 仍低于阈值 → 压缩不触发。
        # 改用 /1.8：中文混合场景下偏保守，确保该压缩时能真正触发。
        total_chars = sum(len(str(m.get("content") or "")) for m in messages)
        if system_prompt:
            total_chars += len(system_prompt)
        if ref_context:
            total_chars += len(ref_context)

        estimated_tokens = int(total_chars / 1.8)

        # 从配置读取阈值（默认 60% 的 128K context = 76K）
        threshold = self._get_compression_threshold()
        if estimated_tokens < threshold:
            return self._compact_messages(messages)

        # 分割段（user 消息是段起始 marker）
        segments = self._split_by_user_message(messages)
        if len(segments) <= 1:
            return self._compact_messages(messages)

        # 当前段不压缩，只压缩历史段
        historical_segments = segments[:-1]
        current_segment = segments[-1]

        compressed: list[dict[str, Any]] = []
        current_tokens = sum(len(str(m.get("content") or "")) for m in messages) - sum(
            len(str(m.get("content") or "")) for m in current_segment
        )

        # 从最旧的段开始逐个替换
        for i, seg in enumerate(historical_segments):
            seg_tokens = sum(len(str(m.get("content") or "")) for m in seg)

            # 尝试加载叙事
            narrative = self._load_narrative_for_segment(seg)
            if narrative:
                narrative_tokens = int(len(narrative) / 1.8)
                if current_tokens - seg_tokens + narrative_tokens < threshold:
                    # 替换后满足阈值，追加叙事 + 剩余段
                    self._append_narrative_to_messages(compressed, narrative, seg, segment_index=i)
                    current_tokens = current_tokens - seg_tokens + narrative_tokens
                    compressed.extend(current_segment)
                    return self._compact_messages(compressed)
                else:
                    # 替换后仍超阈值，继续替换更早的段
                    self._append_narrative_to_messages(compressed, narrative, seg, segment_index=i)
                    current_tokens = current_tokens - seg_tokens + narrative_tokens
            else:
                # 无叙事，降级处理：只保留段首尾消息
                compressed.extend(self._shrink_segment(seg))
                current_tokens = sum(len(str(m.get("content") or "")) for m in compressed)

        # 所有旧段都处理过了，追加当前段
        compressed.extend(current_segment)

        # 仍超阈值 → 强制保留最近 1 段（当前段）
        return self._compact_messages(compressed[-50:])

    def _get_compression_threshold(self) -> int:
        """从配置读取压缩阈值，默认 76K tokens。"""
        try:
            import os
            threshold = int(os.environ.get("COMPRESSION_TOKEN_THRESHOLD", "76800"))
            return threshold
        except Exception:
            return 76800

    # ── tool 上下文压缩（与 _maybe_compress_messages 互补的另一层）──────────
    # _maybe_compress_messages 处理「整 segment 范围」的叙事化（用 narrative_xxx
    # 把历史段替换成摘要），但它对「同 wake 内 ReAct loop N 轮 tool 消息累积」
    # 的覆盖很弱——长单段 session（如 tx_initiative_0705_1143，80+ 轮把上下文从
    # 21K 推到 101K）仍然会膨胀。
    # 本层在 _maybe_compress_messages 之后跑，专门处理「真实 tool 消息」的就地
    # 压缩：把 >depth 轮以前、且 >min_chars 的 tool 行 content 替换为指针，DB
    # 不动（recall_tool_result 工具查 DB 拿回原文）。

    # 真实 LLM tool_call_id 白名单（厂商格式正则）——与 _is_fake 标记双门校验。
    # 当前覆盖（DB 实测）：
    #   - call_-xxx       GLM / StepFun（OpenAI 兼容格式）
    #   - chatcmpl-xxx    OpenAI 原生 response format
    # 接入新厂商时若其 tool_call_id 不匹配任一正则，真实调用会被误判为 fake 而
    # 免疫压缩——加新正则到此即可。
    _REAL_TOOL_ID_PATTERNS = (
        re.compile(r"^call_"),
        re.compile(r"^chatcmpl-"),
    )

    def _is_real_tool_call(self, m: dict[str, Any]) -> bool:
        """判定一行 tool message 是否为真实 LLM 工具调用。

        双门与逻辑（两者都满足才为真实，任一不满足即视为 fake 免疫）：
          1. tool_call_id 命中白名单正则（已知 LLM 厂商格式）
          2. 没有显式 _is_fake=True 标记（assembly 审计侧/段叙事会打标）

        为什么是与门：
          - 仅用白名单：老 fake 注入若沿用旧前缀仍能免疫，但不防"未来不小心造
            了新前缀"的情况；_is_fake 兜底加强。
          - 仅用 _is_fake：DB 读回的 messages（_load_prior_messages）不一定
            带这个 marker（schema 没存），白名单兜底识别为真实。
        """
        if m.get("_is_fake") is True:
            return False
        tid = str(m.get("tool_call_id") or "")
        if not tid:
            return False
        return any(p.match(tid) for p in self._REAL_TOOL_ID_PATTERNS)

    def _get_tool_history_depth(self) -> int:
        """最近 N 轮真实 tool 消息不入压缩，给活跃窗口。默认 8。"""
        try:
            import os
            return max(0, int(os.environ.get("DIGITAL_LIFE_TOOL_HISTORY_DEPTH", "8")))
        except Exception:
            return 8

    def _get_tool_compact_min_chars(self) -> int:
        """content 字符数低于此值的 tool 行不压（短结果压成指针反而变大）。默认 100。"""
        try:
            import os
            return max(0, int(os.environ.get("DIGITAL_LIFE_TOOL_COMPACT_MIN_CHARS", "100")))
        except Exception:
            return 100

    def _compact_old_tool_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """就地压缩 payload 里的旧 tool 消息——只改 content，不动 DB。

        条件（AND）：真实 tool 行 且 > depth 轮之前 且 len(content) > min_chars。
        fake（sys_/narrative_/fake_ 前缀）一律跳过。

        返回新 list（不修改入参，与 _maybe_compress_messages 范式一致）。
        """
        depth = self._get_tool_history_depth()
        min_chars = self._get_tool_compact_min_chars()

        # 从后往前数真实 tool 行，定位"最近 depth 个真实 tool 行"之前的那些行
        real_tool_indices: list[int] = []
        for idx, m in enumerate(messages):
            if m.get("role") != "tool":
                continue
            if not self._is_real_tool_call(m):
                # fake / 未识别格式 → 不计入 depth、不参与压缩
                continue
            real_tool_indices.append(idx)

        # depth=0 表示无活跃窗口，全部候选；depth>0 时最近 depth 条不压
        if depth > 0 and len(real_tool_indices) <= depth:
            return list(messages)
        candidates_to_compress = set(real_tool_indices[:-depth]) if depth > 0 else set(real_tool_indices)

        if not candidates_to_compress:
            return list(messages)

        result: list[dict[str, Any]] = []
        for idx, m in enumerate(messages):
            if idx not in candidates_to_compress or m.get("role") != "tool":
                result.append(m)
                continue
            content = str(m.get("content") or "")
            if len(content) <= min_chars:
                # 短结果保留——压成 ~150 字符指针反而扩大上下文
                result.append(m)
                continue
            tid = str(m.get("tool_call_id") or "")
            name = str(m.get("name") or "")
            result.append({
                "role": "tool",
                "tool_call_id": tid,
                "name": name,
                "content": self._render_tool_pointer(m, tid, name),
            })
        return result

    @staticmethod
    def _render_tool_pointer(m: dict[str, Any], tid: str, name: str) -> str:
        """生成"已压缩"指针文本——LLM 可直读，并指引它调 recall_tool_result。

        精简设计（每条 ~70-110 chars，含 preview；旧版 182-196 chars）：
        - 单行紧凑格式 ``{CMP} name=X id=Y pv="…" → recall_tool_result(ID)``
        - preview 固定保留 ``content[:40]``：实战发现不同 tool 的头部都有用 ——
          ``terminal`` 是 zsh 错误或命令输出，``recall_entity`` 是 entity 名+summary，
          ``express_to_human`` 是 sent 状态。短到 40 chars 不显著拖 token，但让模型
          能不调 recall 就粗判"这条工具结果的语义"，避免老去拉原文。
        - tid 仅出现一次（旧版末尾 recall 重复了一次 tool_call_id）。
        - 全 ASCII + 单行、无中文前缀。

        历史：旧版 ``[旧工具结果已压缩] name=X id=Y preview="…" (60c)\n→ recall_tool_result(tool_call_id="Y")`` 约 190 chars。
        精简后 ~70-110 chars，省 ~45%。
        实测 wake 1752 跨段 85 条指针省 ~9K chars ≈ 4-5K token。
        """
        # preview: 取 content 头 40 chars, 去 newline 收紧
        raw = str(m.get("content") or "")
        preview = raw[:40].replace("\n", " ").strip()

        return f"{{CMP}} name={name} id={tid} pv={preview!r} → recall_tool_result({tid})"

    def _split_by_user_message(self, messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        """按 user 消息切分段。

        只有「真正的 user 消息」才是段（= wake）起始 marker。带 ``_sys_tool``
        tag 的 user 消息是 slow_ctx 注入项（task_board / chat_stream / digest /
        consciousness 等），它们随每次 wake 重新注入、不是新对话回合，若当成
        段起始会导致 segment 切分碎片化、与「segment_index = wake 序号」的
        语义脱钩（见 ``session_db.append_message`` 的 segment_index 自增规则）。
        因此这类注入项归入当前段、不触发新段。
        """
        segments: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []

        for m in messages:
            if m.get("role") == "user" and not m.get("_sys_tool"):
                if current:
                    segments.append(current)
                current = [m]
            else:
                current.append(m)

        if current:
            segments.append(current)
        return segments

    def _load_narrative_for_segment(self, segment: list[dict[str, Any]]) -> str | None:
        """从段中提取 session_id 和 segment_index，加载叙事。"""
        from domain.memory.memory.summaries.consolidation_runtime import (
            load_segment_narrative,
            _lazy_generate_segment_narrative,
        )

        # 尝试从段中提取 session_id 和 segment_index
        session_id = None
        segment_index = None

        # 从 assistant 消息的 tool_calls 中找 session_id
        for m in segment:
            if m.get("tool_calls"):
                try:
                    calls = m["tool_calls"] if isinstance(m["tool_calls"], list) else json.loads(m["tool_calls"] or "[]")
                    for call in calls:
                        args_str = call.get("function", {}).get("arguments", "{}")
                        args = json.loads(args_str) if isinstance(args_str, str) else args_str
                        if "session_id" in args:
                            session_id = args["session_id"]
                            break
                except Exception:
                    pass

        # session_id 还没找到时，用 agent 当前 session（continuation 场景同 session 复用）
        if not session_id and getattr(self, "session_id", None):
            session_id = self.session_id

        if not session_id:
            return None

        # segment_index 优先从 message 自带的持久化字段读取（_load_prior_messages
        # 经 get_messages 的 SELECT * 已带 segment_index 列，语义 = wake 序号，
        # 见 session_db.append_message）。这比旧的「count - user 数 - 1」反推稳：
        # 后者在 prev_history 被 slow_ctx 注入的 user 消息污染（_sys_tool）时算错。
        for m in segment:
            si = m.get("segment_index")
            if isinstance(si, int) and si >= 0:
                segment_index = si
                break

        # 字段缺失（旧库 / 非 DB 来源的 segment）→ 回退到 session_db 反推
        if segment_index is None and self.session_db:
            try:
                count = self.session_db.get_segment_count(session_id)
                user_count = sum(1 for m in segment if m.get("role") == "user")
                segment_index = max(0, count - user_count - 1)
            except Exception:
                pass

        if not session_id or segment_index is None:
            return None

        # 先尝试加载已有叙事
        narrative = load_segment_narrative(session_id, segment_index)
        if narrative:
            return narrative

        # 惰性生成
        if self.session_db:
            try:
                narrative = _lazy_generate_segment_narrative(self.session_db, session_id, segment_index)
                return narrative
            except Exception as e:
                logger.debug("Lazy segment narrative generation failed for %s#%d: %s",
                             session_id[:20], segment_index, e)

        return None

    def _append_narrative_to_messages(
        self,
        target: list[dict[str, Any]],
        narrative: str,
        original_segment: list[dict[str, Any]],
        *,
        segment_index: int = 0,
    ) -> None:
        """将叙事作为 fake tool call 追加到 messages。"""
        # 提取段的时间信息
        time_range = self._extract_time_range(original_segment)

        content = f"> [回顾 · 非新事件] {time_range}\n\n{narrative}"

        # tool_call id 用 segment_index 做种子（确定性约束见 docs/architecture/
        # llm-input-assembly.md）——避免运行期 _sys_tool_counter 计数随唤醒顺序漂移，
        # 让 run_conversation 跟 assemble_llm_input 回溯产出同一个 tool_call_id。
        tid = f"narrative_{segment_index:03d}"

        target.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": tid,
                "type": "function",
                "function": {"name": "segment_narrative", "arguments": "{}"},
            }],
        })
        target.append({
            "role": "tool",
            "tool_call_id": tid,
            "name": "segment_narrative",
            "content": content,
        })

    def _extract_time_range(self, segment: list[dict[str, Any]]) -> str:
        """从段中提取时间范围。"""
        import time
        timestamps = []
        for m in segment:
            ts = m.get("timestamp")
            if ts:
                try:
                    timestamps.append(float(ts))
                except (ValueError, TypeError):
                    pass

        if len(timestamps) >= 2:
            start = time.localtime(timestamps[0])
            end = time.localtime(timestamps[-1])
            return f"{time.strftime('%m-%d %H:%M', start)}-{time.strftime('%H:%M', end)}"
        elif timestamps:
            start = time.localtime(timestamps[0])
            return time.strftime('%m-%d %H:%M', start)
        return ""

    def _shrink_segment(self, segment: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """降级处理：只保留段首尾消息。"""
        if len(segment) <= 4:
            return segment
        # 保留前2条和后2条
        return segment[:2] + segment[-2:]

    # ──────────────────── Tool Output Archiving ────────────────────

    # 工具输出超过此长度则归档（5000 字符）
    _ARCHIVE_THRESHOLD_CHARS = 5000

    def _archive_tool_output(self, content: str, tool_name: str, session_id: str) -> tuple[str, str]:
        """归档过大的工具输出，返回 (archive_id, 摘要文本)。"""
        import hashlib
        import os
        from pathlib import Path

        # 生成 archive_id
        content_hash = hashlib.md5(content.encode()).hexdigest()[:12]
        archive_id = f"{tool_name}_{content_hash}"

        # 归档路径：var/tool_archives/{instance_id}/{session_id}/{archive_id}.txt
        instance_id = os.environ.get("APP_INSTANCE_ID", "default")
        base_dir = Path("var/tool_archives") / instance_id / session_id
        base_dir.mkdir(parents=True, exist_ok=True)

        archive_path = base_dir / f"{archive_id}.txt"
        archive_path.write_text(content[:100000], encoding="utf-8")  # 最多存 100K

        # 返回摘要
        summary = content[:200] + "..." if len(content) > 200 else content
        return archive_id, summary

    def _should_archive_tool_output(self, tool_name: str, content: str) -> bool:
        """判断工具输出是否应该归档。"""
        if len(content) < self._ARCHIVE_THRESHOLD_CHARS:
            return False
        # 只归档特定类型的工具输出
        archiveable = {
            "terminal", "execute_code", "read_file", "read_files",
            "list_directory", "search_files", "web_fetch",
        }
        return tool_name in archiveable

    def _peek_signalled(self) -> list[dict]:
        """只读取内存信号池（不清空）。失败返回空 list。"""
        try:
            from domain.lifecycle.session_events import peek_signalled_events
            return peek_signalled_events()
        except Exception:
            return []

    def _has_uninjected_signalled_events(self) -> bool:
        """内存池里是否有「未在本 wake 注入过」的新事件。

        _injected_signal_event_ids 记录本 session 已经注入给模型的事件 id，
        用于去重——避免模型在同一 wake 里被同一事件反复打断。
        """
        events = self._peek_signalled()
        if not events:
            return False
        return any(
            e.get("event_id") not in self._injected_signal_event_ids
            for e in events
        )

    def _revoke_rest_and_resume(self, rest_call: dict) -> bool:
        """撤销 rest() 的副作用，让 affair 回到 RUNNING 以便处理新事件。

        rest() 返回的 sentinel 字段决定回滚策略：
        - set_alarm=True / reused_alarm_id：本次 rest 新建了闹钟 → cancel 它
        - affair_id 非空：本次 rest 把 affair 改了 BLOCKED → 改回 RUNNING +
          clear_wait_intent
        - reused_alarm_id 仅在 reuse 路径出现：那个闹钟是别处管理的，**不 cancel**，
          只回滚 affair 副作用（模型会重新决定，可能再次 rest 复用它）

        任何一步失败都返回 False（保持原 rest 生效，让 cron 兜底处理新事件）。
        成功返回 True，调用方据此决定是否继续循环。
        """
        try:
            import json as _json
            raw = rest_call.get("result") or ""
            # 从 tool result 字符串里解析 rest 的 sentinel 字段
            try:
                data = _json.loads(raw)
            except Exception:
                return False

            aid = data.get("affair_id")
            reused_id = data.get("reused_alarm_id")
            set_alarm_field = data.get("set_alarm", None)
            wake_at = data.get("wake_at") or ""

            # 判断是否本次 rest 新建了闹钟：
            # - reuse 路径：set_alarm=False + reused_alarm_id=<id> → 复用，**不取消**
            # - until 路径：set_alarm 字段缺失（None）→ 隐含新建了，需取消
            # - 无 affair 兜底路径：affair_id=None 但也新建了闹钟
            is_new_alarm = (set_alarm_field is None) and bool(wake_at) and not reused_id

            # 1. cancel 本次 rest 新建的闹钟（reuse 的不动，那是别处管理的）
            if is_new_alarm:
                try:
                    from domain.lifecycle.alarms import list_pending_alarms, cancel_alarm
                    # dedup 保证 (timer, wake_at) 唯一；按 fire_at 精确匹配后取消
                    for a in list_pending_alarms("timer"):
                        if (a.get("fire_at") or "") == wake_at:
                            cancel_alarm(a.get("id"))
                            break
                except Exception as exc:
                    logger.warning("revoke_rest: cancel alarm failed: %s", exc)

            # 2. 回滚 affair：BLOCKED → RUNNING + clear WaitIntent
            if aid:
                from domain.lifecycle.affairs.runtime import (
                    update_affair, clear_wait_intent,
                )
                from domain.lifecycle.state_machine import AffairStatus
                update_affair(aid, status=AffairStatus.RUNNING)
                clear_wait_intent(aid)

            return True
        except Exception as exc:
            logger.warning("revoke_rest: failed (keeping rest as-is): %s", exc)
            return False

    def _inject_signalled_events(self, messages: list[dict[str, Any]]) -> None:
        """Notify the model when new events arrive mid-session (RUNNING state).

        Design principle — show = consume:
        - message / group_message: show full content + auto-consume in DB + clear from queue.
          Model gets the message immediately, no need to call sense_event_detail separately.
        - Other event types (routine, timer, etc.): show only ID + type, no auto-consume.
          Model decides when/whether to call sense_event_detail.

        Uses peek_signalled_events so the queue is NOT cleared here by default.
        Only message events call consume_signalled_events (which clears the queue).
        """
        try:
            from domain.lifecycle.session_events import peek_signalled_events
            events = peek_signalled_events()
        except ImportError:
            return

        if not events:
            return

        new_events = [e for e in events if e.get("event_id") not in self._injected_signal_event_ids]
        if not new_events:
            return

        # Split by whether we auto-consume
        auto_consume_events: list[dict] = []
        manual_events: list[dict] = []

        for ev in new_events:
            kind = ev.get("kind", "")
            if kind in ("message", "group_message"):
                auto_consume_events.append(ev)
            else:
                manual_events.append(ev)

        # Mark all new events as injected (prevent re-injection this session)
        self._injected_signal_event_ids.update(e.get("event_id") for e in new_events)

        if auto_consume_events:
            self._consume_human_events(auto_consume_events, messages)

        if manual_events:
            self._notify_manual_events(manual_events, messages)

    def _consume_human_events(self, events: list[dict], messages: list[dict[str, Any]]) -> None:
        """Show message/group_message content as tool result and auto-consume.

        渲染策略：调用 ``_render_signal_message`` 走 event_types.yaml 的
        ``wake_prompt`` 模板，**和 wake 启动同源**。

        历史 bug:这里曾 hardcode 模板 ``[飞书消息 #{eid}] (sender) text``，丢了
        chat_id / "对话"/"群" 标识 / chat_name 等关键上下文——模型在 mid-session
        接到消息时既不知道是私聊还是群、更不知道回哪个 chat。结果：用户私聊机器人，
        机器人却把回复发到了最近活跃的群里（7/13 wake #1927 call 23 现象）。

        修复：复用 event_registry 的 wake_prompt，私聊模板含 ``对话：{chat_id}``、
        群模板含 ``群：{chat_name}（{chat_id}）``——模型既能判断回复路径、也能
        正确附带发送上下文。
        """
        # Auto-consume: mark in DB + clear from in-memory queue
        self._do_consume_events(events)

        for ev in events:
            content = _render_signal_message(ev)

            assistant_msg, tool_msg = self._sys_tool_call("wake_signal", content)
            messages.append(assistant_msg)
            messages.append(tool_msg)
            # ⚠ 关键 bug 修复(2026-06-23):mid-session 注入必须持久化到 session_db,
            # 否则下一次 _chat 重新加载 messages 时,wake_signal 消失在历史里——
            # 模型本轮 LLM call 看一眼,下一轮 LLM call 就忘了。
            # 历史 BUG 现象:alpha 在 RUNNING 时收到人类的复杂任务,但只在
            # 当前 LLM call 看到;后续 N 次 LLM call 都失去这条,直到偶然调
            # sense_conversation 才在 messages.db 里翻到——导致 14 分钟黑箱。
            # 修法:_append_message 写到 sessions 表(下一轮 _chat 重启 messages
            # 时它还在)。chat_id 从 ev payload 取(让前端 Transcript 也按 chat 聚合)。
            payload = ev.get("payload", {}) if isinstance(ev.get("payload"), dict) else {}
            if self.session_id:
                self._append_message(
                    self.session_id, "tool", content,
                    tool_name="wake_signal",
                    chat_id=payload.get("chat_id", "") if isinstance(payload, dict) else "",
                )
            if self.audit_ctx is not None:
                try:
                    self.audit_ctx.recall("wake_signal", content)
                except Exception:
                    logger.debug("Failed to dual-write wake_signal", exc_info=True)


    def _notify_manual_events(self, events: list[dict], messages: list[dict[str, Any]]) -> None:
        """Notify about non-message events as tool result (no auto-consume)."""
        lines: list[str] = ["[新事件 — 会话中途到达]"]
        for ev in events:
            eid = ev.get("event_id", "?")
            kind = ev.get("kind", "")
            display = str(ev.get("display_name") or kind)
            lines.append(f"- [#{eid}] {display}")
        lines.append("> 用 `sense_event_detail(event_id)` 查看详情。")

        assistant_msg, tool_msg = self._sys_tool_call("wake_signal", "\n".join(lines))
        messages.append(assistant_msg)
        messages.append(tool_msg)
        if self.audit_ctx is not None:
            try:
                self.audit_ctx.recall("wake_signal", "\n".join(lines))
            except Exception:
                logger.debug("Failed to dual-write wake_signal", exc_info=True)
    def _do_consume_events(self, events: list[dict]) -> None:
        """Mark events as consumed in DB and clear them from the in-memory queue."""
        event_ids = [ev.get("event_id") for ev in events if ev.get("event_id") is not None]
        if not event_ids:
            return

        # 1. Mark consumed in DB
        try:
            from domain.lifecycle.events import consume_event
            for eid in event_ids:
                try:
                    consume_event(eid)
                except Exception:
                    pass
        except ImportError:
            pass

        # 2. Remove only these events from in-memory signalled queue
        try:
            from domain.lifecycle.session_events import consume_signalled_events_by_ids
            consume_signalled_events_by_ids(set(event_ids))
        except ImportError:
            pass

    def _inject_entity_recall(self, messages: list[dict[str, Any]]) -> None:
        """Scan new messages for known entities and inject relevant memories.

        Assistant thinking content (the model's own reasoning) is the primary
        signal — it reveals what the model is focusing on. Tool results and
        user messages are secondary.

        Entity-level dedup: same entity only injected once per session.
        Memory-level dedup: same memory_id only injected once per session.
        """
        new_messages = messages[self._last_scanned_msg_count:]
        self._last_scanned_msg_count = len(messages)

        if not new_messages:
            return

        # Separate: assistant thinking (primary) vs other messages (secondary)
        # reasoning_content 是 GLM 推理过程，经常比 content（"打完话后的输出"）长得多，
        # 包含模型的关注点——是 entity 匹配最强信号。现在 reasoning_content 直接在
        # assistant msg 里（_strip_old_reasoning 保留最近 N 轮），只要它还没被 strip
        # 就纳入扫描。
        thinking_texts: list[str] = []
        other_texts: list[str] = []
        for m in new_messages:
            if m.get("role") == "assistant":
                # 优先 reasoning_content（模型真实的推理过程），fallback 到 content
                content = (m.get("reasoning_content") or m.get("content") or "")
            else:
                content = m.get("content", "")
            if isinstance(content, str) and len(content.strip()) >= 30:
                if m.get("role") == "assistant":
                    thinking_texts.append(content)
                else:
                    other_texts.append(content)

        # Build context: thinking first (higher priority), other second
        context_parts: list[str] = []
        if thinking_texts:
            context_parts.extend(thinking_texts[-2:])  # last 2 rounds of thinking
        if other_texts:
            context_parts.extend(other_texts[-3:])  # last 3 other messages

        if not context_parts:
            return

        combined = " ".join(context_parts)
        if len(combined) < 30:
            return

        try:
            from domain.memory.memory.consciousness.entity_index import (
                extract_entities_from_context,
                query_entities_ranked,
            )
        except ImportError:
            return

        entities = extract_entities_from_context(combined)
        if not entities:
            return

        # Entity-level dedup: skip entities already injected this session
        new_entities = [e for e in entities if e not in self._injected_entities]
        if not new_entities:
            return

        memories = query_entities_ranked(
            new_entities,
            current_context=combined,
            exclude_ids=self._injected_memory_ids,
            limit=3,
        )
        if not memories:
            return

        # Format as detail block — entity name + matched memory snippet + type tag.
        # 用户原设计:联想命中要返详情,不只名字。模型在 turn 中能直接看到关联记忆,
        # 不必再调 recall_entity('名字') 二次拉 (那一步本来基本不发生)。
        # 每个 memory 用 200 字 snippet (足以看到关键结论,不至于过长占 token)
        if new_entities:
            self._prune_recall_injections(messages)
            lines = ["[联想命中 — 你正在思考的上下文里提到了你有相关记忆的实体]"]
            for mem in memories:
                mtype = str(mem.get("memory_type", "")).upper()
                entity = str(mem.get("_matched_entity", ""))
                tag = f"[实体:{entity}]" if entity else ""
                snippet = str(mem.get("snippet", "")).strip().replace("\n", " ")
                if len(snippet) > 200:
                    snippet = snippet[:100] + "…" + snippet[-100:]
                if mtype == "PROFILE":
                    # profile = 对该实体的提炼理解(profile-first,替代散乱碎片)
                    lines.append(f"- [{entity} · 概念] {snippet}")
                else:
                    # snippet 只截关键部分(避免长篇日记占满 faketool)
                    lines.append(f"- [{mtype}]{tag} {snippet}")
            # 注解:多少实体命中 / 多少 memory 返回
            lines.append(f"(命中 {len(new_entities)} 实体: "
                         f"{', '.join(new_entities[:8])}"
                         + (f" 等{len(new_entities)-8}个" if len(new_entities) > 8 else "")
                         + f"; 召回 {len(memories)} 条。"
                           f"如需更多调 recall_entity('实体名'))")
            breadcrumb_text = "\n".join(lines)
            assistant_msg, tool_msg = self._sys_tool_call("entity_recall", breadcrumb_text)
            messages.append(assistant_msg)
            messages.append(tool_msg)
            self._recall_injection_indices = [len(messages) - 2, len(messages) - 1]
            if self.audit_ctx is not None:
                try:
                    self.audit_ctx.recall("entity_recall", breadcrumb_text)
                except Exception:
                    logger.debug("Failed to dual-write entity_recall", exc_info=True)

        self._injected_entities.update(new_entities)
        self._injected_memory_ids.update(
            str(m.get("memory_id", "")) for m in memories if m.get("memory_id")
        )

    def _prune_recall_injections(self, messages: list[dict[str, Any]]) -> None:
        """Remove previous recall injection pairs (assistant tool_call + tool result)."""
        for idx in sorted(self._recall_injection_indices, reverse=True):
            if idx < len(messages):
                messages.pop(idx)
        self._recall_injection_indices = []

    def mark_memories_presented(self, memory_ids: set[str]) -> None:
        """Mark memory IDs as already presented (e.g. from wake prompt).

        Prevents mid-session re-injection of memories already shown at wake time.
        """
        self._injected_memory_ids.update(memory_ids)


def _render_signal_message(ev: dict[str, Any]) -> str:
    """渲染 mid-session 注入的消息事件为 wake_signal 提示文本（模块级 helper，便于测试）。

    与 ``domain.lifecycle.heartbeat._resolve_event_prompt`` 同源——message /
    group_message 走 yaml 的 ``wake_prompt`` 模板，含 chat_id / chat_name /
    sender_position 等完整上下文。回复时模型能看出是私聊还是群、回哪个 chat。

    退化兜底（无 yaml / 非 message 类 / 异常）保留精简模板但显式含 chat_id。
    """
    eid = ev.get("event_id", "?")
    kind = ev.get("kind", "") or ""
    payload = ev.get("payload", {}) if isinstance(ev.get("payload"), dict) else {}
    display = str(ev.get("display_name") or kind or "")

    # 1. 走 yaml 模板（同源 wake_prompt）
    rendered_body = ""
    if kind in ("message", "group_message"):
        try:
            from domain.lifecycle.heartbeat import _resolve_event_prompt
            rendered_body = _resolve_event_prompt(kind, [ev]).strip()
        except Exception:
            rendered_body = ""

    # 2. 退化兜底：无 yaml / 异常时保留精简模板，但加 chat_id
    if not rendered_body:
        text = payload.get("text", "")
        sender = payload.get("sender_name", "")
        chat_id = payload.get("chat_id", "")
        body_inner = (f"({sender}) {text}" if sender else text) if text else display
        rendered_body = (
            "💬 新消息。"
            + (f"\n对话/群：{chat_id}" if chat_id else "")
            + f"\n{body_inner}"
        )

    # 3. 信号头 + 自动已读提示。chat_id 已经在 yaml 模板渲染的正文里
    # （私聊：「对话：{chat_id}」，群：「群：{chat_name}（{chat_id}）」），
    # 不再额外硬塞「必须 express_to_human(chat_id=xxx)」之类的具体调用方式——
    # 模型看到 chat_id 就知道回哪里，参数细节交给模型按工具 schema 自己决定。
    return (
        f"[#{eid} · 新消息到达 - 会话中途注入]\n"
        f"{rendered_body}\n"
        f"> 注意：消息已自动标记为已读，稍后回复即可。"
    )

