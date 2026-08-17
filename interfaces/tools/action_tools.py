"""行动工具集 (Actions) — Agent 对外部世界与自我状态施加影响。

工具分类：
  通信类：
    - express_to_human: 发送消息给用户（飞书 API 直连）
    - rest: 进入休息（设置 WaitIntent + 闹钟 + BLOCKED 状态）

  记忆类：
    - record_thought: 留思绪（意识残留，穿越睡眠保持连续性）
    - write_diary: 写日记
    - remember_him: 记录关于用户的观察
    - update_scratchpad: 更新草稿本（append/replace）
    - add_lesson: 记录经验教训
    - update_self_knowledge: 更新自我认知档案

  管理类：
    - manage_work: 工作看板 CRUD
    - manage_goals: 目标管理
    - manage_plan: 长期计划里程碑
    - manage_daily: 每日计划
    - update_rules: 长期行为规则
    - update_context: 交接上下文

express_to_human 发送拦截：
  调用前会经过 communication.check_before_send() 检查是否有未读消息。
  如果被拦截 → 返回完整消息上下文，让模型看到新消息后重新决定回复内容。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

logger = logging.getLogger("tools.action_tools")

# 注：旧 _schedule_hint_shown_sessions cache 已随 schedule_check 删除（rest 新语义不再用）

from domain.vital.simulation import (
    ENERGY_COST_PER_CALL,
    get_engine,
)
from datetime import timedelta, datetime
import domain.vital as vitals
from domain.memory.memory.consciousness.runtime import (
    record_thought as _record,
    write_diary as _diary,
    write_about_him as _him,
    update_scratchpad as _scratchpad,
    add_work_item as _add_work,
    start_work_item as _start_work,
    complete_work_item as _complete_work,
    remove_work_item as _remove_work,
    manage_goal as _manage_goal,
    manage_plan_item as _manage_plan,
    plan_daily as _plan_daily,
    add_planned_item as _add_daily,
    complete_planned_item as _complete_daily,
    check_daily as _check_daily,
    update_rules as _update_rules,
    update_context as _update_context,
    add_lesson as _add_lesson,
    append_insight as _append_insight,
    update_self_knowledge as _update_self_knowledge,
)

from interfaces.tools.registry import registry


_BLOCK_SENTINEL = "__l4_block__"


def _j(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _list_contact_candidates() -> list[dict]:
    """列出可用联系人/群的 channel 候选——express_to_human 报错时返回给模型。"""
    out = []
    try:
        from domain.contacts import list_contacts as _lc
        for c in (_lc() or []):
            for p in (c.get("platform_ids") or []):
                pf = (p.get("platform") or "").strip()
                pid = (p.get("platform_id") or "").strip()
                if not pid:
                    continue
                if pf == "feishu":
                    prefix = "feishu"
                elif pf == "wechat":
                    prefix = "wechat"
                else:
                    prefix = pf
                is_group = pid.startswith("oc_")
                kind = "group" if is_group else "dm"
                name = (c.get("name") or "").strip() or "(未命名)"
                out.append({"name": name, "channel": f"{prefix}:{kind}:{pid}"})
    except Exception:
        pass
    try:
        # 也加群聊候选
        from domain.social_context import _collect_known_chats
        from infrastructure.config import get_app_instance_id as _get_iid
        iid = _get_iid() or ""
        if iid:
            for cid, cname in (_collect_known_chats(iid) or {}).items():
                prefix = "feishu" if cid.startswith("oc_") else ("wechat" if "@im" in cid else "feishu")
                out.append({"name": cname or "群", "channel": f"{prefix}:group:{cid}"})
    except Exception:
        pass
    return out[:15]  # cap


def _get_runtime_channel_prefix() -> str:
    """返回当前事件来源的平台前缀（feishu / wechat / 默认 feishu）。

    用于 express_to_human 合成默认 channel 字符串——决定走飞书还是微信发送路径。
    优先级：runtime_context ContextVar > _REPLY_CONTEXT 全局 dict > 默认 feishu。
    （_REPLY_CONTEXT 用于跨线程可见；to_thread 里 set 的 ContextVar 主线程看不到）

    旧版本归一为内部代号 "lark"，现已统一为更直观的 "feishu"。
    读侧需同时认 "feishu" 与历史存量 "lark"。
    """
    try:
        from domain.lifecycle.runtime_context import get_current_event_platform
        pf = get_current_event_platform()
        if pf:
            return pf
    except Exception:
        pass
    # fallback：从 _REPLY_CONTEXT 读（跨线程可见）
    try:
        # 优先用全局 mirror（不依赖 ContextVar），其次用 ContextVar
        _iid = _CURRENT_INSTANCE_ID or ""
        if not _iid:
            from infrastructure.config import get_app_instance_id as _get_iid
            _iid = _get_iid() or ""
        if _iid:
            ctx = _REPLY_CONTEXT.get(_iid) or {}
            pf2 = str(ctx.get("platform") or "").strip()
            if pf2:
                return "feishu" if pf2 in ("feishu", "lark") else pf2
    except Exception:
        pass
    return "feishu"


_FEISHU_PREFIXES = ("feishu:", "lark:")


def _is_feishu_channel(channel: str) -> bool:
    """判定是否飞书发送路径——同时认 feishu 与历史遗留 lark 前缀。"""
    return channel.startswith(_FEISHU_PREFIXES)


def _channel_kind(channel: str) -> str:
    """从 channel 提取 kind 段（feishu:<kind>:<id> 中的 <kind>）。无前缀返回 ""。"""
    if not _is_feishu_channel(channel) or channel.count(":") < 2:
        return ""
    return channel.split(":", 2)[1]


def _channel_has_prefix(channel: str, kind: str) -> bool:
    """channel 是否形如 <pf>:<kind>:... （pf = feishu 或 lark）。

    kind 应含尾部冒号，如 'group:' / 'dm:'。
    """
    return any(
        channel.startswith(f"{pf}{kind}")
        for pf in _FEISHU_PREFIXES
    )


def _strip_feishu_prefix(channel: str, *, kind: str) -> str:
    """剥离飞书平台前缀 + kind 段，返回剩余 id 段。

    kind 应含尾部冒号（'group:' / 'dm:'）。同时认 feishu/lark 前缀。
    """
    for pf in _FEISHU_PREFIXES:
        marker = f"{pf}{kind}"
        if channel.startswith(marker):
            return channel[len(marker):]
    return channel


def _channel_has_raw_suffix(channel: str, suffix: str) -> bool:
    """channel 是否形如 <pf>:<suffix>（如 feishu:oc_xxx）——用于 reply context 直接匹配。"""
    return channel.endswith(suffix) and _is_feishu_channel(channel) and channel.count(":") == 1


def _resolve_chat_id(short_or_full: str) -> str:
    """通用短码→完整 ID 补全。模型传 chat_id 时可能给的是 prompt 里的短码
    (oc_5ff7967bf5… / ou_eb5083…)，本函数负责还原成完整 ID 给飞书 API。

    对 oc_(群) 和 ou_(私聊 open_id) 一视同仁——补全是通用机制，不分通道。
    匹配策略（依次尝试）：
      1) 已是完整 ID（>= 32 字符）→ 透传
      2) 当前实例的 reply context（group/dm 上下文）反查前缀匹配
      3) 全局 reply context 变量反查
      4) contacts DB：遍历 platform_ids 找前缀匹配（覆盖 ou_ 私聊）
    失败 → 返回原值，让飞书报 invalid receive_id，便于排查。
    """
    if not short_or_full:
        return ""
    candidate = short_or_full.strip()
    if len(candidate) >= 32:
        return candidate
    needle = candidate.rstrip("…")
    if not needle:
        return candidate

    # (2) 当前实例 reply context
    iid = _get_instance_id_for_context()
    for full_chat in (_REPLY_CONTEXT.get(iid) or {}).values():
        if full_chat and full_chat.startswith(needle):
            return full_chat
    # (3) 全局 reply context 变量
    global _DM_REPLY_CHAT_ID, _GROUP_REPLY_CHAT_ID
    for full_chat in [_DM_REPLY_CHAT_ID or "", _GROUP_REPLY_CHAT_ID or ""]:
        if full_chat and full_chat.startswith(needle):
            return full_chat
    # (4) contacts DB：ou_/oc_ 通用前缀匹配（私聊补全从此覆盖）
    try:
        from domain.contacts import list_contacts
        for c in list_contacts() or []:
            for p in (c.get("platform_ids") or []):
                if p.get("platform") == "feishu":
                    pid = (p.get("platform_id") or "").strip()
                    if pid.startswith(needle):
                        return pid
    except Exception as exc:
        logger.debug("chat_id resolve via contacts failed: %s", exc)
    return candidate  # 无法解析，按原值由飞书报错提示


def _feishu_receive_id_type(receive_id: str) -> str:
    """按 ID 前缀识别飞书 receive_id_type。

    飞书 ID 规范：
      ou_  → open_id
      oc_  → chat_id（群/单聊 chat 都用这个）
      on_  → union_id
      其他 → 默认 open_id（最宽松假设）
    """
    if not receive_id:
        return "open_id"
    if receive_id.startswith("ou_"):
        return "open_id"
    if receive_id.startswith("oc_"):
        return "chat_id"
    if receive_id.startswith("on_"):
        return "union_id"
    return "open_id"


def _explain_feishu_send_failure(resp: dict, channel: str, receive_id: str) -> str:
    """把飞书 IM 消息发送的错误返回翻译成模型可 actionable 的诊断。

    飞书的 msg 是面向开发者的（如 "invalid receive_id"），模型读不出根因，
    会不断切换 chat_id / kind 重试也救不回来。这里按 code/msg 关键词给出
    下一步建议：换 chat / 用其他 chat_id / 沉默退出 / 等接口恢复。
    """
    code = resp.get("code")
    msg = resp.get("msg") or ""
    raw = f"feishu code={code}, msg={msg}"

    # code 230002: chat not exist / bot not in chat
    # 关键词 "invalid receive_id" 历史上=bot 不在该群/chat_id 失效
    if code in (230002, 230009) or "invalid receive_id" in msg.lower():
        return (
            f"{raw} | 我（这个 bot）不在 chat_id={receive_id[:16]}… 里，"
            "或该 chat 已失效。请改用 sense_conversation 查看我真实在的 chat，"
            "别再用当前 channel。"
        )
    # code 99991663 / 99991668 等 token/permission 类
    if code and 99991000 <= code <= 99992000:
        return (
            f"{raw} | 飞书 token/权限临时异常，短期重试或暂不发送；"
            "如需静默可调 rest()。"
        )
    # code 11200 系列：消息被审计/风控
    if code and 11200 <= code <= 11299:
        return (
            f"{raw} | 消息触发了飞书内容合规策略。检查 text 是否含敏感词或过长。"
        )
    # 兜底
    return raw


# ──────────────────────────────── express_to_human ────────────────────────────────

# 全局变量：存储群聊回复上下文（备用）
# 多实例隔离的回复上下文：每个 instance 维护自己的 group/dm chat_id
_REPLY_CONTEXT: dict[str, dict[str, str]] = {}  # instance_id -> {group, dm, platform, wechat_context_token}

# 全局 mirror —— 任何线程可读可写，不依赖 ContextVar 跨线程语义
# handler._route_to_life (子线程) 写，express_to_human (agent 主线程) 读
_CURRENT_INSTANCE_ID: str = ""


def _set_current_instance_id_mirror(iid: str) -> None:
    """供 handler 调：记住当前进程在处理哪个实例。"""
    global _CURRENT_INSTANCE_ID
    _CURRENT_INSTANCE_ID = iid or ""

_GROUP_REPLY_CHAT_ID = None
_DM_REPLY_CHAT_ID = None


def _get_instance_id_for_context() -> str:
    try:
        from infrastructure.config import get_app_instance_id
        return get_app_instance_id() or "_default"
    except Exception:
        return "_default"


def set_group_reply_context(chat_id: str) -> None:
    """设置群聊回复上下文（由 wake_digital_life 调用）。"""
    global _GROUP_REPLY_CHAT_ID
    _GROUP_REPLY_CHAT_ID = chat_id
    iid = _get_instance_id_for_context()
    _REPLY_CONTEXT.setdefault(iid, {})["group"] = chat_id
    logger.info("set_group_reply_context: instance=%s chat_id=%s", iid[:8], chat_id)


def set_dm_reply_context(chat_id: str) -> None:
    """设置私聊回复上下文（飞书 _route_to_life 调用）。"""
    global _DM_REPLY_CHAT_ID
    _DM_REPLY_CHAT_ID = chat_id
    iid = _get_instance_id_for_context()
    _REPLY_CONTEXT.setdefault(iid, {})["dm"] = chat_id
    logger.info("set_dm_reply_context: instance=%s chat_id=%s", iid[:8], chat_id)


def _get_group_reply_chat_id() -> str:
    iid = _get_instance_id_for_context()
    return (_REPLY_CONTEXT.get(iid) or {}).get("group") or _GROUP_REPLY_CHAT_ID


def _get_dm_reply_chat_id() -> str:
    iid = _get_instance_id_for_context()
    return (_REPLY_CONTEXT.get(iid) or {}).get("dm") or _DM_REPLY_CHAT_ID


def _get_recent_group_chat_id() -> Optional[str]:
    """从数据库获取最近未消费的群消息 chat_id。"""
    try:
        import sqlite3
        from infrastructure.config import get_runtime_state_db_path
        db_path = get_runtime_state_db_path()
        if not db_path.exists():
            return None
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        # 查找最近 5 分钟内未消费的群消息
        # 五分钟窗口检查未读 group_message。created_at 现为 UTC ISO，
        # 比较右值也用 UTC ISO（clock.now_iso - 5min）保证字典序正确。
        from domain.lifecycle import clock as _clock
        from datetime import timedelta as _td
        _since = (_clock.now_dt() - _td(minutes=5)).isoformat(timespec="seconds")
        row = conn.execute(
            "SELECT payload FROM events WHERE kind='group_message' "
            "AND consumed_at IS NULL "
            "AND created_at > ? "
            "ORDER BY created_at DESC LIMIT 1",
            (_since,),
        ).fetchone()
        conn.close()
        if row:
            import json
            payload = json.loads(row["payload"])
            return payload.get("chat_id", "")
    except Exception as e:
        logger.debug("Failed to get group chat_id: %s", e)
    return None



def _handle_express_to_human(args: Dict[str, Any], **context) -> str:
    """向用户发送消息 — 数字生命唯一的对外表达通道（支持多通道扇出）。

    channels 参数（list）时：同一 text 依次发到多个通道（如语音说一句 +
    飞书留文字版）。任何一路被发送前拦截（未读消息/未看通道）→ 全部中止
    （拦截语义是"回复过时，重写"，继续发没有意义）；普通发送失败不影响
    其他通道。单 channel（字符串/缺省）走原路径，行为不变。

    发送流程（每一路）：
      1. 发送前拦截（communication.check_before_send）— 检查未读消息
      2. Channel 解析 — 群聊/私聊上下文 → 飞书 chat_id
      3. 飞书 API 直连发送（获取 tenant_access_token → POST 消息）
      4. 发送成功 → 记录到 SENT_LOG + 设置 awaiting_reply 闹钟
      5. 精力消耗（ENERGY_COST_PER_CALL）

    Channel 解析优先级：群聊上下文 > 私聊上下文 > 当前事件 chat
    """
    raw_channels = args.get("channels")
    if isinstance(raw_channels, str):
        raw_channels = [raw_channels]
    if not isinstance(raw_channels, list) or not raw_channels:
        # 单通道原路径（channel/chat_id 参数或上下文兜底）
        return _express_one(args, args.get("channel") or "", **context)

    # 多通道扇出：依次发；拦截即全部中止（拦截有事件消费副作用）
    results: list[dict] = []
    for i, ch in enumerate(raw_channels):
        per_args = dict(args)
        per_args["channel"] = str(ch or "").strip()
        per_args.pop("chat_id", None)  # channel 已显式给出，不让 chat_id 抢优先级
        out = _express_one(per_args, per_args["channel"], **context)
        try:
            parsed = json.loads(out) if isinstance(out, str) else dict(out)
        except Exception:
            parsed = {"raw": out}
        # 被拦截（sent=False 且带拦截语义）→ 中止剩余通道，保留拦截上下文
        if parsed.get("sent") is False and (
            parsed.get("recent_chat_log") or parsed.get("result_summary")
        ):
            parsed.setdefault("aborted_channels", raw_channels[i + 1:])
            return _j(parsed)
        results.append(parsed)

    ok = [r for r in results if r.get("sent")]
    return _j({
        "sent": bool(ok),
        "multi": True,
        "results": results,
        "result_summary": (
            f"已发送到 {len(ok)}/{len(results)} 个通道"
            if ok else "全部通道发送失败"
        ),
    })


def _express_one(args: Dict[str, Any], channel: str, **context) -> str:
    """单通道发送（原 _handle_express_to_human 主体）。"""
    logger.info("express_to_human CALLED: text=%s, channel=%s, chat_id=%s, mentions=%s",
                args.get("text", "")[:50], channel, args.get("chat_id", ""),
                args.get("mention_user_ids", []))
    # 显式 import os（函数顶部）；之前在 try 内 import, line 465 提前调 os.getenv 触发
    # UnboundLocalError。把 import 移到这里，所有 os 用法都被绑定。
    import os
    text = (args.get("text") or "").strip()
    # channel 优先取扇出层传入的值（多通道时每轮不同），args 里的作 fallback
    channel = (channel or args.get("channel") or "").strip()
    chat_id_arg = (args.get("chat_id") or "").strip()
    # ⚠ awaiting_reply 参数解析
    # 模型用 wait_reply 显式表达意图: "auto" / "none" / 数字分钟数
    # 三种值:
    #   "auto" (默认): 系统自动判断——私聊等5min; 群聊@了真人等5min; 群聊没@真人不等
    #   "none": 明确不等(同步信息/通知类, 不需要回复)
    #   数字(N): 明确等N分钟(1-15)
    wait_reply_raw = str(args.get("wait_reply", "auto")).strip().lower()
    if wait_reply_raw in ("none", "no", "false", "0"):
        wait_minutes = 0
    elif wait_reply_raw in ("auto", ""):
        wait_minutes = -1  # 哨兵: 表示需要走自动策略(下面 channel 确定后判定)
    else:
        try:
            wait_minutes = max(0, min(int(wait_reply_raw), 15))
        except ValueError:
            wait_minutes = -1  # 解析失败 fallback to auto

    # mention_user_ids：模型可在消息里 @ 其他 user/bot（飞书群聊场景）
    raw_mentions = args.get("mention_user_ids") or []
    if isinstance(raw_mentions, str):
        raw_mentions = [raw_mentions]
    mention_user_ids: list[str] = []
    invalid_mentions: list[str] = []
    for m in raw_mentions:
        ms = str(m or "").strip()
        if not ms.startswith("ou_"):
            continue
        # 校验 ou 在 contacts 表里 — 模型可能 hallucinate 不存在的 ou_xxx
        # （飞书会渲染 "@" 没有名字 = raw "@" 残留）
        try:
            from domain.contacts import lookup_name
            if lookup_name("feishu", ms):
                mention_user_ids.append(ms)
            else:
                invalid_mentions.append(ms)
                logger.warning("mention_user_ids 含未注册的 ou_%s，跳过", ms[:16])
        except Exception:
            mention_user_ids.append(ms)  # 查询失败 fallback：保留原值

    # 自动 @ 转换：扫 text 里的 "@<displayName>"，按 contacts 表里 kind=human/bot
    # 的 contact name 匹配 → 替换为 <at user_id="ou_xxx"></at> 标签
    # 模型只需要在 text 里写"@zero 看看这个"，不用记 open_id 也能命中
    try:
        from domain.contacts import list_contacts
        from infrastructure.config import get_instance_display_name
        my_display = (get_instance_display_name() or "").strip().lower()
        all_contacts = list_contacts() or []
        # 按 name 长度倒序匹配，避免"@小张"被"@张"先命中
        named = sorted(
            [c for c in all_contacts if (c.get("name") or "").strip()],
            key=lambda c: len(c["name"]),
            reverse=True,
        )
        for c in named:
            cname = c["name"]
            # @name 形式（带 @ 前缀，更明确）
            mention_target = f"@{cname}"
            if mention_target not in text:
                continue
            # 跳过自己（不让 bot @ 自己 — 会自循环）
            if cname.lower() == my_display:
                continue
            # 找平台 ID（暂只支持 feishu）
            feishu_ids = [p["platform_id"] for p in (c.get("platform_ids") or [])
                          if p.get("platform") == "feishu" and (p.get("platform_id") or "").startswith("ou_")]
            if not feishu_ids:
                continue
            ou = feishu_ids[0]
            at_tag = f'<at user_id="{ou}"></at>'
            text = text.replace(mention_target, at_tag)
            if ou not in mention_user_ids:
                mention_user_ids.append(ou)
            logger.info("auto-mention: replaced @%s → %s", cname, ou[:12])
    except Exception as exc:
        logger.debug("auto-mention scan failed: %s", exc)

    if not text:
        return registry.tool_error("text is required")

    # ─── chat_id vs channel 解析（模型自主决策回复目标）──────────
    # 模型可在 chat_id（飞书 oc_xxx）或 channel（"lark:group:oc_xxx" / "lark:dm:oc_xxx"）二选一：
    #   - 显式 chat_id → 内部转 channel（按 wake_reason 推 kind）
    #   - 显式 channel → 直接用
    #   - 都不给 → fallback current_event_chat_id → reply context → env
    # 不再强制覆盖模型意图（移除原 is_group_wake force channel 逻辑）

    # 补全是通用机制：channel 形式里嵌入的短码 ID 也要补全（之前只有 chat_id 参数走补全，
    # 模型写 feishu:dm:ou_eb5083… 时短码会被原样发给飞书触发 invalid receive_id）。
    # 形如 <平台>:<kind>:<id> → 取 <id> 过 _resolve_chat_id，重组回 feishu: 前缀
    # （同时认历史遗留的 lark: 前缀，归一输出统一用 feishu:）。
    if _is_feishu_channel(channel) and channel.count(":") >= 2:
        _parts = channel.split(":", 2)
        _id = _parts[2].strip()
        if _id:
            channel = f"feishu:{_parts[1]}:" + _resolve_chat_id(_id)

    if chat_id_arg:
        # 模型可能传 prompt 显示的短码（如 oc_5ff7967bf5…），还原为完整 ID
        chat_id_arg = _resolve_chat_id(chat_id_arg)
        # kind 由 ID 前缀派生（取代旧版按 wake_reason 推断）：
        #   ou_ → dm（私聊对方 open_id）
        #   oc_ → group（群/会话 chat_id）
        #   on_ → dm（union_id，少见，按私聊处理）
        # 显式 kind 参数仍尊重（向后兼容），否则按 ID 前缀判断。
        explicit_kind = (args.get("kind") or "").strip().lower()
        if explicit_kind in ("group", "dm"):
            kind_str = explicit_kind
        elif chat_id_arg.startswith("oc_"):
            kind_str = "group"
        else:
            kind_str = "dm"  # ou_/on_/其他都按私聊
        _pf = _get_runtime_channel_prefix()
        channel = f"{_pf}:{kind_str}:{chat_id_arg}"
    elif not channel:
        # 都没给 → fallback current_event_chat_id（wake 时 set 的"当前事件来源"）
        try:
            from domain.lifecycle.runtime_context import get_current_event_chat_id
            curr_chat = get_current_event_chat_id()
        except Exception:
            curr_chat = ""
        if curr_chat:
            # 显式 kind=dm + 用 fallback chat：如果 fallback chat 是 oc_ 开头
            # （群 chat_id，不是真实 ou_ open_id），不能伪装成 dm 发——飞书会拒。
            # 这种 case 给模型明确错误，避免"被 dm 套前缀后 sanitize 又改回 group"
            # 的来回拼装陷阱。
            explicit_kind_fb = (args.get("kind") or "").strip().lower()
            if explicit_kind_fb == "dm" and curr_chat.startswith("oc_"):
                return _j({
                    "sent": False,
                    "channel": "",
                    "text": text,
                    "error": (
                        "你显式要求 kind=dm，但 current_event_chat_id 是 group chat "
                        f"{curr_chat[:16]}…。要么去掉 kind 让系统按 ID 前缀发，要么显式 chat_id=ou_xxx。"
                    ),
                })
            # kind 由 fallback chat_id 前缀派生（取代旧版读 wake_reason 判断）
            kind_str = "group" if curr_chat.startswith("oc_") else "dm"
            _pf = _get_runtime_channel_prefix()
            channel = f"{_pf}:{kind_str}:{curr_chat}"
        else:
            _pf = _get_runtime_channel_prefix()
            channel = f"{_pf}:default"
    # 模型给出 channel 直接保留，比如 "feishu:group:oc_xxx" 或 "feishu:dm:ou_xxx" 即可

    session_id = str(context.get("session_id") or "")

    # 通道兜底（仅限 wake 入口显式设置的 reply context；不再从 contacts 表「猜」群）。
    # 设计原则：目标通道必须明确。reply context 是 wake 根据 reason 正确 set 的上下文，
    # 属合法兜底；contacts 表自动挑一个群属于无依据猜测（曾导致发错通道），已移除。
    _default_markers = ("lark:default", "feishu:default", "wechat:default", "voice:default")
    if channel in _default_markers:
        # voice:default → 固定本地扬声器，不需要找 chat_id
        if channel == "voice:default":
            channel = "voice:speaker"
        else:
            _dm = _get_dm_reply_chat_id()
            _grp = _get_group_reply_chat_id()
            if _dm:
                channel = f"{_pf}:{_dm}"
            elif _grp:
                channel = f"{_pf}:{_grp}"
            else:
                # 兜底仍拿不到目标 → 显式拒绝，引导模型主动查 ID（不让系统盲猜发错通道）。
                _candidates = _list_contact_candidates()
                return _j({
                    "sent": False,
                    "channel": f"{_pf}:default",
                    "text": text,
                    "error": (
                        "你没有指定发给谁，且本次唤醒也没有明确的回复上下文。"
                        "请显式传 chat_id（oc_xxx 群 / ou_xxx 私聊），或先调 sense_contacts 按名字查到 ID 再发。"
                    ),
                    "candidates": _candidates,
                })

    # ── awaiting_reply 自动策略(在 channel 确定后, mention 解析完) ──
    # wait_minutes=-1 表示模型选了 "auto", 需要根据通道+@情况自动判定
    if wait_minutes == -1:
        is_dm = ":dm:" in channel or channel.split(":")[-1].startswith("ou_")
        mentioned_specific = bool(mention_user_ids)
        if is_dm or mentioned_specific:
            wait_minutes = 5  # 私聊 或 群聊@真人 → 等5min
        else:
            wait_minutes = 0  # 群聊没@真人 → 不等
        logger.info("express_to_human: awaiting_reply auto → %dmin (dm=%s, @human=%s)",
                     wait_minutes, is_dm, mentioned_specific)

    # ── 发送前校验（通道已 100% 确定，此刻执行顺序：先未读消息，再目标通道是否查看过）──
    # 从已解析 channel 反解目标 chat_id（形如 lark:group:oc_xxx → oc_xxx）。
    target_chat_id = ""
    try:
        if channel.count(":") >= 2 and not channel.split(":", 2)[2].strip().lower() == "default":
            target_chat_id = channel.split(":", 2)[2].strip()
    except Exception:
        target_chat_id = ""
    try:
        from domain.lifecycle.communication import check_before_send
        block = check_before_send(text, session_id=session_id, target_chat_id=target_chat_id)
        if block:
            return _j(block)
    except Exception:
        pass

    # ── WeChat (ClawBot) 发送路径 —— 按 channel 前缀分发 ──
    if channel.startswith("wechat:"):
        return _send_wechat_clawbot(channel, text, context, mention_user_ids)

    # ── 语音输出路径（本地 TTS 播放，不依赖飞书）──
    if channel.startswith("voice:"):
        return _send_voice_local(channel, text, context, mention_user_ids)

    # Send via feishu direct API (primary path)
    sent = False
    err = None
    # 分段发送统计（仅飞书工具直发路径会填；其它路径保持 None 兼容老契约）
    segments_sent: int | None = None
    segments_total: int | None = None
    # 私聊路径的默认目标：仅用当前实例自己的回复上下文（DM/group），不读全局 FEISHU_FALLBACK。
    # 全局值跨实例串味（alpha 会拿 zero 的 chat 撞 cross app）。找不到时留空，
    # 由闭包内的 DM 分支按 channel 显式失败（提示模型用 sense_contacts 查 ID）。
    FEISHU_CHAT_ID = _get_dm_reply_chat_id() or _get_group_reply_chat_id()
    try:
        import httpx
        import os

        # 优先从 apps/{instance_id}/config/app.yaml 读飞书凭证（channels.feishu.*）
        # 多实例共享进程时 env 是 Zero 启动时填的，Alpha 不能复用
        app_id = ""
        app_secret = ""
        try:
            from pathlib import Path as _P
            import yaml as _yaml
            from infrastructure.config import get_project_root, get_app_instance_id
            iid = get_app_instance_id()
            if iid:
                cfg_path = get_project_root() / "apps" / iid / "config" / "app.yaml"
                if cfg_path.exists():
                    cfg = _yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
                    channels = cfg.get("channels") or {}
                    feishu = channels.get("feishu") or {} if isinstance(channels, dict) else {}
                    app_id = (feishu.get("app_id") or "").strip() if isinstance(feishu, dict) else ""
                    # app_secret 优先从实例 config/secrets.env 读取（已 load_runtime_dotenv 加载到 env）
                    app_secret = os.getenv("FEISHU_APP_SECRET") or ""
        except Exception:
            pass
        # 兜底 env
        if not app_id:
            app_id = os.getenv("FEISHU_APP_ID") or os.getenv("LARK_APP_ID") or ""
        if not app_secret:
            app_secret = os.getenv("FEISHU_APP_SECRET") or os.getenv("LARK_APP_SECRET") or ""
        if app_id and app_secret:
            logger.info("express_to_human: using feishu credentials app_id=%s (instance=%s)",
                        app_id[:12], _get_instance_id_for_context()[:8])
            _token_url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
            _msg_url = "https://open.feishu.cn/open-apis/im/v1/messages"
            # 飞书 text 消息字符上限（与 FeishuAdapter.capabilities.max_text_length 对齐，
            # 见 interfaces/ingress/feishu.py:96）。超长→分段发送而非静默截断。
            FEISHU_MAX_LEN = 4000

            # Outer-scope state shared with _send_feishu_direct — values needed
            # after run_async returns for the fan-out path. Closure-internal
            # assignment pattern (without ``nonlocal``) used to silently fail
            # NameError when fan-out code read them.
            target_chat = ""
            routed_id = ""
            send_text = text
            # 真实的 chat 类型（dm/group），由 _send_feishu_direct 内部 _真实_ 决定后写入。
            # 写 conversation_log 必须用这个，不能凭 channel 字符串推断——
            # 因为 line 626-630 会把 lark:dm:oc_xxx 重写成 lark:group:oc_xxx，
            # 导致 conversation_log 把私聊全部误标为 group（污染 social_context）。
            real_chat_type = "dm"

            async def _send_segments_in_loop(
                segments: list[str],
                post_one,
                asyncio_mod,
            ) -> tuple[bool, str | None, int, int]:
                """循环发送多段文本：保留已成功段，部分成功即 True。

                Args:
                    segments: 已切好段的文本列表（split_text_for_send 产物）。
                    post_one: async callable(seg) -> (ok: bool, err: str | None)。
                    asyncio_mod: asyncio 模块（用于 sleep；外层注入避免重复 import 查找）。

                Returns:
                    (any_sent, first_err, sent_count, total)
                """
                sent_count = 0
                first_err: str | None = None
                for idx, seg in enumerate(segments):
                    try:
                        ok, e = await post_one(seg)
                    except Exception as exc:
                        # 单段异常：记录但不中断，继续后续段（保留已发段）
                        logger.warning(
                            "express_to_human feishu: segment %d/%d exception: %s",
                            idx + 1, len(segments), exc,
                        )
                        ok, e = False, str(exc)
                    if ok:
                        sent_count += 1
                    elif first_err is None:
                        first_err = e
                    # 段间适度等待，避免触发飞书发消息 QPS 限制；末段不 sleep
                    if idx < len(segments) - 1:
                        await asyncio_mod.sleep(0.2)
                return sent_count > 0, first_err, sent_count, len(segments)

            async def _send_feishu_direct():
                """发送飞书消息。超长文本自动分段循环发送。

                返回 tuple[Any, str | None, int, int]：
                    (ok_or_partial, err, segments_sent, segments_total)
                - ok_or_partial: bool —— 至少一段成功即 True（保留已发段语义）
                - err: 首个失败原因（全成功为 None）
                - segments_sent/total: 给外层 fan-out / JSON 透出用
                """
                # routed_id 在 DM 分支赋值，group 分支读不到会 UnboundLocalError —— 在
                # group 分支失败时 _explain_feishu_send_failure 读 routed_id 就会炸
                # （单测 hit 不到这条路径）。一并加入 nonlocal。
                nonlocal target_chat, send_text, channel, routed_id, real_chat_type
                import asyncio as _aio
                from interfaces.ingress.text_segmenter import split_text_for_send

                async with httpx.AsyncClient(timeout=30) as c:
                    tr = await c.post(_token_url, json={"app_id": app_id, "app_secret": app_secret})
                    token = tr.json().get("tenant_access_token", "")
                    if not token:
                        return False, "failed to get token", 0, 0
                    # 由 channel 决定发送目标（channel 前缀 feishu: 或历史遗留 lark: 均识别）：
                    #   - <pf>:group:<chat_id> 或 <pf>:<group_chat_id>（_GROUP_REPLY_CHAT_ID 命中）→ 群聊
                    #   - <pf>:dm:<open_id (ou_)>                                  → 私聊（按 open_id）
                    #   - <pf>:dm:<chat_id (oc_)>  ← 模型经常误写 dm 前缀但 target 是群 chat_id
                    #                                                            → 重写成 group 路径，按 chat_id 发到群
                    #   - <pf>:<chat_id (oc_)>                                     → 群聊（按 chat_id 直发）
                    #   - 兜底：FEISHU_ALLOWED_USERS 第一个 open_id                → 私聊
                    _grp_ctx = _get_group_reply_chat_id()
                    is_group_channel = (
                        _channel_has_prefix(channel, "group:")
                        or (bool(_grp_ctx) and _channel_has_raw_suffix(channel, f"{_grp_ctx}"))
                    )
                    # Backwards-compat: model sometimes writes "<pf>:dm:oc_xxx" — read DM prefix
                    # but the id is actually a group chat_id (oc_). Detect and rewrite to group.
                    if not is_group_channel and _channel_has_prefix(channel, "dm:"):
                        tail = _strip_feishu_prefix(channel, kind="dm:").strip()
                        if tail.startswith("oc_"):
                            channel = f"feishu:group:{tail}"
                            is_group_channel = True

                    # 超长文本分段（飞书 text 消息上限，与 FeishuAdapter.capabilities.max_text_length=4000 对齐）。
                    # send_text 已含 mention 前缀（群聊）或 cleaned text（私聊）；先解析出实际要发的文本。
                    if is_group_channel:
                        # 群聊路径：从 channel 解析 group chat_id，不用 FEISHU_CHAT_ID
                        # （那个可能被 DM context 污染）
                        if _channel_has_prefix(channel, "group:"):
                            target_chat = _strip_feishu_prefix(channel, kind="group:").strip()
                        else:
                            target_chat = _grp_ctx
                        # ⚠️ 真实 chat_type：只有 target_chat 等于配置的 group context 时才算群。
                        # 否则可能是 <pf>:dm:oc_<私聊 conv> 被 rewrite 的——那种情形下
                        # 上面 send 走了 chat_id 接口（API 要求），但语义仍是私聊，
                        # 不应记 group（否则 conversation_log chat_type 自相矛盾，
                        # social_context 会把私聊当群）。
                        real_chat_type = "group" if target_chat == (_grp_ctx or "") else "dm"
                        # mention_user_ids：默认 prepend 到 text 前；
                        # 但若 text 里已经含 <at user_id="ou_xxx"></at> 标签，
                        # 跳过那些 ou_（auto-mention 已经替换过，避免重复）
                        ats_already_in_text = set()
                        import re as _re_at
                        for m_ou in _re_at.findall(
                            r'<at user_id="(ou_[a-zA-Z0-9_-]+)"></at>', text
                        ):
                            ats_already_in_text.add(m_ou)
                        prepend_ids = [ou for ou in mention_user_ids if ou not in ats_already_in_text]
                        if prepend_ids:
                            mention_prefix = " ".join(
                                f'<at user_id="{ou}"></at>' for ou in prepend_ids
                            ) + " "
                            send_text = mention_prefix + text
                        else:
                            send_text = text

                        async def _post_one(seg: str) -> tuple[bool, str | None]:
                            payload: dict = {"text": seg}
                            # mentioned_list 仅随首段发送——避免分 N 段时群里把人 @ N 次。
                            # 首段已含 <at> 标签前缀（mention_prefix），mentioned_list 与之配套。
                            if mention_user_ids and _grp_seg_idx[0] == 0:
                                payload["mentioned_list"] = mention_user_ids
                            _grp_seg_idx[0] += 1
                            msg_resp = await c.post(
                                _msg_url,
                                headers={"Authorization": f"Bearer {token}"},
                                params={"receive_id_type": "chat_id"},
                                json={
                                    "receive_id": target_chat,
                                    "msg_type": "text",
                                    "content": json.dumps(payload),
                                },
                            )
                            rd = msg_resp.json()
                            if rd.get("code") == 0:
                                return True, None
                            return False, _explain_feishu_send_failure(rd, channel, target_chat)

                        # mention 前缀只在首段附加（前缀在 send_text 开头，切分时随首段）；
                        # mentioned_list 也只在首段发，避免群里多次 @。
                        _grp_seg_idx = [0]  # 闭包计数器：标记当前段是否首段
                        segments = split_text_for_send(send_text, FEISHU_MAX_LEN)
                        return await _send_segments_in_loop(segments, _post_one, _aio)
                    else:
                        # 私聊路径：接受 open_id (ou_) 或 chat_id (oc_) 形式
                        target_chat = FEISHU_CHAT_ID
                        routed_id = ""
                        if _channel_has_prefix(channel, "dm:"):
                            routed_id = _strip_feishu_prefix(channel, kind="dm:").strip()
                        elif target_chat:
                            routed_id = target_chat
                        # 找不到目标：直接失败，把根因和下一步交给模型。
                        # 不再偷读全局 FEISHU_ALLOWED_USERS（跨实例串味）。
                        if not routed_id:
                            return False, (
                                "私聊发送需要 ou_xxx(open_id) 或 oc_xxx(chat_id)，但你没填且无 DM 上下文。"
                                "用 sense_contacts 查看联系人拿到 ou_xxx 后，"
                                "调 express_to_human(text, chat_id='ou_xxx') 再发。"
                            ), 0, 0
                        routed_type = _feishu_receive_id_type(routed_id)
                        # DM path: strip 私聊不支持的 <at> 标签，避免显示多余 "@"
                        # 私聊本身是一对一，没必要 @ 谁。把 <at user_id=".."></at> 替换为 @<name> 或 删除
                        dm_clean_text = text
                        try:
                            import re as _re_dm_at
                            from domain.contacts import lookup_name
                            def _strip_at(m):
                                ou = m.group(1)
                                n = lookup_name("feishu", ou)
                                return f"@{n}" if n else ""  # 命中显示名字，否则删干净
                            dm_clean_text = _re_dm_at.sub(
                                r'<at user_id="(ou_[a-zA-Z0-9_-]+)"></at>', _strip_at, dm_clean_text
                            )
                        except Exception:
                            pass

                        async def _post_one_dm(seg: str) -> tuple[bool, str | None]:
                            msg_resp = await c.post(
                                _msg_url,
                                headers={"Authorization": f"Bearer {token}"},
                                params={"receive_id_type": routed_type},
                                json={
                                    "receive_id": routed_id,
                                    "msg_type": "text",
                                    "content": json.dumps({"text": seg}),
                                },
                            )
                            rd = msg_resp.json()
                            if rd.get("code") == 0:
                                return True, None
                            return False, _explain_feishu_send_failure(rd, channel, routed_id)

                        segments = split_text_for_send(dm_clean_text, FEISHU_MAX_LEN)
                        return await _send_segments_in_loop(segments, _post_one_dm, _aio)

            from interfaces.tools.interrupt import is_interrupted
            if not is_interrupted():
                from interfaces.tools.async_utils import run_async
                # 提前算好 group vs dm，与 _send_feishu_direct 内的 is_group_channel
                # 完全一致（否则外层以为非 group，fan-out 不触发，sibling 收不到）。
                _grp_ctx = _get_group_reply_chat_id() or ""
                _is_group_send = (
                    _channel_has_prefix(channel, "group:")
                    or (bool(_grp_ctx) and _channel_has_raw_suffix(channel, f"{_grp_ctx}"))
                )
                result = run_async(_send_feishu_direct())
                if isinstance(result, tuple) and len(result) == 4 and result[0]:
                    sent = True
                    seg_sent, seg_total = result[2], result[3]
                    segments_sent, segments_total = seg_sent, seg_total
                    # 部分送达：保留已成功段，但把首失败原因透出给模型
                    if result[1] and seg_sent < seg_total:
                        err = f"部分送达 {seg_sent}/{seg_total} 段：{result[1]}"
                        logger.warning("express_to_human: partial send %d/%d: %s",
                                       seg_sent, seg_total, result[1])
                    else:
                        err = None
                    logger.info("express_to_human: sent OK (segments=%d/%d)",
                                seg_sent, seg_total)
                    # Fan-out 到群消息聚合库 + fan-out 给其他实例事件。
                    # ⚠️ 必须用 real_chat_type=='group'（与 conversation_log 写入同源），
                    # 不能用 _is_group_send——后者在 oc_ 私聊被 rewrite 成 lark:group:oc_xxx
                    # 时也是 True，会把私聊误广播给兄弟实例 + 写进群聚合库（隐私泄漏）。
                    if real_chat_type == "group":
                        try:
                            from domain.conversations import publish_chat_message
                            from infrastructure.config import get_app_instance_id, get_instance_display_name
                            sender_iid = get_app_instance_id() or ""
                            sender_display = get_instance_display_name() or "Zero"
                            # 原始 text（去掉 mention 前缀的部分，因为飞书已渲染）
                            # 用 send_text 包含 <at> 标签，fan-out 不含 bother 标签
                            from domain.contacts import lookup_name, any_id_is_bot
                            import re as _re
                            _at_pat = r'<at user_id="(ou_[a-zA-Z0-9_-]+)"></at>'
                            plain_text = _re.sub(
                                _at_pat,
                                lambda m: f"@{lookup_name('feishu', m.group(1)) or '网友'}",
                                send_text,
                            )
                            # 去重决策：正文若 @ 到了本群机器人（兄弟实例或第三方
                            # bot），飞书会把消息单独推给被@的机器人——已送达。
                            # 本侧再广播就是重复（receiver 收两遍）。提取全部被 @ 的
                            # open_id，任一对应 kind=bot 联系人→本次不广播。
                            _mentioned_ids = _re.findall(_at_pat, send_text)
                            _skip_broadcast = bool(_mentioned_ids) and any_id_is_bot("feishu", _mentioned_ids)
                            publish_chat_message(
                                chat_id=target_chat,
                                sender_id=sender_iid,
                                sender_name=sender_display,
                                text=plain_text,
                                msg_id="",
                                sender_kind="bot",
                                broadcast=not _skip_broadcast,
                            )
                        except Exception as exc:
                            # 升级到 warning 之前是 debug——历史上 fan_out 静默失败过
                            # （sibling 没收到消息、模型误以为已通知）。
                            logger.warning(
                                "express_to_human: group fan-out failed: %s", exc,
                                exc_info=True,
                            )
                elif isinstance(result, tuple) and len(result) == 4:
                    err = result[1] or "feishu direct send failed"
                    segments_sent, segments_total = result[2], result[3]
                    logger.warning("express_to_human: send failed: %s", err)
                else:
                    # 兜底：旧式 2 元组或异常形态（防御性，正常不会到这）
                    if isinstance(result, tuple) and result[0]:
                        sent = True
                        err = None
                    elif isinstance(result, tuple):
                        err = result[1] if len(result) > 1 else "feishu direct send failed"
                        logger.warning("express_to_human: send failed: %s", err)
        else:
            err = "no FEISHU_APP_ID/SECRET in env"
            logger.warning("express_to_human: %s", err)
    except Exception as e2:
        err = f"feishu send error: {e2}"
        logger.warning("express_to_human: send exception: %s", e2)

    vitals.consume_energy(ENERGY_COST_PER_CALL)

    # 发送成功 → 记录到对话日志 + 排队"等回复"事件
    if sent:
        # 三态收条-态 3: 发送成功 → 撤掉 ⚙️(消息本身即是回应)
        try:
            from application.ingress.reaction_state import clear_all_reactions_sync
            clear_all_reactions_sync()
        except Exception as _ce:
            logger.debug("clear_all_reactions failed: %s", _ce)
        try:
            from domain.lifecycle.conversation_log import log_conversation
            parts = channel.split(":")
            _raw_pf = parts[0] if parts else "feishu"
            # 归一平台前缀：历史存量可能写 lark/feishu，统一记 feishu。
            platform = "feishu" if _raw_pf in ("feishu", "lark") else _raw_pf
            # ⚠️ conversation_id 与 chat_type 必须从 _send_feishu_direct 闭包暴露出的
            # real_chat_type + target_chat/routed_id 取（它们是 send 真正用的值）。
            # 早期版本凭 channel 字符串推断 chat_type，但 line 626-630 会把 `lark:dm:oc_xxx`
            # （真实是私聊，只是飞书 conversation_id 碰巧 oc_ 开头）rewrite 成 group，
            # 导致 conversation_log 把私聊全误标 group → social_context 把私聊当群。
            if target_chat:
                conv_id = target_chat
            elif routed_id:
                conv_id = routed_id
            elif len(parts) >= 3 and parts[1] in ("group", "dm"):
                conv_id = parts[2]
            elif len(parts) >= 2:
                conv_id = parts[1]
            else:
                conv_id = channel
            chat_type = real_chat_type  # 真实 dm/group，由 send 路径决定
            # conversation_log 是 snippet 数据源，截 text 防止 prompt 膨胀
            # out 也带 sender_name（实例的 display_name），让 chat_stream 渲染自然
            # e.g. "Zero：xxx" 而不是 "你：xxx"
            out_sender = ""
            try:
                from infrastructure.config import get_instance_display_name
                out_sender = (get_instance_display_name() or "").strip()
            except Exception:
                pass
            if not out_sender:
                out_sender = "你"  # 退化兜底

            # conversation_log 只存可读文本（不含 <at user_id="ou_xxx"></at>)
            # 用 contacts 反查 mentioned open_id → @<name>；未命中的 ou 用 "用户短码"
            log_text = text[:300]
            try:
                import re as _re_at_strip
                from domain.contacts import lookup_name
                def _replace_at_tag(m: _re_at_strip.Match) -> str:
                    ou = m.group(1)
                    n = lookup_name("feishu", ou)
                    if n:
                        return f"@{n}"
                    short = ou[3:11] if len(ou) > 11 else ou
                    return f"@用户{short}"
                log_text = _re_at_strip.sub(r'<at user_id="(ou_[a-zA-Z0-9_-]+)"></at>', _replace_at_tag, log_text)
            except Exception:
                pass

            log_conversation(
                platform=platform,
                conversation_id=conv_id,
                chat_type=chat_type,
                direction="out",
                text=log_text,
                sender_name=out_sender,
            )
        except Exception:
            pass
        try:
            from domain.lifecycle.runtime_context import set_current_conversation_id
            set_current_conversation_id(channel.split(":")[-1] if ":" in channel else channel)
        except Exception:
            pass
        try:
            if wait_minutes > 0:
                # ⚠ 2026-06-24 引入了 find_alarms_by_filter 这个 import，但它
                # 在 alarms.py 里从未定义（死 import）→ 整行 ImportError 被
                # 下方 except 静默吞掉，导致 cancel_alarms_by_filter + set_alarm
                # 自 6/24 起从未执行 → awaiting_reply 事件再也没被设上。
                # DEDUP 用的是 events.list_recent_events（920 行），与此 import 无关，删除即可。
                from domain.lifecycle.alarms import cancel_alarms_by_filter, set_alarm
                from domain.lifecycle import clock as _clock
                # 同通道精确清旧闹钟：发到群 A 时只清群 A 的 awaiting_reply，
                # 保留群 B 等待（之前用 cancel_alarms_by_kind 会全局清，跨通道误取消）
                cancel_alarms_by_filter(
                    "awaiting_reply",
                    payload_filter={"channel": channel},
                )

                # ⚠ 2026-06-24 修复:多消息不重复催的事件 DEDUP。
                # 真人习惯:正在等回复时不会再说"我没收到回复"——会接着等。
                # 历史现象:用户连发 3 条消息,系统在同通道设了 3 个 awaiting_reply
                # 闹钟,每个 fire 各自叫醒一次 agent —— 后续 wake 1177 醒来一次性
                # 收到 3 条 await 事件,显得"卡在处理多事件"。
                # 修法:SET 新 timer 前,先看 events 表有没有同通道的未消费 awaiting_reply。
                # 有 → 不再 SET(沿用老的),只清掉过长 fire_at 让它顶 15min 即可;
                #      这样不会拉升事件队列里 awaiting 数量。
                # 无 → 真的需要新 await,正常 SET。
                should_set_new = True
                try:
                    from domain.lifecycle.events import list_recent_events
                    recent = list_recent_events(hours=1, include_consumed=False, limit=50) or []
                    for ev in recent:
                        if (ev.get("kind") == "awaiting_reply"
                                and (ev.get("payload") or {}).get("channel") == channel
                                and ev.get("consumed_at") in (None, "")):
                            should_set_new = False
                            logger.info(
                                "express_to_human: channel=%s 仍在 awaiting_reply(event_id=%s) "
                                "— 不重复设新 timer(沿用原 await 队列)",
                                channel, ev.get("event_id"),
                            )
                            break
                except Exception as exc:
                    logger.debug("awaiting dedup check failed (will still SET): %s", exc)

                if should_set_new:
                    set_alarm(
                        event_kind="awaiting_reply",
                        fire_at=(_clock.now_dt() + timedelta(minutes=wait_minutes)).isoformat(timespec="seconds"),
                        payload={
                            "last_sent_text": text[:200],
                            "channel": channel,
                            "hint": "或许该去做自己的事了？看看计划或笔记里有没有想继续的。",
                        },
                    )
        except Exception as exc:
            # awaiting_reply 的设立是 express_to_human 的关键副作用，失败必须有日志
            # —— 历史上这里是 `except: pass`，曾把 ImportError 静默吞掉一周才被发现。
            logger.warning("express_to_human: set awaiting_reply failed: %s", exc, exc_info=True)

    try:
        from domain.todos import record_session_human_reply

        record_session_human_reply(
            context.get("session_id"),
            sent=sent,
            text=text,
            channel=channel,
            error=err,
        )
    except Exception:
        pass

    if sent:
        if segments_total and segments_total > 1:
            if segments_sent == segments_total:
                note = f"已送达（channel={channel}，共 {segments_total} 段）。"
            else:
                note = (f"已部分送达（channel={channel}，{segments_sent}/{segments_total} 段"
                        f"{f'，失败：{err}' if err else ''}）。")
        else:
            note = f"已送达（channel={channel}）。"
    else:
        note = f"未送达（channel={channel}, error={err}）。"
    note += " 沉默是你的默认状态 — 没必要每件事都发言。"

    return _j({
        "sent": sent,
        "channel": channel,
        "text_length": len(text),
        "error": err,
        "note": note,
        "segments_sent": segments_sent,
        "segments_total": segments_total,
    })


registry.register(
    name="express_to_human",
    toolset="actions",
    schema={
        "name": "express_to_human",
        "description": (
            "向人类用户表达——可以是回应、分享、求助、状态同步或关心。"
            "这是'表达'，不是'回复'——你有权选择说什么、何时说、对谁说。\n\n"
            "参数：\n"
            "- text: 必填。要说的话。\n"
            "- chat_id: 飞书对话 ID（oc_xxx）。**留空 = 回复当前事件来源 chat**。"
            "可在一个 turn 内多次调用、指定不同 chat_id 实现多目标广播/转告。\n"
            "- channels: 多通道数组（如语音+飞书同时发）。同一句话说一次即可，不用调两次。\n"
            "- kind: 'group' 或 'dm'。**默认按当前 wake 推断**，仅在你需要跨类型（如把私聊内容转告到群里）时显式指定。\n\n"
            "你不需要每次都回复。可以沉默做事、可以用工具后再表达、可以等到信息齐全再统一同步。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要说的话"},
                "chat_id": {
                    "type": "string",
                    "description": (
                        "目标对话 ID（飞书 oc_xxx）。"
                        "留空 = 回复当前事件来源 chat。"
                        "用于跨对话通知、转告、多目标广播。"
                    ),
                },
                "kind": {
                    "type": "string",
                    "enum": ["group", "dm"],
                    "description": "对话类型。留空时按 wake_reason 推断（group_message→group，否则 dm）。",
                },
                "channel": {
                    "type": "string",
                    "description": "（兼容）完整 channel 字符串如 'lark:group:oc_xxx'。优先级低于 chat_id。",
                },
                "channels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "（多通道）一次发多个通道，同一 text 依次发送。"
                        "如语音场景：['voice:speaker', 'feishu:dm:ou_xxx'] = 语音说一句 + 飞书留文字版"
                        "（URL/代码等技术细节语音读不了，配一个飞书通道）。"
                        "任一通道被拦截（有未读消息需重写）会中止剩余通道。"
                    ),
                },
                "mention_user_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "（可选）@ 的 user open_id 列表（ou_xxx）。"
                        "用于群聊里主动 @ 其他用户或 bot。text 里不需要写 @xxx；"
                        "系统会自动在消息前缀加 <at> 标签。"
                        "另一简单用法：text 里直接写 '@displayName'（如 '@zero'），系统自动转 <at> 标签。"
                    ),
                },
                "wait_reply": {
                    "type": "string",
                    "description": (
                        "发出消息后是否等待真人回复。awaiting_reply 闹钟到期没收到回复会叫醒你做别的。\n"
                        "可选值:\n"
                        "  - 'auto' (默认): 系统自动判断——私聊等5min; 群聊@了真人等5min; 群聊没@真人不等。\n"
                        "  - 'none': 明确不等(通知/同步/闲聊类消息)。\n"
                        "  - 数字(1-15): 明确等N分钟。\n"
                        "判断规则: 你发消息是否期待对方回应?\n"
                        "  期待→填 'auto' 或具体分钟数; 不期待(同步/报告/通知)→填 'none'。"
                    ),
                    "default": "auto",
                },
            },
            "required": ["text"],
        },
    },
    handler=_handle_express_to_human,
    check_fn=lambda: True,
    emoji="💬",
)



# ──────────────────────────────── write_diary ────────────────────────────────

def _handle_write_diary(args: Dict[str, Any], **_) -> str:
    text = args.get("text") or ""
    mode = (args.get("mode") or "replace").strip()
    if mode not in ("replace", "append"):
        mode = "replace"
    if mode == "replace" and not text.strip():
        return registry.tool_error("text is required (replace mode 需要完整正文)")

    snap = vitals.consume_energy(ENERGY_COST_PER_CALL)
    entities = args.get("entities")
    if entities and isinstance(entities, list):
        _diary(text, entities=entities, mode=mode)
    else:
        _diary(text, mode=mode)

    note = f"今日日记已{'覆写' if mode == 'replace' else '追加一段'}，精力消耗 {ENERGY_COST_PER_CALL:.0f}"
    return _j({
        "ok": True,
        "mode": mode,
        "note": note,
        "energy": round(snap.energy, 1),
    })


registry.register(
    name="write_diary",
    toolset="actions",
    schema={
        "name": "write_diary",
        "description": (
            "写/编辑今日日记 (diary/YYYY-MM-DD.md, 按天分文件, 只给自己看不发任何人)。"
            "默认 replace 模式: 用 text 整体替换今日日记正文, 就像编辑一份文件——"
            "你能任意编辑/追加/删除段落, 由你自己负责组织全文。\n"
            "工作流: 先 sense_memory 读今日日记 → 局部改 → write_diary(text=改后全文, mode=replace)。\n"
            "晚间复盘(self_review)时必写一段连贯的今日总结(今天做了什么/学到什么/明天重点)。\n"
            "mode=append: 追加一条 `## 时间戳` 碎片段, 用于快速插入不动既有内容。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "日记正文。replace 模式下是今日完整正文; append 模式下是一条碎片"},
                "mode": {
                    "type": "string",
                    "enum": ["replace", "append"],
                    "default": "replace",
                    "description": "replace=覆写今日正文(编辑/重写/删除从改全文自然实现); append=追加一条碎片段",
                },
                "entities": {"type": "array", "items": {"type": "string"}, "description": "关联的实体"},
            },
            "required": ["text", "mode"],
        },
    },
    handler=_handle_write_diary,
    check_fn=lambda: True,
    emoji="📔",
    schema_visible=False,  # V6 工具精简: 降级
)


# ──────────────────────────────── record_thought ────────────────────────────────

_THOUGHT_CALL_COUNT = 0
_THOUGHT_MAX_PER_SESSION = 15


def _handle_record_thought(args: Dict[str, Any], **_) -> str:
    global _THOUGHT_CALL_COUNT
    _THOUGHT_CALL_COUNT += 1
    text = (args.get("text") or "").strip()
    tag = (args.get("tag") or "").strip()
    kind = (args.get("kind") or "status").strip().lower()
    if not text:
        return registry.tool_error("text is required")

    # kind 归一化
    valid_kinds = {"idea", "doubt", "block", "warning", "status"}
    if kind not in valid_kinds:
        kind = "status"

    if kind != "status" and _THOUGHT_CALL_COUNT > _THOUGHT_MAX_PER_SESSION:
        snap = vitals.get_current_vitals()
        return _j({
            "ok": False,
            "note": (
                f"本轮已记录 {_THOUGHT_CALL_COUNT} 次思绪，够了。"
                f"精力 {snap.energy:.0f}。请现在决定：rest() 休息，或 express_to_human() 联系用户。"
            ),
            "energy": round(snap.energy, 1),
            "calls_this_session": _THOUGHT_CALL_COUNT,
        })

    snap = vitals.consume_energy(ENERGY_COST_PER_CALL)
    # kind 进入 tag（保持 tag 作为自由标签的可选性），同时同步到 INSIGHTS.md
    effective_tag = tag if tag else kind
    entities = args.get("entities")
    if entities and isinstance(entities, list):
        _record(text, tag=effective_tag, entities=entities)
    else:
        _record(text, tag=effective_tag)

    # 非 status 的 kind 同步写 INSIGHTS.md，晚上 self_review 用 sense_insights 拾起来
    if kind != "status":
        try:
            from domain.memory.memory.consciousness.runtime import append_insight
            append_insight(kind=kind, text=text, tag=tag, entities=entities or [])
        except Exception:
            pass

    remaining = _THOUGHT_MAX_PER_SESSION - _THOUGHT_CALL_COUNT
    hint = ""
    kind_hint = ""
    if kind == "block":
        kind_hint = " [block 已记入 INSIGHTS，今晚 self_review 会拾起来]"
    elif kind == "doubt":
        kind_hint = " [doubt 已记入 INSIGHTS，self_review 时必答]"
    elif kind == "idea":
        kind_hint = " [idea 已记入 INSIGHTS，self_review 时验证真伪]"
    elif kind == "warning":
        kind_hint = " [warning 已记入 INSIGHTS，明日 morning_plan 会回头提醒]"
    if remaining <= 0:
        hint = " 已达上限，现在请调用 rest(until=...) 或 rest(reuse=...) 进入休息。"
    elif remaining <= 3:
        hint = f" 还可记录{remaining}次，建议尽快调用 rest(until=...) 或 rest(reuse=...) 休息。"

    return _j({
        "ok": True,
        "note": f"思绪已留下（kind={kind}），精力消耗 {ENERGY_COST_PER_CALL:.0f}。{kind_hint}{hint}",
        "energy": round(snap.energy, 1),
        "thoughts_remaining": remaining,
        "kind": kind,
    })


registry.register(
    name="record_thought",
    toolset="actions",
    schema={
        "name": "record_thought",
        "description": (
            "留思绪给未来——既是穿越睡眠的连续性载体（kind=status），也是过程中随手捕捉的灵感/卡点/质疑（kind=idea/doubt/block/warning）。\n"
            "退出前至少调一次 kind=status；遇到 insight 立刻调，不要等晚上。\n\n"
            "kind 语义：\n"
            "- status: 退出前留给睡醒的自己的当前状态/上下文/下一步。穿越睡眠。\n"
            "- idea: 闪现的洞察或猜测（不一定对，记下来晚上自审）。\n"
            "- doubt: 对当前做法/假设的质疑。\n"
            "- block: 卡点——具体卡在哪、缺什么、可能解法。\n"
            "- warning: 反复出现的模式或今天违反了某条规则——警觉信号。\n\n"
            "非 status 类的 kind 会同步写入 INSIGHTS.md，晚上 self_review 用 sense_insights 拾起来。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "记的具体内容。一句话，不是流水账。"},
                "kind": {
                    "type": "string",
                    "description": "思绪分类：status / idea / doubt / block / warning。默认 status。",
                    "enum": ["status", "idea", "doubt", "block", "warning"],
                },
                "tag":  {"type": "string", "description": "可选标签——给 status 自由标签，其他 kind 已隐含 tag"},
                "entities": {"type": "array", "items": {"type": "string"}, "description": "关联的实体，用于后续检索。如股票代码、工具名、概念名等"},
            },
            "required": ["text"],
        },
    },
    handler=_handle_record_thought,
    check_fn=lambda: True,
    emoji="💭",
)


# ──────────────────────────────── remember_him ────────────────────────────────

def _handle_remember_him(args: Dict[str, Any], **_) -> str:
    """[退役中] 记录对主用户/重要联系人的观察。

    原写 HIM.md。HIM.md 已迁移到 contacts.about 字段(自由文本画像)。
    此 handler 保留作为兼容入口——历史 session 的 tool_call 重放仍可用,
    但 schema 不再注入(prompt 里看不到), 注释里明确指向推荐路径:
    用 set_entity_profile 或 record_thought,画像信息走 contacts。
    """
    text = (args.get("text") or "").strip()
    if not text:
        return registry.tool_error("text is required")
    # 找主 contact(优先 zhp/张浩普/蓝先生 关键字, fallback 第一个 human + platform_id)
    from domain.contacts.store import list_contacts, update_contact
    KEYWORDS = ("zhp", "zhanghaopu", "张浩普", "蓝先生", "zhang", "haopu")
    primary = None
    for c in list_contacts(include_blocked=False):
        n = (c.get("name") or "").lower()
        if any(k in n for k in KEYWORDS):
            primary = c
            break
    if primary is None:
        for c in list_contacts(include_blocked=False):
            if c.get("kind") == "human" and c.get("platform_ids"):
                primary = c
                break
    if primary is None:
        # 写入失败兜底:把内容 record_thought(模型仍能在记忆里搜到这次观察)
        from domain.memory.memory.consciousness.runtime import record_thought as _rt
        _rt(text, tag="observation")
        return _j({"ok": True, "_note": "未找到合适的 contact 写入, "
                    "已先用 record_thought 兜底。建议改用 record_thought 或 set_entity_profile。",
                    "date": ""})
    # 追加到 about(若已有内容则加一行)
    existing = primary.get("about") or ""
    new_about = (existing + "\n\n" if existing else "") + text
    update_contact(primary["id"], about=new_about)
    from domain.lifecycle.clock import now_iso
    return _j({
        "ok": True,
        "note": f"已记录到 contact {primary['id'][:8]} ({primary.get('name')}) 的 about 画像。",
        "_deprecated_hint": "remember_him 已退役, 改用 set_entity_profile 或 record_thought。"
                            " 画像自动落 contacts.about。",
        "date": now_iso(),
    })



# ──────────────────────────────── update_scratchpad ────────────────────────────────

def _handle_update_scratchpad(args: Dict[str, Any], **_) -> str:
    text = (args.get("text") or "").strip()
    mode = (args.get("mode") or "append").strip()
    if not text:
        return registry.tool_error("text is required")
    if mode not in ("append", "replace"):
        return registry.tool_error("mode must be 'append' or 'replace'")

    # 写入纪律提示:SCRATCHPAD 同时 task 段超 2 应提示收敛
    # (不阻止写入, 返警告让模型自觉)
    warning = ""
    try:
        from pathlib import Path as _P
        from domain.memory.memory.consciousness.runtime import _get_runtime_home
        sp_path = _get_runtime_home() / "memories" / "SCRATCHPAD.md"
        if sp_path.exists():
            cur = sp_path.read_text(encoding="utf-8")
            cur_tasks = [L for L in cur.split("\n") if L.startswith("## ")]
            if len(cur_tasks) >= 3:
                warning = (
                    f" ⚠ 当前草稿已有 {len(cur_tasks)} 个并行任务段(>2),"
                    "建议先调 memory_hygiene 收敛,或主动把旧任务 done。"
                )
    except Exception:
        pass

    snap = vitals.consume_energy(ENERGY_COST_PER_CALL)
    _scratchpad(text, mode=mode)

    return _j({
        "ok": True,
        "mode": mode,
        "note": f"草稿本已更新（{mode}）{warning}",
        "energy": round(snap.energy, 1),
    })


registry.register(
    name="update_scratchpad",
    toolset="actions",
    schema={
        "name": "update_scratchpad",
        "description": (
            "更新你的草稿本——记录你在研究什么、想做什么、最近对什么感兴趣。"
            "mode='append' 追加一条，mode='replace' 整体替换。"
            "没事干的时候翻翻草稿本，看看有没有想继续琢磨的事。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "内容"},
                "mode": {"type": "string", "description": "append 或 replace", "enum": ["append", "replace"]},
            },
            "required": ["text"],
        },
    },
    handler=_handle_update_scratchpad,
    check_fn=lambda: True,
    emoji="📋",
    schema_visible=False,  # V6 工具精简: 降级
)


# ──────────────────────────────── manage_work (兼容别名) ────────────────────────────────

def _handle_manage_work(args: Dict[str, Any], **_) -> str:
    """已统一到 todo 工具，此为兼容入口。

    add → todo(create), start → todo(start), complete → todo(done), remove → todo(cancel)
    """
    action = (args.get("action") or "add").strip().lower()
    text = (args.get("text") or "").strip()
    priority = (args.get("priority") or "中").strip()

    snap = vitals.consume_energy(ENERGY_COST_PER_CALL)
    energy = round(snap.energy, 1)

    if action == "add":
        if not text:
            return registry.tool_error("text is required for add")
        from domain.todos.crud import create_task
        _prio_map = {"高": "high", "中": "medium", "低": "low"}
        result = create_task(
            title=text, description="", priority=_prio_map.get(priority, "medium"),
            status="planned", source="manual",
        )
        return _j({"ok": result.get("ok", False), "action": "add", "text": text,
                    "todo_id": result.get("task", {}).get("id", ""),
                    "energy": energy,
                    "_note": "此工具已统一为 todo(action='create')，请改用 todo"})

    if not text:
        return registry.tool_error("text (match keyword) is required for start/complete/remove")

    from domain.todos.crud import list_tasks, update_task
    matching = [t for t in list_tasks(status_filter="planned") + list_tasks(status_filter="in_progress")
                if text in (t.get("title") or "") or text in (t.get("description") or "")]
    if not matching:
        return _j({"ok": False, "action": action, "text": text, "reason": "未找到匹配的待办",
                    "energy": energy})

    target = matching[0]
    tid = target["id"]
    _status_map = {"start": "in_progress", "complete": "done", "remove": "cancelled"}
    ok = update_task(tid, status=_status_map.get(action, "")).get("ok", False)
    _todo_action_map = {"start": "start", "complete": "done", "remove": "cancel"}
    return _j({"ok": ok, "action": action, "text": text, "todo_id": tid,
                "energy": energy,
                "_note": f"此工具已统一为 todo(action='{_todo_action_map.get(action)}', todo_id=...)，请改用 todo"})



# ──────────────────────────────── manage_goals ────────────────────────────────

def _handle_manage_goals(args: Dict[str, Any], **_) -> str:
    """[退役中] 管理目标列表。原写 GOALS.md, 现转发到 todos 表:type='goal'。

    action 映射:
      review → sense_todos(type='goal')
      add    → todo(action='create', type='goal', title=text)
      complete → todo(action='done')  by 模糊匹配
      abandon → todo(action='cancel')
    """
    action = (args.get("action") or "review").strip().lower()
    text = (args.get("text") or "").strip()
    description = (args.get("description") or "").strip()
    priority = (args.get("priority") or "中").strip()

    if action not in ("add", "complete", "abandon", "review"):
        return registry.tool_error("action must be add/complete/abandon/review")

    snap = vitals.consume_energy(ENERGY_COST_PER_CALL)
    energy = round(snap.energy, 1)
    _PRIO = {"高": "high", "中": "medium", "低": "low"}
    prio = _PRIO.get(priority, "medium")

    from domain.todos.crud import create_task, list_tasks, update_task

    if action == "review":
        goals = [t for t in list_tasks() if (t.get("type") == "goal")]
        return _j({"ok": True, "goals": goals,
                    "_deprecated_hint": "manage_goals 已退役, review 改用 sense_todos(type='goal')",
                    "energy": energy})
    if action == "add":
        if not text:
            return registry.tool_error("text is required for add")
        result = create_task(title=text, detail=description, priority=prio,
                             type="goal", project_id="", status="planned",
                             source="migrated:GOALS.md")
        return _j({"ok": result.get("ok", False), "task": result.get("task"),
                    "todo_id": result.get("task", {}).get("id", ""),
                    "_deprecated_hint": "manage_goals 已退役, add 改用 todo(action='create', type='goal')",
                    "energy": energy,
                    "reason": result.get("reason", "")})
    # complete / abandon: 模糊匹配 title
    matching = [t for t in list_tasks()
                if t.get("type") == "goal" and t.get("status") in ("planned", "in_progress")
                and text in (t.get("title") or "")]
    if not matching:
        return _j({"ok": False, "reason": "未找到匹配的目标", "energy": energy})
    new_status = "done" if action == "complete" else "cancelled"
    tid = matching[0]["id"]
    ok = update_task(tid, status=new_status).get("ok", False)
    return _j({"ok": ok, "todo_id": tid,
                "_deprecated_hint": f"manage_goals 已退役, 改用 todo(action='{'done' if action=='complete' else 'cancel'}', todo_id=...)",
                "energy": energy})



# ──────────────────────────────── manage_plan ────────────────────────────────

def _handle_manage_plan(args: Dict[str, Any], **_) -> str:
    """[退役中] 管理计划的里程碑。原写 PLANS.md, 现转发到 todos + todo_plans 表。

    映射:每个 `goal` 对应一个 todo(type='goal'), 里程碑是该 todo 的 plan_item。
      add_milestone     → 找/建 goal todo, create_plan(task_id, text)
      complete_milestone → complete_plan(plan_id)
      remove_milestone  → skip_plan(plan_id)(无硬删, 用 'skipped' 状态)
    """
    action = (args.get("action") or "").strip().lower()
    goal = (args.get("goal") or "").strip()
    text = (args.get("text") or "").strip()

    if action not in ("add_milestone", "complete_milestone", "remove_milestone"):
        return registry.tool_error("action must be add_milestone/complete_milestone/remove_milestone")
    if not goal or not text:
        return registry.tool_error("goal and text are required")

    snap = vitals.consume_energy(ENERGY_COST_PER_CALL)
    energy = round(snap.energy, 1)

    from domain.todos.crud import (list_tasks, create_task, list_plans,
                                    create_plan, complete_plan, skip_plan)

    # 找该 goal 对应的 task(无则建一个)
    matching = [t for t in list_tasks()
                if t.get("type") == "goal" and goal in (t.get("title") or "")]
    if matching:
        tid = matching[0]["id"]
    else:
        result = create_task(title=goal, type="goal", project_id="",
                             status="planned", source="migrated:PLANS.md")
        if not result.get("ok"):
            return _j({"ok": False, "reason": result.get("reason", ""), "energy": energy})
        tid = result["task"]["id"]

    if action == "add_milestone":
        pr = create_plan(tid, text)
        return _j({"ok": pr.get("ok", False), "task_id": tid,
                    "plan_id": pr.get("plan_id"),
                    "_deprecated_hint": "manage_plan 已退役,改用 todo(action='create', type='goal') "
                                        "建 goal 头 + todo_plan(action='create') 加里程碑",
                    "energy": energy})
    # find existing plan by text
    plans = list_plans(tid)
    target = None
    for p in plans:
        if text in (p.get("content") or ""):
            target = p
            break
    if not target:
        return _j({"ok": False, "reason": "未找到匹配的里程碑",
                    "energy": energy})
    pid = target["id"]
    if action == "complete_milestone":
        return _j({"ok": True, "plan_id": pid,
                    "_deprecated_hint": "manage_plan 已退役,改用 todo_plan(action='done')",
                    "energy": energy,
                    **complete_plan(pid)})
    # remove → skip(safe, 保留历史)
    return _j({"ok": True, "plan_id": pid,
                "_deprecated_hint": "manage_plan 已退役,改用 todo_plan(action='skip')",
                "energy": energy,
                **skip_plan(pid)})



def _create_plan_item_alarms(text: str) -> list[dict]:
    """解析 plan text 中的 HH:MM 时间项，给每个到点项注册一个 timer 闹钟。

    产品语义: manage_daily plan 写的计划项应当走通用闹钟事件(timer),
    不再独立成 daily_item 类型——timer 一律是"到点了告诉模型该做某事",
    与来源(rest/timer/daily plan)无关。

    修复: 创建前检查是否已有相同时间的闹钟,避免重复。
    """
    import re
    from datetime import datetime, timedelta
    from domain.lifecycle.alarms import set_alarm, list_pending_alarms
    from domain.lifecycle import clock as _clock

    created = []
    # 北京作息：HH:MM 解析按北京日历；fire_at 写库时再 astimezone(UTC) 保证存储统一。
    now_bj = _clock.beijing_now_dt()
    today = now_bj.date()

    # 查询现有未触发的闹钟，按 fire_at 分组
    try:
        pending_alarms = list_pending_alarms(kind=None)  # 不限制类型，查所有
        existing_times = set()
        for alarm in pending_alarms:
            fire_at = alarm.get("fire_at", "")
            if fire_at:
                # 提取 HH:MM 部分（解析为 UTC 后转回北京小时数）
                try:
                    fire_dt = _clock.parse_iso(fire_at)
                    existing_times.add(fire_dt.astimezone(_clock.BEIJING).strftime("%H:%M"))
                except Exception:
                    pass
    except Exception as exc:
        logger.warning("Failed to check existing alarms, proceeding without dedup: %s", exc)
        existing_times = set()

    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"(\d{1,2}:\d{2})\s+(.+)", line)
        if not m:
            continue
        time_str = m.group(1)
        item_text = m.group(2).strip()

        # 去重检查：如果该时间已有闹钟，跳过
        if time_str in existing_times:
            logger.info("Skipping duplicate alarm for %s: already exists", time_str)
            continue

        try:
            h, m_min = map(int, time_str.split(":"))
            fire_dt = now_bj.replace(hour=h, minute=m_min, second=0, microsecond=0)
            # 如果时间已过今天，设到明天
            if fire_dt <= now_bj:
                fire_dt += timedelta(days=1)
            alarm_id = set_alarm(
                event_kind="timer",
                fire_at=_clock.to_storage_iso(fire_dt),
                # timer 类型的 payload 字段: reason(简短描述) + mental_context(留空,
                # 让模型调 sense_event_detail 时只看 reason 一行) + source(来源标记)
                payload={
                    "reason": f"📋 {item_text}",
                    "mental_context": "",
                    "source": "manage_daily",
                },
            )
            created.append({"time": time_str, "text": item_text, "alarm_id": alarm_id})
            existing_times.add(time_str)  # 防止同一次调用中重复
        except Exception as exc:
            logger.debug("Failed to create plan-item timer for %s: %s", line, exc)

    return created

# ──────────────────────────────── manage_daily ────────────────────────────────

def _handle_manage_daily(args: Dict[str, Any], **_) -> str:
    """[退役中] 管理每日计划。原写 DAILY.md + 双写 timers, 现全转 tasks/timers。

    映射:
      plan    → 每行一项:HH:MM 起 → timer 闹钟;其他 → todo(type='daily', deadline=今天)
      add     → todo(action='create', type='daily', deadline=今天)
      complete → todo(action='done')(按 title 模糊匹配)
      check   → sense_todos(type='daily') 范围
    """
    import re as _re
    action = (args.get("action") or "check").strip().lower()
    text = (args.get("text") or "").strip()

    if action not in ("plan", "add", "complete", "check"):
        return registry.tool_error("action must be plan/add/complete/check")

    snap = vitals.consume_energy(ENERGY_COST_PER_CALL)
    energy = round(snap.energy, 1)

    # 兼容:继续保留 _create_plan_item_alarms(HH:MM → timer 闹钟, 这是真在用)
    _HHMM_RE = _re.compile(r"\d{1,2}:\d{2}")

    if action == "plan":
        if not text:
            return registry.tool_error("text is required for plan（每行一个任务）")
        # HH:MM 项仍走 timer(已迁移完毕, 不再走 DAILY.md)
        timers = _create_plan_item_alarms(text)
        timer_info = ""
        if timers:
            lines = [f"- {t['time']} → {t['text']}" for t in timers]
            timer_info = f"\n已设定闹钟（{len(timers)}项）：\n" + "\n".join(lines)
        # 非 HH:MM 文字项 → tasks 表
        from domain.todos.crud import create_task
        from domain.lifecycle.clock import now_iso as _now_iso
        non_timer = [ln.strip() for ln in text.splitlines()
                     if ln.strip() and ln.strip().startswith("-")
                     and not _HHMM_RE.search(ln[:8])]
        created_ids = []
        for ln in non_timer:
            clean = ln.lstrip("-").strip()
            if not clean:
                continue
            r = create_task(title=clean, type="daily", deadline=_now_iso()[:10],
                            project_id="", status="planned",
                            source="migrated:DAILY.md")
            if r.get("ok"):
                created_ids.append(r["task"]["id"])
        return _j({"ok": True,
                    "result": f"已写入 tasks 表({len(created_ids)} 项) + "
                              f" timers 闹钟({len(timers)} 项)" + timer_info,
                    "timers_created": len(timers),
                    "tasks_created": created_ids,
                    "_deprecated_hint": "manage_daily 已退役, plan 改用 todo(action='create', "
                                        "type='daily'); HH:MM 项用 todo_trigger 或 sense_schedule。",
                    "energy": energy})

    if action == "add":
        if not text:
            return registry.tool_error("text is required for add")
        from domain.todos.crud import create_task
        from domain.lifecycle.clock import now_iso as _now_iso
        r = create_task(title=text, type="daily", deadline=_now_iso()[:10],
                        project_id="", status="planned",
                        source="migrated:DAILY.md")
        return _j({"ok": r.get("ok", False), "todo_id": r.get("task", {}).get("id", ""),
                    "result": "已添加到今日计划" if r.get("ok") else r.get("reason", ""),
                    "_deprecated_hint": "manage_daily 已退役, add 改用 todo(action='create', type='daily')",
                    "energy": energy})

    if action == "complete":
        if not text:
            return registry.tool_error("text is required for complete")
        from domain.todos.crud import list_tasks, update_task
        from domain.lifecycle.clock import now_iso as _now_iso
        today = _now_iso()[:10]
        # 当日 type=daily 任务中按 title 模糊匹配
        matching = [t for t in list_tasks()
                    if t.get("type") == "daily" and t.get("status") in ("planned", "in_progress")
                    and (t.get("deadline") or "") == today
                    and text in (t.get("title") or "")]
        if not matching:
            return _j({"ok": False, "result": "没找到匹配的当日任务",
                        "energy": energy})
        ok = update_task(matching[0]["id"], status="done").get("ok", False)
        return _j({"ok": ok, "todo_id": matching[0]["id"],
                    "result": "已完成" if ok else "更新失败",
                    "_deprecated_hint": "manage_daily 已退役, complete 改用 todo(action='done')",
                    "energy": energy})

    # check
    from domain.todos.crud import list_tasks
    from domain.lifecycle.clock import now_iso as _now_iso
    today = _now_iso()[:10]
    items = [t for t in list_tasks()
             if t.get("type") == "daily" and (t.get("deadline") or "") == today]
    return _j({"ok": True, "tasks": items,
                "_deprecated_hint": "manage_daily 已退役, check 改用 sense_todos(type='daily')",
                "energy": energy})



# ──────────────────────────────── update_rules ────────────────────────────────

def _handle_update_rules(args: Dict[str, Any], **_) -> str:
    text = (args.get("text") or "").strip()
    mode = (args.get("mode") or "append").strip()
    if not text:
        return registry.tool_error("text is required")
    if mode not in ("append", "replace"):
        return registry.tool_error("mode must be 'append' or 'replace'")

    snap = vitals.consume_energy(ENERGY_COST_PER_CALL)
    entities = args.get("entities")
    if entities and isinstance(entities, list):
        _update_rules(text, mode=mode, entities=entities)
    else:
        _update_rules(text, mode=mode)

    # 兼容层: 同步写入认知库 (新统一入口) — 让规则可被联想命中
    # mode='replace' 整体覆盖的场景里,逐条追加更合理:模型单条写入即可
    cog_note = ""
    if mode == "append":
        try:
            from domain.memory.memory.recall.unified.cognition_store import add_cognition_direct
            entity_links = entities if isinstance(entities, list) else []
            # V2 (2026-07-23): 透传 payload, 让规则可被精确去重/冲突检测
            # 模型给的 entities 列表会自动拼出 cog_key(如 ["金开新能"] + 文本前缀)
            payload = _build_rule_payload(args, entities)
            cog_result = add_cognition_direct(
                text=text, entity_links=entity_links, source="rule",
                payload=payload,
            )
            if cog_result.get("duplicate_warning"):
                cog_note = " " + cog_result["duplicate_warning"]
            elif cog_result.get("conflict_warning"):
                cog_note = " " + cog_result["conflict_warning"]
        except Exception:
            pass

    return _j({
        "ok": True,
        "mode": mode,
        "note": f"行为规则已更新（{mode}）" + cog_note,
        "energy": round(snap.energy, 1),
    })


def _build_rule_payload(args: Dict[str, Any], entities: Any) -> dict | None:
    """V2: 从 update_rules 入参构造认知 payload (cog_key + value)。

    规则类认知大多有清晰的 subject:predicate 形态, 把 entities[0] 当 subject,
    并用模糊 predicate 推断(如"止损"→ stop_loss_line, "仓位"→ position_size)。

    若无法推 cog_key → 返回 None, 走 V1 兜底。
    """
    p = args.get("payload")
    if isinstance(p, dict) and p.get("key"):
        # 模型直接给了完整 payload — 优先用
        return p
    # 简单启发式: 用第一个 entity + 文本提取一个粗粒度 predicate
    if not isinstance(entities, list) or not entities:
        return None
    subject = str(entities[0]).strip()
    if not subject or len(subject) > 20:
        return None
    # 不强行造 predicate(会乱); 让模型有需要时自己手动写 payload
    return None


registry.register(
    name="update_rules",
    toolset="actions",
    schema={
        "name": "update_rules",
        "description": (
            "更新长期行为规则。这些规则在每次唤醒时都会被注入，帮助你保持行为一致性。"
            "每条规则应包含：什么场景下、应该怎么做、为什么、违反的后果。"
            "mode='append' 追加一条，mode='replace' 整体替换。"
            "用 evening_review 和 weekly_review 来积累规则，不要频繁改动。\n\n"
            "(V2 可选) 关键参数类规则(如止损线/仓位/汇报时间)可加 payload 字段, "
            "形如 {\"payload\": {\"key\": \"金开新能:stop_loss_line\", \"value\": -0.07}}, "
            "供精确去重/冲突检测。纯语气/沟通类规则无需 payload。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "规则内容"},
                "mode": {"type": "string", "description": "append 或 replace", "enum": ["append", "replace"]},
                "entities": {"type": "array", "items": {"type": "string"}, "description": "关联的实体，用于后续检索"},
                "payload": {
                    "type": "object",
                    "description": "(V2 可选) 结构化主键, 用于参数类规则的精确去重/冲突检测。格式同 add_cognition",
                    "properties": {
                        "key": {"type": "string", "description": "subject:predicate"},
                        "value": {"description": "任意 JSON"},
                        "polarity": {"type": "string", "enum": ["positive", "negative", "neutral"], "description": "极性(embedding 分不开 positive/negative)"},
                    },
                    "required": ["key"],
                },
            },
            "required": ["text"],
        },
    },
    handler=_handle_update_rules,
    check_fn=lambda: True,
    emoji="📜",
    schema_visible=False,  # V6 工具精简: 降级
)


# ──────────────────────────────── update_context ────────────────────────────────

def _handle_update_context(args: Dict[str, Any], **_) -> str:
    text = (args.get("text") or "").strip()
    if not text:
        return registry.tool_error("text is required")

    snap = vitals.consume_energy(ENERGY_COST_PER_CALL)
    _update_context(text)

    return _j({
        "ok": True,
        "note": "交接上下文已更新（覆盖旧内容）。",
        "energy": round(snap.energy, 1),
    })


registry.register(
    name="update_context",
    toolset="actions",
    schema={
        "name": "update_context",
        "description": "写入交接上下文——给明天的自己留个条，告诉下次醒来时应该知道的事。每次覆盖旧内容。",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "上下文内容"},
            },
            "required": ["text"],
        },
    },
    handler=_handle_update_context,
    check_fn=lambda: True,
    emoji="📋",
    schema_visible=False,  # V6 工具精简: 降级
)


# ──────────────────────────────── add_lesson ────────────────────────────────

def _handle_add_lesson(args: Dict[str, Any], **_) -> str:
    text = (args.get("text") or "").strip()
    if not text:
        return registry.tool_error("text is required")

    snap = vitals.consume_energy(ENERGY_COST_PER_CALL)
    entities = args.get("entities")
    section = (args.get("section") or "other").strip().lower()

    # 兼容层: 同时写 LESSONS.md(旧 human-readable) + add_cognition(新认知库)
    if entities and isinstance(entities, list):
        _add_lesson(text, entities=entities, section=section)
    else:
        _add_lesson(text, section=section)

    # 同步写入认知库 (新统一入口)
    cog_result = None
    try:
        from domain.memory.memory.recall.unified.cognition_store import add_cognition_direct
        entity_links = entities if isinstance(entities, list) else []
        if not entity_links:
            entity_links = [section]
        # V2 (2026-07-23): 透传模型给的 payload (参数/版本/工具特定键的教训)
        payload = args.get("payload")
        if payload is not None and not isinstance(payload, dict):
            try:
                import json as _json
                payload = _json.loads(payload)
            except Exception:
                payload = None
        # 安全校验: 空 dict / 无 key → 视为无 payload
        if isinstance(payload, dict) and not payload.get("key"):
            payload = None
        cog_result = add_cognition_direct(
            text=text, entity_links=entity_links, source="lesson",
            payload=payload,
        )
    except Exception:
        pass  # 认知库写入失败不阻塞旧路径

    # 写入纪律提示:同 section 同主题超阈 → 提示合并而非新写
    warning = ""
    try:
        from domain.memory.memory.consciousness.runtime import _get_runtime_home
        les_path = _get_runtime_home() / "memories" / "LESSONS.md"
        if les_path.exists():
            text_full = les_path.read_text(encoding="utf-8")
            section_titles = {
                "trading": "交易策略", "system": "代码工程", "tool": "工具使用",
                "workflow": "工作方式", "rule": "沟通规则", "other": "其他",
            }
            st = section_titles.get(section, "其他")
            if f"## {st}" in text_full:
                sec_text = text_full.split(f"## {st}", 1)[1].split("## ", 1)[0]
                sec_count = sec_text.count("---\n[")
                if sec_count >= 25:
                    warning = f" ⚠ 当前 ## {st} 已 {sec_count} 条,建议调 memory_hygiene skill 合并同主题"
    except Exception:
        pass

    # 如有重复/冲突警告,加到文明提示里
    if cog_result:
        if cog_result.get("duplicate_warning"):
            warning += " " + cog_result["duplicate_warning"]
        elif cog_result.get("conflict_warning"):
            warning += " " + cog_result["conflict_warning"]

    return _j({
        "ok": True,
        "note": "经验教训已记录。" + warning,
        "energy": round(snap.energy, 1),
        "cognition_chunk_id": cog_result.get("new_chunk_id") if cog_result else None,
    })


registry.register(
    name="add_lesson",
    toolset="actions",
    schema={
        "name": "add_lesson",
        "description": (
            "记录一条可迁移的经验教训。长期积累,每次唤醒时自动注入最近 3 条。"
            "LESSONS.md 按 section 主题分节存,请选最贴切的 section。\n\n"
            "(V2 可选) 数值/参数类教训(如某工具的ideal参数/某个版本号/某个频率)可带 payload,"
            "形如 {\"payload\": {\"key\": \"execute_buyscript:optimal_position_pct\", \"value\": 12.5}}, "
            "供未来精确去重/冲突检测。纯叙述类教训无需 payload。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "教训内容——发现了什么,以后应该怎么做"},
                "section": {
                    "type": "string",
                    "description": "主题分节。trading(交易策略/量化) / system(代码工程/系统行为) / tool(工具使用:express_to_human/terminal/sense_*/飞书@机制/权限) / workflow(工作方式/复盘方法论/精力管理) / rule(沟通规则/权限边界) / other",
                    "enum": ["trading", "system", "tool", "workflow", "rule", "other"],
                },
                "entities": {"type": "array", "items": {"type": "string"}, "description": "关联的实体,用于后续检索。如股票代码、工具名、概念名等"},
                "payload": {
                    "type": "object",
                    "description": "(V2 可选) 结构化主键, 用于参数/版本号的教训的精确去重。格式同 add_cognition",
                    "properties": {
                        "key": {"type": "string", "description": "subject:predicate"},
                        "value": {"description": "任意 JSON"},
                        "polarity": {"type": "string", "enum": ["positive", "negative", "neutral"], "description": "判断极性"},
                    },
                    "required": ["key"],
                },
            },
            "required": ["text"],
        },
    },
    handler=_handle_add_lesson,
    check_fn=lambda: True,
    emoji="💡",
    schema_visible=False,  # V6 工具精简: 降级
)


# ──────────────────────────────── add_insight ─────────────────────────────────────────
# 之前 INSIGHTS.md 是 record_thought(kind != status) 内部副带写,模型不知道这回事,
# 没有专门写入入口。补 add_insight 让模型显式记灵感/质疑/卡点/警告。

def _handle_add_insight(args: Dict[str, Any], **_) -> str:
    text = (args.get("text") or "").strip()
    if not text:
        return registry.tool_error("text is required")
    kind = (args.get("kind") or "idea").strip().lower()
    valid_kinds = {"idea", "doubt", "block", "warning"}
    if kind not in valid_kinds:
        kind = "idea"
    snap = vitals.consume_energy(ENERGY_COST_PER_CALL)
    entities = args.get("entities") if isinstance(args.get("entities"), list) else None
    try:
        _append_insight(kind=kind, text=text, entities=entities)
    except Exception as exc:
        return registry.tool_error(f"append insight failed: {exc}")
    return _j({
        "ok": True,
        "note": f"灵感碎片已记录 [{kind}]。",
        "energy": round(snap.energy, 1),
    })


registry.register(
    name="add_insight",
    toolset="actions",
    schema={
        "name": "add_insight",
        "description": (
            "记录一条灵感碎片。INSIGHTS.md 是闪念寄存室——idea/doubt/block/warning 四类。"
            "和 LESSONS(已验证打法)区别:INSIGHTS 是未验证的、值得回头看的、不必每次 wake 都见。"
            "self_review 复盘时会 sense_insights 拾起来评估,验证后该升级成 lesson + 删原 insight。"
            "你不该用 add_insight 记抽象的大道理或当日记——那是 record_thought / write_diary 的活。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "一句话灵感/质疑/卡点/警告。建议短(<150 字)"},
                "kind": {
                    "type": "string",
                    "description": "idea(闪现洞察,doubt=质疑当前做法,block=具体卡点(缺什么/可能解),warning=反模式警觉",
                    "enum": ["idea", "doubt", "block", "warning"],
                },
                "entities": {"type": "array", "items": {"type": "string"}, "description": "关联实体(股票名/概念/工具名),用于语义联想"},
            },
            "required": ["text"],
        },
    },
    handler=_handle_add_insight,
    check_fn=lambda: True,
    emoji="✨",
    schema_visible=False,  # V6 工具精简: 降级
)


# ──────────────────────────────── update_self_knowledge ────────────────────────────────

def _handle_update_self_knowledge(args: Dict[str, Any], **_) -> str:
    text = (args.get("text") or "").strip()
    if not text:
        return registry.tool_error("text is required")
    mode = (args.get("mode") or "append").strip()
    if mode not in ("append", "replace"):
        return registry.tool_error("mode must be 'append' or 'replace'")

    _update_self_knowledge(text, mode=mode)

    return _j({
        "ok": True,
        "note": "自我认知已更新。" if mode == "append" else "自我认知已替换。",
    })


registry.register(
    name="update_self_knowledge",
    toolset="actions",
    schema={
        "name": "update_self_knowledge",
        "description": "更新自我认知档案——对自己行为模式的中立观察。不是在写规则，而是在认识自己。",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "观察到的行为模式，如'在X情况下我倾向于Y'"},
                "mode": {"type": "string", "description": "append（默认，追加一条观察）/ replace（整体替换）", "enum": ["append", "replace"]},
            },
            "required": ["text"],
        },
    },
    handler=_handle_update_self_knowledge,
    check_fn=lambda: True,
    emoji="🪞",
    schema_visible=False,  # V6 工具精简: 降级
)


# ──────────────────────────────── search_history ────────────────────────────────

def _handle_search_history(args: Dict[str, Any], **_) -> str:
    """检索历史对话片段——通过内容/时间/工具类型搜索旧段。

    用途：在需要回顾之前做过什么时使用（如修复 bug、重拾任务线索）。
    返回匹配片段的上下文（前后各几条消息）。
    """
    from domain.memory.memory.summaries.recall_utils import search_session_messages

    query = args.get("query", "").strip()
    session_id = args.get("session_id")
    time_range = args.get("time_range", "24h")  # 1h/6h/24h/7d
    tool_type = args.get("tool_type")  # execute_code/terminal/read_file 等
    limit = min(int(args.get("limit", 5)), 20)

    if not query:
        return registry.tool_error("query is required")

    results = search_session_messages(
        query=query,
        session_id=session_id,
        time_range=time_range,
        tool_type=tool_type,
        limit=limit,
    )

    if not results:
        return _j({"ok": True, "results": [], "note": f"未找到与「{query}」相关的对话片段"})

    return _j({
        "ok": True,
        "results": results,
        "count": len(results),
    })


registry.register(
    name="search_history",
    toolset="actions",
    schema={
        "name": "search_history",
        "description": "检索历史对话片段——通过关键词搜索之前的对话内容。用于需要回顾之前做过什么时（修复 bug、重拾任务线索等）。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词（文件名、函数名、错误信息等）"},
                "session_id": {"type": "string", "description": "可选，限定搜索某个 session"},
                "time_range": {"type": "string", "description": "时间范围：1h/6h/24h/7d，默认 24h", "enum": ["1h", "6h", "24h", "7d"]},
                "tool_type": {"type": "string", "description": "可选，限定工具类型：execute_code/terminal/read_file 等"},
                "limit": {"type": "integer", "description": "返回结果数量，默认 5，最大 20"},
            },
            "required": ["query"],
        },
    },
    handler=_handle_search_history,
    check_fn=lambda: True,
    emoji="🔍",
)


# ──────────────────────────────── read_archive ────────────────────────────────

def _handle_read_archive(args: Dict[str, Any], **_) -> str:
    """读取归档的工具输出——压缩时归档的大文件可以通过此工具回溯。"""
    import os
    from pathlib import Path
    from infrastructure.config import get_app_instance_id

    archive_id = args.get("archive_id", "").strip()
    session_id = args.get("session_id", "").strip()

    if not archive_id:
        return registry.tool_error("archive_id is required")

    instance_id = get_app_instance_id()
    base_dir = Path("var/tool_archives") / instance_id
    if session_id:
        archive_path = base_dir / session_id / f"{archive_id}.txt"
    else:
        # 搜索所有 session 中的该 archive_id
        candidates = list(base_dir.glob(f"*/{archive_id}.txt"))
        if candidates:
            archive_path = candidates[0]
        else:
            return registry.tool_error(f"Archive {archive_id} not found")

    if not archive_path.exists():
        return registry.tool_error(f"Archive file not found: {archive_id}")

    try:
        content = archive_path.read_text(encoding="utf-8")
        # 截断过长的归档内容
        if len(content) > 5000:
            content = content[:5000] + f"\n... (共 {len(content)} 字符，已截断)"
        return _j({
            "ok": True,
            "archive_id": archive_id,
            "session_id": session_id,
            "content": content,
            "size": len(content),
        })
    except Exception as e:
        return registry.tool_error(f"Failed to read archive: {e}")


registry.register(
    name="read_archive",
    toolset="actions",
    schema={
        "name": "read_archive",
        "description": "读取归档的工具输出——压缩时归档的大文件内容回溯。",
        "parameters": {
            "type": "object",
            "properties": {
                "archive_id": {"type": "string", "description": "归档文件 ID（叙事中会标注 archive_id）"},
                "session_id": {"type": "string", "description": "可选，session ID 用于定位归档路径"},
            },
            "required": ["archive_id"],
        },
    },
    handler=_handle_read_archive,
    check_fn=lambda: True,
    emoji="📦",
    schema_visible=False,  # V6 工具精简: 降级
)


# ──────────────────────────────── recall_tool_result ────────────────────────────────


def _handle_recall_tool_result(args: Dict[str, Any], **context) -> str:
    """取回之前被上下文压缩的工具结果原文。

    agent 的 _compact_old_tool_messages 会把 >depth 轮以前且 >min_chars 的真实
    tool 消息在「发给 LLM 的 payload」里就地替换为指针("该结果已压缩"),DB 保留
    原文。当模型在历史里看到 "[旧工具结果已压缩] ... → recall_tool_result(...)"
    并需要再次读取该次调用的完整结果时，通过本工具按 tool_call_id 精确取回。
    """
    session_id = str(context.get("session_id") or "")
    tool_call_id = (args.get("tool_call_id") or "").strip()
    if not tool_call_id:
        return registry.tool_error("tool_call_id is required")
    if not session_id:
        # 没有 session 上下文无法定位；按协议报错而不是隐式跨 session 全扫，
        # 避免取到同名工具调用但不相关的历史行。
        return registry.tool_error(
            "session 上下文缺失，无法定位 tool_call_id（多半是工具被在 session 外调用）"
        )

    from infrastructure.ai.session_db import SessionDB
    db = SessionDB()
    row = db.get_tool_message_by_call_id(session_id, tool_call_id)
    if not row:
        return registry.tool_error(
            f"未找到 tool_call_id={tool_call_id} 的历史结果。"
            "可能是：(1) id 来自其他 session；(2) 旧 session 已被清理；"
            "(3) 该 id 不是真实工具调用（fake 注入项不会被压缩，也无原文可召回）。"
        )
    return _j({
        "tool_call_id": tool_call_id,
        "tool_name": row.get("tool_name", ""),
        "timestamp": row.get("timestamp", 0),
        "content": row.get("content", ""),
    })


registry.register(
    name="recall_tool_result",
    toolset="senses",
    schema={
        "name": "recall_tool_result",
        "description": (
            "取回之前被上下文压缩的工具结果原文。"
            "当历史消息中出现 '[旧工具结果已压缩] ... → recall_tool_result(...)' "
            "提示、且你需要再次读取该次工具调用的完整结果时使用。注意只有"
            "同时「够老」且「够大」的工具结果才会被压缩；最近几轮的、或短结果"
            "仍然在上下文里，不必召回。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tool_call_id": {
                    "type": "string",
                    "description": "消息里标注的工具调用 id（形如 call_xxx）",
                },
            },
            "required": ["tool_call_id"],
        },
    },
    handler=_handle_recall_tool_result,
    check_fn=lambda: True,
    emoji="🔍",
    schema_visible=False,  # V6 工具精简: 降级
    # 召回的就是「当初被压掉的全量」，不能用默认 8KB 再截一道——
    # 否则花一次调用却还是拿不到完整内容，召回机制失效。
    max_result_size_chars=50000,
)


# ──────────────────────────────── rest ────────────────────────────────

def _summarize_current_session_outputs() -> str:
    """当前 session 内的产出汇总——只抓"产出类"工具(写文件/沉淀/认知形成/
    完成任务/重要发消息), 不抓纯 sense/记忆查询。用于 rest 提示卡前的"本轮产出"。

    返回 "<N> 项: file1, file2, lesson X, task done #abc, ..." 形式, 无产出时返 ""。
    """
    try:
        from infrastructure.config import get_current_session_id
        session_id = get_current_session_id()
        if not session_id:
            return ""
        # 从 session_db 拿当前 session 所有 tool_calls
        from infrastructure.ai.session_db import SessionDB
        session_db = SessionDB()
        messages = session_db.get_messages(session_id)
        # 产出类关键词(与 digest 抓产出对齐)
        OUTPUT_PREFIXES = ("写文件: ", "沉淀 ", "形成认知", "取代认知", "修订认知",
                           "更新规则: ", "更新自我认知: ", "注册 ", "登记附件: ",
                           "完成任务: ", "创建待办: ", "日记: ")
        outputs: list[str] = []
        for m in messages:
            tc = m.get("tool_calls")
            if not tc:
                continue
            try:
                calls = tc if isinstance(tc, list) else json.loads(tc or "[]")
            except Exception:
                continue
            for call in calls:
                fn = call.get("function", {})
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments", "{}"))
                except Exception:
                    args = {}
                from domain.memory.memory.summaries import summarize_tool_call
                s = summarize_tool_call(name, args)
                if s and any(s.startswith(p) for p in OUTPUT_PREFIXES):
                    # 简短化: 取前 50 chars
                    short = s[:50] + ("…" if len(s) > 50 else "")
                    if short not in outputs:  # 去重
                        outputs.append(short)
        if not outputs:
            return ""
        if len(outputs) > 6:
            head = "; ".join(outputs[:5])
            return f"{len(outputs)} 项 — {head} 等"
        return "; ".join(outputs)
    except Exception:
        return ""


def _build_pre_rest_card() -> str:
    """睡前的简短提示卡：让模型在 rest 前扫一眼"是否还有该改/该记的事"。

    设计原则：
      - **轻量、提示为主**——只显示统计 + 突出的几条详情，不强制 rest 前必做某事。
        模型看到后自己决定要不要在 sleep 前补一刀。
      - **只在"有事"时给详情**——大量行枯燥统计反而被当作噪音忽略。
      - **覆盖三类容易睡一觉就忘的事**：
        1. todo：长期挂在 in_progress、过期的、可能做完没改状态的
        2. 项目交付物：过期/长期未推进的（按用户选定的"项目相关"项）
        3. 记忆碎片：今天新增的 idea/doubt/warning（"要不要固化为 todo"避免丢失）
        4. 闹钟：8 小时内会触发的，让模型心里有数（不要起冲突）

    失败时返回空串（绝不让 rest 流程因为提示卡出错而崩）。
    """
    try:
        from infrastructure.config import get_app_instance_id
        iid = get_app_instance_id() or ""
    except Exception:
        iid = ""

    parts: list[str] = []

    # ── 0. 本轮产出（session 内的产出汇总, 来自同一 session 的 summarize_tool_call） ──
    try:
        outputs_summary = _summarize_current_session_outputs()
        if outputs_summary:
            parts.append(f"📤 本轮产出: {outputs_summary}")
    except Exception as exc:
        logger.debug("pre_rest_card outputs summary failed: %s", exc)

    # ── 1. 待办（todo）盘点 ──
    try:
        from domain.todos.crud import list_tasks
        from domain.lifecycle.clock import beijing_now_dt
        from datetime import timedelta
        now = beijing_now_dt()
        in_progress = list_tasks(status_filter="in_progress", assignee_instance=iid, include_unassigned=False) if iid else list_tasks(status_filter="in_progress")
        planned = list_tasks(status_filter="planned", assignee_instance=iid, include_unassigned=False) if iid else list_tasks(status_filter="planned")

        # 过期识别：deadline < now 的 in_progress
        overdue_in_progress: list[dict] = []
        from datetime import datetime
        for t in in_progress:
            dl = t.get("deadline") or ""
            if dl:
                try:
                    ddt = datetime.fromisoformat(dl.replace("Z", "+00:00")) if "T" in dl else None
                    if ddt and ddt < now:
                        overdue_in_progress.append(t)
                except Exception:
                    pass

        open_total = len(in_progress) + len(planned)
        if open_total == 0 and not overdue_in_progress:
            # 没有任意开口的待办 → 该段不输出，避免每次 rest 都污染返回
            pass
        else:
            parts.append(
                f"📋 待办：{len(in_progress)} in_progress · {len(planned)} planned"
                + (f" · ⚠️ 其中 {len(overdue_in_progress)} 个 in_progress 已过期未关" if overdue_in_progress else "")
            )
            for t in overdue_in_progress[:3]:  # 最多列 3 条
                tid = t.get("id", "?")
                title = (t.get("title") or t.get("description") or "")[:40]
                parts.append(f"  - #{tid} 过期：{title}（决定 done / paused / 继续？）")
    except Exception as exc:
        logger.debug("pre_rest_card todos gather failed: %s", exc)

    # ── 2. 项目交付物（仅 active 项目的过期 deliverable） ──
    try:
        from domain.project.loader import load_all_projects
        from domain.project.crud import list_deliverables
        from domain.project._infra import get_db as _get_proj_db
        proj_db = _get_proj_db()
        active_projects = {pid: cfg for pid, cfg in (load_all_projects() or {}).items()
                           if cfg.manager == iid or any(p.assignees and iid in p.assignees for p in (cfg.positions or []))}
        overdue_dl_count = 0
        for pid, cfg in active_projects.items():
            delivs = list_deliverables(proj_db, project_id=pid)
            for d in delivs:
                status = str(d.get("status") or "").lower()
                if status in ("done", "cancelled"):
                    continue
                due = d.get("due_date") or ""
                if due:
                    try:
                        from datetime import datetime
                        from domain.lifecycle.clock import beijing_now_dt
                        due_dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
                        if due_dt < beijing_now_dt():
                            overdue_dl_count += 1
                    except Exception:
                        pass
        if active_projects:
            parts.append(f"📁 项目：参与 {len(active_projects)} 个 active 项目" +
                         (f" · ⚠️ {overdue_dl_count} 个交付物过期" if overdue_dl_count else ""))
    except Exception as exc:
        logger.debug("pre_rest_card project gather failed: %s", exc)

    # ── 3. 记忆碎片（INSIGHTS.md）──
    try:
        from domain.memory.memory.consciousness.runtime import read_insights
        todays_insights = read_insights(days_back=1)
        todays_count = len([l for l in todays_insights.splitlines() if l.startswith("- [")]) if todays_insights else 0
        if todays_count > 0:
            # 按 kind 分一下
            kinds_count: dict[str, int] = {}
            for line in todays_insights.splitlines():
                m_k = line.split("]", 1)[0].lstrip("- [").strip()
                if m_k:
                    kinds_count[m_k] = kinds_count.get(m_k, 0) + 1
            kind_str = " / ".join(f"{k}={v}" for k, v in sorted(kinds_count.items()))
            parts.append(
                f"💡 今日灵感碎片：{todays_count} 条（{kind_str}）"
                f"—— 若有该固化成 todo 的（idea 转任务、doubt 待验证、warning 待避坑），"
                f"建议在 sleep 前用 todo create 或 task_note 记下，避免睡一觉就忘"
            )
    except Exception as exc:
        logger.debug("pre_rest_card insights gather failed: %s", exc)

    # ── 4. 闹钟（未来 8 小时会触发的） ──
    try:
        from domain.lifecycle.alarms import list_pending_alarms
        from domain.lifecycle.clock import beijing_now_dt
        from datetime import datetime, timedelta
        horizon = beijing_now_dt() + timedelta(hours=8)
        upcoming: list[dict] = []
        # timer + routine 都看
        for kind in ("timer", "routine"):
            for a in list_pending_alarms(kind):
                fa = a.get("fire_at") or ""
                try:
                    fa_dt = datetime.fromisoformat(fa.replace("Z", "+00:00"))
                    if fa_dt <= horizon:
                        upcoming.append({"id": a.get("id"), "kind": kind, "fire_at": fa,
                                         "reason": (a.get("payload_json") or "")[:60]})
                except Exception:
                    continue
        upcoming.sort(key=lambda x: x["fire_at"])
        if upcoming:
            preview = "、".join(
                f"#{u['id']}({u['fire_at'][11:16] if len(u['fire_at']) > 16 else u['fire_at']})"
                for u in upcoming[:4]
            )
            parts.append(f"⏰ 未来 8h 内 {len(upcoming)} 个闹钟会触发：{preview}（避免起冲突）")
    except Exception as exc:
        logger.debug("pre_rest_card alarms gather failed: %s", exc)

    if not parts:
        return ""

    return "## 🌙 睡前提示卡（rest 前 30 秒扫一眼，决定要不要补一刀）\n\n" + "\n".join(parts)


# 每个 session 至多展示一次提示卡——第一次调 rest 弹卡让模型先看，
# 第二次调（任意参数）视为已确认，直接进 sleep，不再二次打扰。
# key = session_id；session 结束后由 GC 清理失效 key（见 _gc_session_rest_state）
_rest_card_shown_sessions: set[str] = set()


def _get_request_session_id(**kwargs) -> str:
    """从 dispatch kwargs 取 session_id；为空则用空串作为 fallback key。"""
    return str(kwargs.get("session_id") or "")


def _should_show_pre_rest_card(args: Dict[str, Any], **kwargs) -> bool:
    """判断本次 rest 调用要不要展示提示卡（True=展示预览，False=直接睡）。

    逻辑：
      - 显式 confirm=true → 直接睡（不展示）
      - 本次 session 已展示过提示卡 → 直接睡（避免重复打扰）
      - 其它 → 展示（首次调用）
    """
    if bool(args.get("confirm")):
        return False
    sid = _get_request_session_id(**kwargs)
    if sid and sid in _rest_card_shown_sessions:
        return False
    return True


def _mark_pre_rest_card_shown(**kwargs) -> None:
    sid = _get_request_session_id(**kwargs)
    if sid:
        _rest_card_shown_sessions.add(sid)


def _gc_session_rest_state(active_session_ids: set[str]) -> None:
    """session 结束后由 scheduler 调一次清理，避免 set 无限增长。"""
    _rest_card_shown_sessions.difference_update(
        {sid for sid in list(_rest_card_shown_sessions) if sid not in active_session_ids}
    )


def _handle_rest(args: Dict[str, Any], **kwargs) -> str:
    """rest — 设闹钟 + 结束 session。

    新语义（2026-06-15：
      until 或 reuse 必填一个）：

      rest(until='2026-06-15T15:00:00+08:00', mental_context='...')
          设新闹钟 → BLOCKED → 等到点 wakeup
      rest(reuse=42, mental_context='补充...')
          复用现有 timer 闘钟 #42（不新建）→ BLOCKED → 等原 fire_at wakeup
      rest()  → 报错，并提示现有可复用的闹钟 id 列表

    重叠检测（核心机制）：
      until=X 但 ±10min 内已有 timer → 报错提示 "和 #X 重叠" + ID。
      模型看到 ID 就调 rest(reuse=X) 复用即可。

    这套语义=人设闘钟的样子：
      「我想设下午 3 点」→ rest(until=15:00)
      「发现已经设过 3 点」→ rest(reuse=<刚提示的 id>)
    """
    from datetime import timedelta, datetime
    from domain.lifecycle import clock as _clock

    # ── 解析参数 ──
    # 两种语义（必填其一）：
    #   until=<ISO8601>          新建/覆盖闹钟，到点唤醒
    #   reuse=<alarm_id>         复用现有 timer 闹钟（重叠场景）
    # 不传任何参数 → 报错强制让模型做决定（避免无限期睡死）
    until = (args.get("until") or "").strip()
    hours = args.get("hours")
    reuse_raw = args.get("reuse")
    reason = (args.get("reason") or "").strip() or "休息"
    mental = (args.get("mental_context") or "").strip()
    # 提示卡展示逻辑：每个 session 多至多展示一次。
    # 第一次调 rest（不管 until / reuse / 都重叠场景）→ 返回提示卡 + mark shown
    # 第二次调（任意参数 / 甚至不带 confirm）→ 直接真睡
    # 显式 confirm=true 视为"模型已确认"→ 直接睡，跳过预览
    show_card = _should_show_pre_rest_card(args, **kwargs)
    # 处理 reuse（可能是 int 或 str）
    reuse_id: int = 0
    if reuse_raw not in (None, "", 0):
        try:
            reuse_id = int(reuse_raw)
            if reuse_id <= 0:
                reuse_id = 0
        except (ValueError, TypeError):
            return registry.tool_error(f"invalid reuse: {reuse_raw!r} 必须是 alarm_id（正整数）")

    # ── 解析目标时间（仅 until 或 hours 路径需要） ──
    from domain.lifecycle import clock as _clock
    from domain.lifecycle.alarms import set_alarm, cancel_alarm, list_pending_alarms
    from domain.lifecycle.runtime_context import get_current_affair
    from domain.lifecycle.affairs.runtime import (
        set_wait_intent, WaitIntent, update_affair, get_affair,
    )
    from domain.lifecycle.state_machine import AffairStatus, WaitType
    from datetime import datetime, timedelta

    now = _clock.beijing_now_dt()

    # ─── 路径 A：reuse=<alarm_id>，复用现有 timer/routine ───
    if reuse_id > 0:
        # 找对应的闹钟（查 timer + routine，和重叠检测的数据源一致）
        target_alarm = None
        for a in list_pending_alarms("timer") + list_pending_alarms("routine"):
            if (a.get("id") or 0) == reuse_id:
                target_alarm = a
                break
        if not target_alarm:
            # 闹钟已触发或不存在 — 列出可用 pending 闹钟（timer + routine）让模型重选
            pending = list_pending_alarms("timer") + list_pending_alarms("routine")
            snap = vitals.consume_energy(0)
            import json as _j_reuse_fallback
            timer_lines = []
            for a in pending[:5]:
                try:
                    p = _j_reuse_fallback.loads(a.get("payload_json") or "{}") or {}
                    r = p.get("reason", "")
                except Exception:
                    r = ""
                timer_lines.append(f"  · id={a.get('id')} {a.get('fire_at')}" + (f" ({r})" if r else ""))
            return _j({
                "preview": True,
                "note": f"reuse={reuse_id} 对应的 timer 已触发或不存在。以下是当前可用的 pending timer, 请从中选一个 reuse:",
                "available_alarms": "\n".join(timer_lines) if timer_lines else "(当前没有任何 pending timer, 请用 rest(until=...) 新建)",
                "pre_rest_card": _build_pre_rest_card() or None,
                "energy": round(snap.energy, 1),
            })

        target_fire_at = target_alarm.get("fire_at") or ""
        if show_card:
            # 首次调用：预览模式，返回提示卡 + mark shown
            _mark_pre_rest_card_shown(**kwargs)
            existing_payload_preview = {}
            try:
                import json as _j_preview
                existing_payload_preview = _j_preview.loads(target_alarm.get("payload_json") or "{}") or {}
            except Exception:
                pass
            return _j({
                "preview": True,
                "will_reuse_alarm_id": reuse_id,
                "fire_at": target_fire_at,
                "existing_reason": existing_payload_preview.get("reason", ""),
                "previous_mental_context": (existing_payload_preview.get("mental_context") or "").strip(),
                "pre_rest_card": _build_pre_rest_card() or None,
                "message": (
                    f"⏸️ 预览：将复用 timer 闹钟 #{reuse_id}（{target_fire_at}）休息。"
                    "看完提示卡决定要不要补 todo/project 的事，处理完再调 rest 任意参数进入休息。"
                ),
            })
        # 合并 mental_context（如果模型传了 mental_context）
        import json as _j_reuse
        try:
            existing_payload = _j_reuse.loads(target_alarm.get("payload_json") or "{}") or {}
        except Exception:
            existing_payload = {}
        previous_mental = (existing_payload.get("mental_context") or "").strip()
        new_mental = previous_mental
        if mental:
            new_mental = (previous_mental + ("\n\n" if previous_mental else "") + mental).strip()
            # 把合并后的 mental_context 写回闹钟 payload (直接 UPDATE, 不 cancel+rebuild)
            new_payload = dict(existing_payload)
            new_payload["mental_context"] = new_mental
            try:
                from domain.lifecycle.alarms import update_alarm_payload
                update_alarm_payload(reuse_id, new_payload)
            except Exception as exc:
                logger.warning("rest: reuse merge mental_context failed: %s", exc)

        # 标 BLOCKED + 设 WaitIntent 复用 fire_at
        aid = get_current_affair()
        if aid:
            existing_affair = get_affair(aid)
            if existing_affair:
                update_affair(aid, status=AffairStatus.BLOCKED)
            intent = WaitIntent(
                wait_type=WaitType.UNTIL,
                resume_when=target_fire_at,
                reason=reason,
                resume_action="",
                meta={"vitals_at_sleep": {}, "reused_alarm_id": reuse_id},
            )
            set_wait_intent(aid, intent)

        return _j({
            _BLOCK_SENTINEL: True,
            "started": True,
            "set_alarm": False,
            "reused_alarm_id": reuse_id,
            "fire_at": target_fire_at,
            "existing_reason": existing_payload.get("reason", ""),
            "previous_mental_context": previous_mental,
            "merged_mental_context": new_mental if mental else None,
            "message": f"复用 timer 闹钟 #{reuse_id}（{target_fire_at}），已结束 session",
            "pre_rest_card": _build_pre_rest_card() or None,
        })

    # ─── 路径 B：解析 until/hours 设新闹钟 ───
    target_dt = None
    if until:
        try:
            target_dt = datetime.fromisoformat(until)
            if target_dt.tzinfo is None:
                target_dt = target_dt.replace(tzinfo=_clock.BEIJING)
        except Exception as e:
            return registry.tool_error(f"invalid until: {e}")
    elif hours is not None:
        try:
            target_dt = now + timedelta(hours=float(hours))
        except Exception:
            return registry.tool_error("hours must be a number")

    # ─── 路径 C：都没传 → 不报错, 直接返回 preview 提示卡 ───
    # 设计: 第一次 rest() 无参 = 看"睡前提示卡", 模型拿到后处理再调 rest(reuse/until) 真睡。
    # 之前这里返 tool_error, 模型被迫多走一轮试错 → 压缩为 2 轮 preview→sleep。
    if target_dt is None:
        snap = vitals.consume_energy(0)  # 不消耗精力, 只拿 snapshot
        energy = round(snap.energy, 1)
        existing_timers = list_pending_alarms("timer")
        import json as _j_hint
        timer_lines = []
        suggested_reuse = None
        if existing_timers:
            for a in existing_timers[:5]:
                try:
                    p = _j_hint.loads(a.get("payload_json") or "{}") or {}
                    r = p.get("reason", "")
                except Exception:
                    r = ""
                timer_lines.append(f"  · id={a.get('id')} {a.get('fire_at')}" + (f" ({r})" if r else ""))
                if suggested_reuse is None:
                    suggested_reuse = a.get("id")
        card = _build_pre_rest_card() or ""
        return _j({
            "preview": True,
            "note": "rest 无参数 = 预览睡前提示卡。看一眼处理完后, 再调 rest(reuse=<id>) 或 rest(until=...) 真睡。" if suggested_reuse else "rest 无参数 = 预览。当前无可复用闹钟, 处理完后调 rest(until=...) 新建闹钟真睡。",
            "will_reuse_alarm_id": suggested_reuse,
            "pre_rest_card": card or None,
            "available_alarms": "\n".join(timer_lines) if timer_lines else "(无未触发 timer)",
            "energy": energy,
        })

    target_iso = _clock.to_storage_iso(target_dt)

    # ── 查现有闹钟：精确同 fire_at OR ±10min 近似重叠 ──
    # 设计语义：模型 rest(until=15:00) 时如果已有 15:00（或 14:55-15:05）的 timer
    # → 报错提示"和 #X 重叠，用 reuse=X 复用"。模型看到 ID 后调用 rest(reuse=X) 复用。
    import json as _j_overlap
    from datetime import datetime as _dt

    def _parse_payload(a: dict) -> dict:
        try:
            return _j_overlap.loads(a.get("payload_json") or "{}") or {}
        except Exception:
            return {}

    target_dt_ts = target_dt.timestamp()
    overlap_alarm_id = 0
    overlap_alarm_fire_at = ""
    overlap_alarm_reason = ""
    for a in list_pending_alarms("timer") + list_pending_alarms("routine"):  # V6.1: 不含 awaiting_reply (语义不同, 不该互相干扰)
        fa = a.get("fire_at") or ""
        if not fa:
            continue
        try:
            other_dt = _dt.fromisoformat(fa)
            if other_dt.tzinfo is None:
                other_dt = other_dt.replace(tzinfo=now.tzinfo)
            diff = abs(other_dt.timestamp() - target_dt_ts)
            if diff <= 600:  # ±10min 视为重叠
                overlap_alarm_id = a.get("id") or 0
                overlap_alarm_fire_at = fa
                p = _parse_payload(a)
                overlap_alarm_reason = p.get("reason") or ""
                break
        except Exception:
            continue

    # 重叠 → 返回提示信息。若本 session 还没展示过提示卡，附带 pre_rest_card
    # 让模型先扫一眼要不要补 todo/project，处理完再 reuse 或换时间。
    if overlap_alarm_id:
        overlap_msg = (
            f"until={target_iso} 和现有 timer 闹钟 #{overlap_alarm_id}"
            f"（{overlap_alarm_fire_at}"
            + (f"，reason={overlap_alarm_reason}" if overlap_alarm_reason else "")
            + f"）重叠。\n"
            f"→ 复用现有 → rest(reuse={overlap_alarm_id}, mental_context='给未来的留言')\n"
            f"→ 换个时间 → rest(until='')"
        )
        if show_card:
            _mark_pre_rest_card_shown(**kwargs)
            return _j({
                "preview": True,
                "overlap": True,
                "overlap_alarm_id": overlap_alarm_id,
                "pre_rest_card": _build_pre_rest_card() or None,
                "error_hint": overlap_msg,
                "message": (
                    f"⏸️ 预览：你设的 until={target_iso} 与现有 timer #{overlap_alarm_id}"
                    f"（{overlap_alarm_fire_at}）重叠。下面是你的睡前提示卡，看完决定："
                    f"补完待办后再 rest(reuse={overlap_alarm_id}) 真睡，"
                    f"或 rest(until='换的时间')。"
                ),
            })
        # 已展示过提示卡 → 直接返 overlap error（让模型选 reuse 或换时间，重复调不会卡）
        return registry.tool_error(overlap_msg)

    # until 路径：首次调（未展示提示卡）→ 预览；显式 confirm=true / 已展示 → 真睡
    if show_card:
        _mark_pre_rest_card_shown(**kwargs)
        return _j({
            "preview": True,
            "will_set_until": target_iso,
            "pre_rest_card": _build_pre_rest_card() or None,
            "message": (
                f"⏸️ 预览：将在 {target_iso} 休息（新建 timer 闹钟）。"
                "看完提示卡决定要不要补 todo/project 的事，处理完再调 "
                f"rest(until='{target_iso}') 真睡——同一个 session 第二次调直接进入休息。"
            ),
        })

    # 不重叠 → 设新闹钟
    payload = {
        "reason": reason,
        "mental_context": mental,
    }

    # ── 设置 WaitIntent + 标 BLOCKED + set_alarm ──
    from domain.lifecycle.state_machine import WaitType

    aid = get_current_affair()
    if not aid:
        # 兼容兜底（affair 不存在时仍允许设闹钟）
        try:
            set_alarm("timer", fire_at=target_iso, payload=payload)
        except Exception as exc:
            logger.warning("rest: set_alarm failed (no affair): %s", exc)
        return _j({
            _BLOCK_SENTINEL: True,
            "started": True,
            "affair_id": None,
            "wake_at": target_iso,
            "mental_context": mental,
            "message": f"进入休息，预计 {target_iso} 醒来。闹钟已设置。当你的精力恢复之后，系统会自然而然地叫醒你（不必等到闹钟）。",
            "pre_rest_card": _build_pre_rest_card() or None,
        })

    intent = WaitIntent(
        wait_type=WaitType.UNTIL,
        resume_when=target_iso,
        reason=reason,
        resume_action="",
        meta={"vitals_at_sleep": {}},
    )

    existing_affair = get_affair(aid)
    if existing_affair:
        update_affair(aid, status=AffairStatus.BLOCKED)
    else:
        logger.warning("_handle_rest: affair %s 不在 DB 中", aid)

    set_wait_intent(aid, intent)
    try:
        # set_alarm 内置 dedup：(event_kind="timer", fire_at=target_iso) 已有则 UPDATE payload
        # 我们只需调用即可，自动覆盖（但前面应该已经拦截重叠，这里是干净的新建）
        set_alarm("timer", fire_at=target_iso, payload=payload)
    except Exception as exc:
        logger.warning("rest: set_alarm failed: %s", exc)

    return _j({
        _BLOCK_SENTINEL: True,
        "started": True,
        "affair_id": aid,
        "wake_at": target_iso,
        "mental_context": mental,
        "message": f"进入休息，预计 {target_iso} 醒来。闹钟已设置。当你的精力恢复之后，系统会自然而然地叫醒你（不必等到闹钟）。",
        "pre_rest_card": _build_pre_rest_card() or None,
    })


registry.register(
    name="rest",
    toolset="actions",
    schema={
        "name": "rest",
        "description": (
            "休息 — 设定闹钟 + 结束 session。两步式：\n"
            "\n"
            "## 创建新闹钟(默认)\n"
            "  rest(until='<ISO8601 时间>')\n"
            "  第一次调 → 返睡前提示卡(待办 / 灵感 / 闹钟清单)，看一眼处理完。\n"
            "  第二次调同样参数 → 真睡，session 结束，BLOCKED。\n"
            "\n"
            "## 复用现有闹钟(overlap 场景)\n"
            "  rest(reuse=<id>)，其中的 id 来自当前 wake 注入的「📅 日程」段里\n"
            "  标注的 (#{id})。例如 `21:20 → 晚间复盘 (#NNN)` 就 reuse=NNN。\n"
            "  ⚠️ **不要凭记忆传 reuse id** — 上次 wake 看到的 id 可能已触发。\n"
            "  只用本次 wake 注入的日程清单里标注的 id，或先调 sense_schedule\n"
            "  查当前 pending 闹钟。21:00 选择或 reuse 不确定时，直接 rest(until=…) 新建更简单。\n"
            "\n"
            "## 不传任何参数(无参 rest)\n"
            "  rest() 无参 → 直接返睡前提示卡 + 列出当前 pending timer(\n"
            "  含 id)，模型从列表中选 reuse 或新建 until 到第二次真睡。\n"
            "\n"
            "## mental_context\n"
            "  给未来自己的留言(做到哪/下一步/卡点)。reuse 时会追加到原备注后。\n"
            "\n"
            "## 醒来\n"
            "  精力恢复后系统自动叫醒(可能比 until 早)。until 设真正想被叫醒的时间。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "until": {
                    "type": "string",
                    "description": "ISO8601 唤醒时间。和 reuse 至少填一个。",
                },
                "reuse": {
                    "type": "integer",
                    "description": "复用现有 timer 闹钟的 id（从 sense_schedule / 重复报错提示获得）。和 until 至少填一个。",
                },
                "hours": {"type": "number", "description": "（兼容）睡多少小时，until 优先"},
                "mental_context": {"type": "string", "description": "给未来自己的留言"},
                "reason": {"type": "string", "description": "为何休息（简短说明，内部记录）"},
                "confirm": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "显式跳过提示卡、直接真睡。default=false。一般不用传——"
                        "第二次调 rest（任意参数）也会自动真睡（系统记忆同 session 已展示过）。"
                        "仅在极少数场景（已处理完毕、希望一次调用直接睡）传 confirm=true。"
                    ),
                },
            },
        },
    },
    handler=_handle_rest,
    check_fn=lambda: True,
    emoji="😴",
    reveals_tools_on=["record_thought"],  # V6: preview 时暴露"写思绪"
)


__all__ = []


# ── 语音输出（express_to_human 的 voice 路径）──────────────────────────


def _send_voice_local(
    channel: str,
    text: str,
    context: dict,
    mention_user_ids: list,
) -> str:
    """语音输出通道：用 edge-tts 本地播放文本。

    不依赖飞书/微信 API，直接 TTS 播放。
    写 conversation_log（platform=voice）维护语音对话历史。
    返回和飞书/微信路径一致的 JSON 格式。

    注意：语音通道适合简短口语化回复。URL、文件路径、代码等技术细节
    应通过飞书等其他渠道发送——模型在收到 voice 通道时自行注意。
    """
    from interfaces.tools import registry

    if not text.strip():
        return registry.tool_error("voice: 文本为空")

    # TTS 播放
    sent = False
    err = None
    try:
        from infrastructure.config import get_app_instance_id
        from infrastructure.perception.voice_output import speak, is_tts_enabled, get_tts_voice, get_tts_rate

        iid = get_app_instance_id() or ""
        if not is_tts_enabled(iid):
            return registry.tool_error(
                "voice: TTS 未启用（app.yaml perception.tts_enabled: true）"
            )
        voice = get_tts_voice(iid)
        rate = get_tts_rate(iid)
        sent = speak(text, voice=voice, rate=rate)
    except FileNotFoundError:
        err = "/usr/bin/say not found（仅支持 macOS）"
    except Exception as exc:
        err = f"voice TTS error: {exc}"

    # 写 conversation_log（维护语音对话历史，sense_conversation 可查）
    if sent:
        try:
            from domain.lifecycle.conversation_log import log_conversation
            from infrastructure.config import get_instance_display_name

            out_sender = ""
            try:
                out_sender = (get_instance_display_name() or "").strip()
            except Exception:
                pass
            if not out_sender:
                out_sender = "你"

            log_conversation(
                platform="voice",
                conversation_id="voice_local_speaker",
                chat_type="dm",
                direction="out",
                text=text[:2000],  # 截断防 prompt 膨胀
                sender_name=out_sender,
            )
        except Exception as exc:
            logger.debug("voice conversation_log failed: %s", exc)

        # 撤收条（和飞书路径一致）
        try:
            from application.ingress.reaction_state import clear_all_reactions_sync
            clear_all_reactions_sync()
        except Exception:
            pass

    note = "已通过语音播放。" if sent else f"语音播放失败（{err}）。"
    if sent:
        note += " 这是语音通道——用户在听你说，不是在读。URL、文件路径、代码等技术细节不适合语音，如需发送请另用飞书。"
    note += " 沉默是你的默认状态 — 没必要每件事都发言。"

    return _j({
        "sent": sent,
        "channel": channel,
        "text_length": len(text),
        "error": err,
        "note": note,
        "segments_sent": 1 if sent else 0,
        "segments_total": 1,
    })


# ── WeChat ClawBot 发送（express_to_human 的 wechat 路径）──────────────

def _send_wechat_clawbot(
    channel: str,
    text: str,
    context: dict,
    mention_user_ids: list,
) -> str:
    """通过 ClawBot API 发送微信私聊消息。

    channel 格式：wechat:dm:<user_id>
    user_id 形如 xxx@im.wechat。

    ClawBot 限制：
      - 必须带 context_token（从收到的消息里取），否则不能发
      - 仅私聊，不支持群聊
      - 不能主动推送（必须有 context_token 关联对话）
    """
    parts = channel.split(":", 2)
    if len(parts) < 3:
        return json.dumps({"sent": False, "channel": channel, "error": "channel 格式错误，应为 wechat:dm:<user_id>"}, ensure_ascii=False)
    kind, target_id = parts[1], parts[2].strip()
    if kind != "dm":
        return json.dumps({"sent": False, "channel": channel, "error": "ClawBot 仅支持私聊（dm），不支持群聊"}, ensure_ascii=False)

    # 读 ClawBot 凭证
    from infrastructure.config import get_app_instance_id, get_project_root
    iid = get_app_instance_id()
    if not iid:
        return json.dumps({"sent": False, "error": "无法确定当前实例 ID"}, ensure_ascii=False)

    bot_token = ""
    import os as _os
    bot_token = (_os.getenv("WECHAT_BOT_TOKEN") or "").strip()
    if not bot_token:
        # 从实例 secrets.env 读
        secrets_path = get_project_root() / "apps" / iid / "config" / "secrets.env"
        if secrets_path.exists():
            for line in secrets_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("WECHAT_BOT_TOKEN="):
                    bot_token = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not bot_token:
        return json.dumps({"sent": False, "error": "WECHAT_BOT_TOKEN 未配置（在 secrets.env 中填）"}, ensure_ascii=False)

    # 从 runtime_context 拿 ClawBot context_token（handler 在入站时存的）
    context_token = ""
    try:
        from domain.lifecycle.runtime_context import get_current_context_token
        context_token = get_current_context_token() or ""
    except Exception:
        pass
    # fallback：从 _REPLY_CONTEXT 取（跨线程可见）
    if not context_token:
        try:
            ctx = _REPLY_CONTEXT.get(iid) or {}
            context_token = str(ctx.get("wechat_context_token") or "")
        except Exception:
            pass

    if not context_token:
        return json.dumps({
            "sent": False,
            "channel": channel,
            "error": "ClawBot 需要 context_token 才能回复（当前会话没有微信上下文）。ClawBot 不支持主动推送。",
        }, ensure_ascii=False)

    # 超长文本分段发送（替代旧 text[:2000] 静默截断）。
    # 详见 interfaces/ingress/text_segmenter.py。
    from interfaces.ingress.text_segmenter import split_text_for_send

    max_len = 2000  # ClawBot 单条上限，与 WeChatClawBotAdapter.capabilities.max_text_length 对齐
    segments = split_text_for_send(text, max_len)

    # ClawBot 发送 header（跟 getupdates 一样）
    import base64 as _b64
    import random as _rnd
    import uuid as _uuid
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Authorization": f"Bearer {bot_token}",
        "X-WECHAT-UIN": _b64.b64encode(str(_rnd.randint(0, 0xFFFFFFFF)).encode()).decode(),
        "iLink-App-Id": "bot",
        "iLink-App-ClientVersion": "132100",
    }

    # ClawBot 2.4.4 sendmessage 格式（来自 npm 包 send.js buildTextMessageReq）：
    # body = { msg: { to_user_id, client_id, message_type:2(BOT), message_state:2(FINISH),
    #                 item_list: [{type:1, text_item:{text:...}}], context_token } }
    def _do_send(seg_text: str) -> dict:
        import httpx
        payload = {
            "msg": {
                "from_user_id": "",
                "to_user_id": target_id,
                "client_id": f"openclaw-weixin-{_uuid.uuid4().hex[:16]}",
                "message_type": 2,
                "message_state": 2,
                "item_list": [{"type": 1, "text_item": {"text": seg_text}}],
                "context_token": context_token,
            }
        }
        with httpx.Client(timeout=30) as c:
            r = c.post(
                "https://ilinkai.weixin.qq.com/ilink/bot/sendmessage",
                headers=headers,
                json=payload,
            )
            r.raise_for_status()
            return r.json()

    import time as _time
    sent_count = 0
    first_err: str | None = None
    try:
        for idx, seg in enumerate(segments):
            try:
                result = _do_send(seg)
            except Exception as exc:
                # 单段失败：记录但不中断，继续发后续段（保留已发段）
                logger.warning(
                    "express_to_human wechat: segment %d/%d send failed: %s",
                    idx + 1, len(segments), exc,
                )
                if first_err is None:
                    first_err = str(exc)
            else:
                if isinstance(result, dict) and (
                    not result or result.get("ret") == 0 or result.get("errcode") == 0
                ):
                    sent_count += 1
                else:
                    if first_err is None:
                        first_err = f"ClawBot API returned: {result}"
                    logger.warning(
                        "express_to_human wechat: segment %d/%d rejected: %s",
                        idx + 1, len(segments), result,
                    )
            # ClawBot 限速比飞书紧，段间稍等；末段无需 sleep
            if idx < len(segments) - 1:
                _time.sleep(0.3)

        if sent_count > 0:
            logger.info(
                "express_to_human wechat: sent %d/%d segments OK (target=%s)",
                sent_count, len(segments), target_id[:20],
            )
            # 记录到 conversation_log：只记第一段，避免一条回复拆成 N 条污染对话史检索。
            # text 字段加尾标记表明这是分段回复的首段。
            first_seg = segments[0]
            log_text = first_seg if len(segments) == 1 else first_seg + " ……（共 %d 段）" % len(segments)
            try:
                from domain.lifecycle.conversation_log import log_conversation
                from infrastructure.config import get_instance_display_name
                out_sender = (get_instance_display_name() or "").strip() or "我"
                log_conversation(
                    platform="wechat",
                    conversation_id=target_id,
                    chat_type="dm",
                    direction="out",
                    text=log_text,
                    sender_name=out_sender,
                )
            except Exception as _le:
                logger.debug("wechat log_conversation failed: %s", _le)
            return json.dumps({
                "sent": True,
                "channel": channel,
                "text": first_seg,          # 向后兼容：取首段
                "segments_sent": sent_count,
                "segments_total": len(segments),
                **({"error": first_err} if first_err else {}),
            }, ensure_ascii=False)
        else:
            return json.dumps({
                "sent": False,
                "channel": channel,
                "segments_sent": 0,
                "segments_total": len(segments),
                "error": first_err or "ClawBot send failed",
            }, ensure_ascii=False)
    except Exception as exc:
        logger.error("express_to_human wechat send failed: %s", exc)
        return json.dumps({
            "sent": False,
            "channel": channel,
            "segments_sent": sent_count,
            "segments_total": len(segments),
            "error": str(exc),
        }, ensure_ascii=False)


def run_async_in_thread(coro_func):
    """在同步上下文里跑 async coroutine（用于 express_to_human 的同步 handler）。"""
    import threading
    result = [None]
    exc = [None]
    def _runner():
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            result[0] = loop.run_until_complete(coro_func)
        except Exception as e:
            exc[0] = e
        finally:
            loop.close()
    t = threading.Thread(target=_runner)
    t.start()
    t.join(timeout=35)
    if exc[0]:
        raise exc[0]
    return result[0]


# ──────────────────────────────── 退役工具(handler-only, schema 不暴露) ────────────────────────────────
# 这些工具的 schema 不再注入 system prompt(模型看不到自然不会主动调), 但 handler 保留:
#   - 历史 session 的 tool_calls 重放仍能 dispatch
#   - 模型偶尔幻觉调用时返回 _deprecated_hint, 引导改用新家
#
# 迁移目标见 commit 注释和 docs/design/tool-and-storage-rfc.md (待补)
# 退役时间: 2026-07-17

def _register_retired_handlers() -> None:
    """把退役工具以 handler-only 模式注册——schema 不暴露但 dispatch 可用。

    包在函数里惰性注册, 避免模块顶部 import 顺序问题。
    """
    registry.register_handler_only(
        name="manage_work",
        toolset="actions",
        handler=_handle_manage_work,
        description="[retired] → todo(action=...)",
        emoji="📝",
    )
    registry.register_handler_only(
        name="manage_goals",
        toolset="actions",
        handler=_handle_manage_goals,
        description="[retired] → todo(action='create', type='goal')",
        emoji="🎯",
    )
    registry.register_handler_only(
        name="manage_plan",
        toolset="actions",
        handler=_handle_manage_plan,
        description="[retired] → todo(type='goal') + todo_plan",
        emoji="📐",
    )
    registry.register_handler_only(
        name="manage_daily",
        toolset="actions",
        handler=_handle_manage_daily,
        description="[retired] → todo(type='daily') + timer 阔钟",
        emoji="📅",
    )
    registry.register_handler_only(
        name="remember_him",
        toolset="actions",
        handler=_handle_remember_him,
        description="[retired] → set_entity_profile / record_thought, 画像落 contacts.about",
        emoji="🌸",
    )


_register_retired_handlers()
