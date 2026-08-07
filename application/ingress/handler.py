"""消息入口处理 — 将飞书消息路由到正确的数字生命实例。

核心流程 (handle_message):
  1. resolve_instance() 解析消息应发往哪个实例
  2. 编排前置检查（orchestration preflight）
  3. 设置实例上下文（ContextVar + 环境变量）
  4. _route_to_life() → 发出 message/group_message 事件
  5. 如果 affair 为 BLOCKED/PENDING → 直接后台唤醒
  6. 如果 affair 为 RUNNING → 立即注入事件到运行中会话（不等 cron tick）

直接唤醒 vs 等待 tick：
  - BLOCKED 状态直接唤醒：用户发消息时应立即响应，不等 60s cron 周期
  - RUNNING 状态注入事件：避免 cron tick 等待导致连续消息被遗漏
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time

from interfaces.ingress.base import IngressAdapter, NormalizedMessage

logger = logging.getLogger(__name__)


def _sender_is_sibling_bot(sender_id: str, platform: str) -> str:
    """Return the sibling app_id if ``sender_id`` belongs to another
    digital-life instance on this platform. Empty string otherwise.

    Each instance configures its own Feishu app at ``apps/<id>/config/app.yaml``;
    the app_id is what Feishu reports as ``sender_id`` when a bot emits a
    message. Two bot instances in the same group will see each other's
    replies — without suppression, that creates an echo loop.

    We never suppress the *self* instance's own messages because Feishu's
    WS delivery already filters the sender's own posts.
    """
    if not sender_id or not platform:
        return ""
    try:
        from infrastructure.config import discover_instances, get_app_instance_id
    except Exception:
        return ""
    me = ""
    try:
        me = get_app_instance_id() or ""
    except Exception:
        pass
    try:
        import yaml as _yaml
        from pathlib import Path
        from infrastructure.config import get_project_root
        for iid in discover_instances():
            if iid == me:
                continue
            cfg_path = get_project_root() / "apps" / iid / "config" / "app.yaml"
            if not cfg_path.is_file():
                continue
            try:
                data = _yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            # 通道字段统一收敛到 channels.<type>；旧 messenger 段需先跑迁移脚本
            identities = []
            channels = data.get("channels")
            if isinstance(channels, dict):
                for ch_cfg in channels.values():
                    if isinstance(ch_cfg, dict):
                        aid = (ch_cfg.get("app_id") or "").strip()
                        if aid:
                            identities.append(aid)
                        bid = (ch_cfg.get("bot_id") or "").strip()
                        if bid:
                            identities.append(bid)
            for ident in identities:
                if ident and ident == sender_id:
                    return ident
    except Exception:
        return ""
    return ""


async def handle_message(*, adapter: IngressAdapter, msg: NormalizedMessage) -> bool:
    """处理一条入站消息：解析实例 → 发出事件 → 按需唤醒。

    完整流程：
      1. resolve_instance() 确定消息归属的数字生命实例
      2. 编排前置检查（orchestration preflight），命中则直接回复跳过
      3. 设置实例上下文（set_instance_context + 环境变量）
      4. _route_to_life() → 发出 message/group_message 事件
      5. BLOCKED/PENDING → 后台线程直接唤醒（不等 cron tick）
      6. RUNNING → 立即注入到运行中会话内存队列（不等 cron tick）
    """
    from application.ingress.router import resolve_instance

    instance_id = resolve_instance(msg)
    logger.info("Ingress message: platform=%s chat=%s -> instance=%s", msg.platform, msg.chat_id[:20], instance_id)

    # 黑名单拦截：sender_id 在黑名单中 → 直接丢弃，不 emit 事件、不唤醒
    if msg.sender_id:
        try:
            from domain.contacts import is_blocked, get_or_create_stub
            # platform 直接用 msg.platform（wechat/feishu/...）
            platform = msg.platform
            # 先查黑名单（若已 blocked 直接 drop，不消耗资源创建 stub）
            if is_blocked(platform, msg.sender_id):
                logger.info(
                    "Ingress drop: sender %s on %s is in blocklist",
                    msg.sender_id[:16], platform,
                )
                return False
            # 未知 sender 自动注册 stub 联系人（name 空，notes=auto-registered stub）
            # 前端可将其与现有联系人合并（编辑时把 platform_id 加到 named contact 上，
            # update_contact 会自动删除该 stub）。
            # 飞书 sender_type=='app' → 这是机器人（兄弟实例或第三方 bot），
            # 标记 kind='bot'，发送侧据此判断"@ 到了机器人→飞书会送达→本侧不再广播"。
            get_or_create_stub(platform, msg.sender_id, kind="bot" if msg.sender_is_bot else "human")
        except Exception:
            pass

    text = msg.content
    # 多模态附件：从 NormalizedMessage 提取 attachment 摘要（不含 local_path，避免 prompt 泄漏）
    # 每元素形如 {"attachment_id", "source", "source_key", "mime", "kind"}
    # handler 只读 attachment_id 列表（用于 record_inbound）+ 整体 attachments 摘要（用于 events payload）
    attachment_summaries: list[dict] = list(getattr(msg, "attachments", []) or [])
    attachment_ids: list[str] = [a.get("attachment_id", "") for a in attachment_summaries if a.get("attachment_id")]

    # ── Ignore Feishu's WS echo of sibling-bot messages ──
    # Two digital-life instances in the same Feishu group have POSIX
    # visibility into each other through two channels:
    #   1) Feishu's own WS fan-out (Feishu server delivers every bot's posts
    #      to every other bot in the chat) — purely reactive, no semantic
    #      context.
    #   2) The cross-instance aggregate DB at ``var/conversations/chats.db``
    #      + fan-out ``emit_event`` into the sibling's events table — the
    #      canonical channel by which one sibling perceives the other.
    # Channel (1) creates echo loops once the bots start replying. Channel
    # (2) is the right path; the message already lands in the sibling's
    # events queue via ``_fan_out_to_other_instances`` and triggers a wake
    # through the scheduler naturally.
    #
    # So when this handler sees a sibling sender on the WS-ingress side, we
    # ignore it completely (no chat aggregate write, no event emission, no
    # wake). The router-side wake will happen on the next cron tick or via
    # the BLOCKED→wake direct dispatch path.
    sibling_app_id = _sender_is_sibling_bot(msg.sender_id, msg.platform)
    if sibling_app_id:
        logger.info(
            "INGRESS_DROP reason=sibling_WS_echo app_id=%s chat=%r sender=%r — "
            "已由 broadcast 路径处理，本侧不 emit、不 wake",
            sibling_app_id[:16], msg.chat_id, msg.sender_id[:16],
        )
        return True

    # 三态收条-态 1: 入站已收到 → 加 ✅ 表情。
    # batch 合并时:merged_texts 里每条消息都加 ✅。
    # 后续 mark_processing 切 🤔,express_to_human 发送后全部撤。
    if not getattr(msg, "sender_is_bot", False):
        try:
            from application.ingress.reaction_state import register_received
            # 最新一条
            if msg.message_id:
                await register_received(msg.message_id, adapter)
            # batch 内其他条(marged_texts 带 msg_id)
            for item in (getattr(msg, "merged_texts", None) or []):
                mid = item.get("msg_id") if isinstance(item, dict) else ""
                if mid and mid != msg.message_id:
                    await register_received(mid, adapter)
        except Exception as _re:
            logger.debug("register_received failed: %s", _re)

    # Orchestration preflight
    orchestration_reply = _build_orchestration_reply(text)
    if orchestration_reply:
        await adapter.send(msg.chat_id, orchestration_reply, reply_to=msg.message_id)
        return True

    # /new 指令：强制下次唤醒开新 session（不接续 15min 内的旧 session，省 token）。
    #   /new（单独）→ 纯切 session，回复确认，不 emit 不 wake
    #   /new <正文> → 剥离前缀，用正文开新 session 唤醒
    # RUNNING 时延后生效：正文注入当前会话，下次唤醒才开新（不打断当前 wake）。
    _force_new_token = None
    if text and text.lstrip().startswith("/new"):
        stripped = text.lstrip()[4:].strip()  # len("/new") == 4
        from domain.lifecycle.scheduler import set_force_new_session, reset_force_new_session
        _force_new_token = set_force_new_session(True)
        if not stripped:
            # 纯切 session：不 emit、不 wake，直接回复确认
            try:
                await adapter.send(
                    msg.chat_id,
                    "✂️ 下次唤醒将开新会话（不接续历史）。",
                    reply_to=msg.message_id,
                )
            except Exception:
                pass
            reset_force_new_session(_force_new_token)
            return True
        text = stripped  # 用剩余正文继续走正常 emit（且 ContextVar 已标 force_new）

    # /observe 指令：触发一次屏幕+麦克风感知（等效快捷键，但不依赖辅助功能权限）。
    # 绕过 pynput 全局快捷键的 TCC 限制——飞书发 /observe 即可触发录制。
    #   /observe        → 截图 + 录 5 秒音 → 视觉+ASR → 注入 perception_signal
    #   /observe 10     → 录 10 秒（可选秒数，默认 5）
    if text and text.lstrip().startswith("/observe"):
        parts = text.lstrip().split()
        seconds = 5
        if len(parts) > 1:
            try:
                seconds = max(2, min(int(parts[1]), 30))
            except ValueError:
                pass
        try:
            await adapter.send(msg.chat_id, f"👁️ 观察中（{seconds}s）…", reply_to=msg.message_id)
        except Exception:
            pass
        # 异步触发感知（不阻塞飞书 handler）
        import asyncio
        asyncio.create_task(_trigger_perception_capture(instance_id, seconds, msg.chat_id, adapter))
        return True

    if text:
        prev_id = os.environ.get("DIGITAL_LIFE_INSTANCE_ID")
        os.environ["DIGITAL_LIFE_INSTANCE_ID"] = instance_id
        # Set instance context so events are written to the correct channel
        # (instance:{uuid}) — without this, events land in instance:zero.
        from domain.lifecycle.events import set_instance_context, reset_instance_context
        from infrastructure.config import set_current_instance_id, reset_current_instance_id
        ctx_token = set_instance_context(instance_id)
        config_token = set_current_instance_id(instance_id)
        try:
            await asyncio.to_thread(
                _route_to_life,
                text,
                instance_id=instance_id,
                is_group=msg.is_group,
                sender_name=msg.sender_name,
                sender_id=msg.sender_id,
                chat_name=msg.chat_name,
                chat_id=msg.chat_id,
                mentions_bot=msg.mentions_bot,
                mention_names=getattr(msg, "mention_names", []) or [],
                mention_map=getattr(msg, "mention_map", []) or [],
                msg_id=getattr(msg, "message_id", "") or "",
                # app_identity：从 adapter 自己拿（"平台知识唯一家园" = Adapter）。
                # 接第二个平台时只需该平台 adapter 实现 app_identity，这里零改动。
                app_id=getattr(adapter, "app_identity", "") or "",
                # 多通道：把 platform + ClawBot context_token 穿透到 express_to_human
                platform=msg.platform,
                ctx_token=getattr(msg, "context_token", "") or "",
                merged_texts=getattr(msg, "merged_texts", None) or None,
                adapter=adapter,
                # 多模态附件：从 NormalizedMessage 透传
                attachment_summaries=attachment_summaries,
                attachment_ids=attachment_ids,
            )
        finally:
            reset_current_instance_id(config_token)
            reset_instance_context(ctx_token)
            if prev_id:
                os.environ["DIGITAL_LIFE_INSTANCE_ID"] = prev_id
            if _force_new_token is not None:
                reset_force_new_session(_force_new_token)
    return True


async def _trigger_perception_capture(instance_id: str, seconds: int, chat_id: str, adapter) -> None:
    """/observe 命令的异步执行：截图+录音 → pipeline → emit perception_signal。

    截图用 mss（只需屏幕录制权限，已授权），录音用 sounddevice（需麦克风权限）。
    不依赖 pynput/辅助功能——绕过全局快捷键的 TCC 限制。
    """
    import asyncio

    def _capture_and_report():
        import time
        from pathlib import Path
        from infrastructure.perception.config import load_config, media_dir
        from infrastructure.perception.pipeline import run_pipeline

        cfg = load_config(instance_id)
        out_dir = media_dir(instance_id)
        ts = int(time.time())
        frames: list[str] = []
        audio_path: str | None = None

        # 截图（mss，不依赖辅助功能）
        try:
            import mss
            import mss.tools
            with mss.mss() as sct:
                monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
                # 按 fps 截几帧
                n_frames = max(1, min(int(seconds * cfg.frame_fps), cfg.max_frames))
                interval = seconds / n_frames if n_frames > 0 else 0.5
                for i in range(n_frames):
                    shot = sct.grab(monitor)
                    p = out_dir / f"observe_{ts}_{i:03d}.png"
                    mss.tools.to_png(shot.rgb, shot.size, output=str(p))
                    frames.append(str(p))
                    if i < n_frames - 1:
                        time.sleep(interval)
        except Exception as exc:
            logger.warning("/observe screenshot failed: %s", exc)

        # 录音（sounddevice）
        try:
            import wave
            import sounddevice as sd
            sr = 16000
            audio_path = str(out_dir / f"observe_{ts}.wav")
            data = sd.rec(int(seconds * sr), samplerate=sr, channels=1, dtype="int16")
            sd.wait()
            # 静音检测
            import numpy as np
            if int(np.abs(data).max()) < 100:
                logger.info("/observe audio is silent, skipping ASR")
                audio_path = None
            else:
                with wave.open(audio_path, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(sr)
                    wf.writeframes(data.tobytes())
        except Exception as exc:
            logger.warning("/observe audio capture failed: %s", exc)
            audio_path = None

        # 走 pipeline
        result = run_pipeline(
            instance_id=instance_id,
            source="observe_command",
            frame_image_paths=frames,
            audio_path=audio_path,
            media_path_for_record=frames[0] if frames else "",
            config=cfg,
        )
        return result

    try:
        result = await asyncio.to_thread(_capture_and_report)
        summary = result.summary or "（无内容）"
        try:
            await adapter.send(chat_id, f"👁️ 观察完成：{summary[:200]}")
        except Exception:
            pass
    except Exception as exc:
        logger.error("/observe failed: %s", exc)
        try:
            await adapter.send(chat_id, f"❌ 观察失败：{exc}")
        except Exception:
            pass


def _build_orchestration_reply(text: str) -> str | None:
    """编排前置检查：如果消息匹配编排规则，直接返回预置回复，跳过事件注入。"""
    try:
        from domain.orchestration import plan_gateway_reply

        reply = plan_gateway_reply(text)
        return reply.text if reply else None
    except Exception as exc:
        logger.debug("Orchestration preflight skipped: %s", exc)
        return None


def _route_to_life(
    text: str,
    *,
    instance_id: str = "",
    is_group: bool = False,
    sender_name: str = "",
    sender_id: str = "",
    chat_name: str = "",
    chat_id: str = "",
    mentions_bot: bool = False,
    mention_names: list = None,
    mention_map: list = None,
    msg_id: str = "",
    app_id: str = "",
    platform: str = "feishu",
    ctx_token: str = "",
    merged_texts: list = None,
    adapter=None,
    attachment_summaries: list = None,
    attachment_ids: list = None,
) -> None:
    """消息路由核心——发出事件并根据 affair 状态决定唤醒策略。

    三路分发：
      1. 发出 message/group_message 事件（_emit_l4_human_event）
      2. BLOCKED/PENDING → 立即后台线程唤醒（用户消息应立刻响应）
      3. RUNNING → 注入内存队列（_inject_msg_to_running_session，不等 cron tick）

    入站任何真人/群消息都意味着该实例上一轮发送已被看见——但仅限**同通道**。
    跨通道不要误取消（之前的 BUG：群 A 在等回复，群 B 来了无关消息 → 全局取消 →
    群 A 的 awaiting_reply 也被清掉，等不到 5min 自动 wake 的提醒）。

    修法（2026-06-16）：合成入站 channel 字符串（lark:<group|dm>:<chat_id>），
    调 cancel_alarms_by_filter 精确按通道取消。
    """
    # ⚠ 诊断日志[入口]：群消息穿透排查用。零行为变更。
    # 入口 conocer 一到就把入站身份信息打出来，便于和 EMIT_*/POP_*/CONSUME_* 串起来。
    logger.info(
        "INGRESS_BEGIN instance_id=%r is_group=%s sender=%r(%r) chat_id=%r "
        "mentions_bot=%s merged_count=%d platform=%s text_head=%r",
        instance_id, is_group, sender_name, sender_id,
        chat_id, mentions_bot,
        len(merged_texts) if merged_texts else 0, platform,
        (text[:60] + ("…" if len(text) > 60 else "")) if isinstance(text, str) else text,
    )

    try:
        from domain.lifecycle.alarms import cancel_alarms_by_filter

        # 1) 合成入站 channel 字符串（前缀按平台）
        inbound_channel = None
        if chat_id:
            kind = "group" if is_group else "dm"
            # 平台前缀：feishu→feishu（旧版曾归一为 lark，现已统一更直观的 feishu；
            # 读侧 cancel_alarms/scheduler 同时认 feishu: 与存量 lark:）。
            _prefix = "feishu" if platform == "feishu" else platform
            inbound_channel = f"{_prefix}:{kind}:{chat_id}"

        # 2) 按通道精确取消
        #    无 chat_id 时 fallback 全局取消（兜底，理论上不该发生）
        if inbound_channel:
            n = cancel_alarms_by_filter(
                "awaiting_reply",
                payload_filter={"channel": inbound_channel},
            )
        else:
            n = cancel_alarms_by_filter("awaiting_reply")
        if n > 0:
            logger.info("Inbound cancelled %d awaiting_reply alarm(s) on channel=%s",
                        n, inbound_channel or "(all)")
    except Exception:
        logger.exception("Failed to cancel awaiting_reply on inbound")

    event_id = _emit_l4_human_event(
        text,
        is_group=is_group,
        sender_name=sender_name,
        sender_id=sender_id,
        chat_name=chat_name,
        chat_id=chat_id,
        mentions_bot=mentions_bot,
        mention_names=mention_names,
        mention_map=mention_map,
        msg_id=msg_id,
        app_id=app_id,
        platform=platform,
        merged_texts=merged_texts,
        adapter=adapter,
        attachment_summaries=attachment_summaries or [],
        attachment_ids=attachment_ids or [],
    )
    logger.info(
        "EMIT_HUMAN_RESULT event_id=%d is_group=%s chat_id=%r "
        "instance_id=%r outcome=%s",
        event_id, is_group, chat_id, instance_id,
        "ok" if event_id > 0 else "FAILED-or-merged",
    )

    try:
        from interfaces.tools.action_tools import (
            set_dm_reply_context,
            set_group_reply_context,
        )

        # 不论是否 mid-session injection，每条消息都要更新 reply context：
        if chat_id:
            if is_group:
                set_group_reply_context(chat_id)
                set_dm_reply_context("")
                import interfaces.tools.action_tools as _at
                _at._DM_REPLY_CHAT_ID = None
            else:
                set_dm_reply_context(chat_id)
                set_group_reply_context("")
                import interfaces.tools.action_tools as _at
                _at._GROUP_REPLY_CHAT_ID = None

            # 多通道：存 platform + context_token 到 runtime_context（全链路可见）
            # 这是 ClawBot 发回复的必要条件
            try:
                from domain.lifecycle.runtime_context import (
                    set_current_event_platform,
                    set_current_context_token,
                    set_current_reply_msg_id,
                )
                set_current_event_platform(platform)
                set_current_context_token(ctx_token or "")
                # 三态收条:记录当前 wake 的入站原消息 id,供 express_to_human 发送后撤 ⚙️
                set_current_reply_msg_id(msg_id or "")
            except Exception:
                pass

            # 同时存 _REPLY_CONTEXT（为 express_to_human fallback 用）
            try:
                import interfaces.tools.action_tools as _at2
                # 用固定的 instance_id 作为 key + 设全局 mirror
                _at2._set_current_instance_id_mirror(instance_id)
                _iid = instance_id
                if _iid:
                    _ctx = _at2._REPLY_CONTEXT.get(_iid) or {}
                    _ctx["platform"] = platform
                    _ctx["chat_id"] = chat_id
                    _ctx["is_group"] = is_group
                    _ctx["wechat_context_token"] = ctx_token or ""
                    _at2._REPLY_CONTEXT[_iid] = _ctx
            except Exception:
                pass
        # scheduler 的 reply-context 设置只在 BLOCKED→wake 路径触发，
        # mid-session injection 时 scheduler 不再调用，会留下上轮 wake 的 stale context。
        # 这里根据当前消息类型把 group/dm 互相清干净 + 同步 wake_reason。
        if chat_id:
            if is_group:
                set_group_reply_context(chat_id)
                set_dm_reply_context("")  # 清 DM 污染
                import interfaces.tools.action_tools as _at
                _at._DM_REPLY_CHAT_ID = None
            else:
                set_dm_reply_context(chat_id)
                set_group_reply_context("")  # 清 group 污染
                import interfaces.tools.action_tools as _at
                _at._GROUP_REPLY_CHAT_ID = None

        # 同步 wake_reason：让 express_to_human 的 is_group_wake 判断看到最新消息类型，
        # 而不是 session 启动时的 wake 类型（避免 DM 在群聊 session 中响应到群）
        try:
            from domain.lifecycle.runtime_context import set_current_wake_reason
            set_current_wake_reason("group_message" if is_group else "message")
        except Exception:
            pass
    except Exception:
        pass

    # ⭐ refactor/emit-driven-wake 之后:emit 内部已经接管 wake 决策。
    #    handler 不再看 affair 状态、不起线程、不做 urgency 分流——只 emit 完走人。
    #    BLOCKED → _wake_or_inject 起线程叫醒(pop 全队列)
    #    RUNNING → _wake_or_inject 调 signal_new_events(mid-session 注入)
    #    失败 → 60s cron 兜底
    # handler 不在这里调 get_affair / wake_digital_life / _inject_msg_to_running_session
    # 任何一个——这些都在 events._wake_or_inject 内统一处理。
    logger.info("INGRESS_END reason=emitted_handler_no_wake_decision")
    return


# refactor/emit-driven-wake: _inject_msg_to_running_session / _mirror_inject_to_audit_turn
# 已经搬到 domain/lifecycle/events.py(_inject_to_running_session / _mirror_inject_to_audit_turn)。
# emit_event 在 affair==RUNNING 时自动调用,handler 不再自己决定何时 inject。




_GROUP_CHAT_CONFIG_CACHE: dict[str, dict] = {}
_GROUP_CHAT_CONFIG_MTIME: dict[str, float] = {}


def _load_group_chat_config() -> dict:
    """从当前实例的 app.yaml 读 group_chat 配置(attention_keywords / owner_names)。
    缓存按文件 mtime 失效,避免每次入站消息都打开 yaml。
    """
    try:
        from infrastructure.config import get_instance_app_config_path, get_app_instance_id
        instance_id = get_app_instance_id() or ""
        cfg_path = get_instance_app_config_path(instance_id)
        if not cfg_path.exists():
            return {}
        mtime = cfg_path.stat().st_mtime
        if instance_id in _GROUP_CHAT_CONFIG_CACHE and _GROUP_CHAT_CONFIG_MTIME.get(instance_id) == mtime:
            return _GROUP_CHAT_CONFIG_CACHE[instance_id]
        import yaml
        with open(cfg_path) as f:
            full = yaml.safe_load(f) or {}
        gc = full.get("group_chat") or {}
        _GROUP_CHAT_CONFIG_CACHE[instance_id] = gc
        _GROUP_CHAT_CONFIG_MTIME[instance_id] = mtime
        return gc
    except Exception as exc:
        # 历史 BUG: 之前吞错静默 return {}，导致 _classify_group_urgency
        # 永远拿不到 owner_names / attention_keywords，所有非 @ 群消息
        # 全部被判 soft 不立即 wake。改成 warning 暴露根因。
        logger.warning("_load_group_chat_config failed (instance=%s): %s",
                       get_app_instance_id() if 'get_app_instance_id' in dir() else "?", exc,
                       exc_info=True)
        return {}


def _unused_classify_urgency_removed() -> None:
    """Placeholder.

    refactor/emit-driven-wake 之后 handler 不再做 urgency 分流——延迟归
    ``group_buffer``(消息系统层,慢 timer 20-30s / 快 timer 0-5s),叫醒
    归 ``events._wake_or_inject``。这里曾经是 ``_classify_group_urgency``,
    但已经无人调用:外部 ``feishu.py`` 直接复用 ``_load_group_chat_config``
    读 keyword/owner list(它实际的判定逻辑在 group_buffer 的 ``_is_priority``
    里实现),不影响。

    若将来确实需要 emergency 内联分类,放在 ``wakeup_policy.py``
    (它已经是事件→wake 的策略集散地)。"""


def _sender_notes(sender_id: str, sender_name: str = "") -> str:
    """查发送者备注。仅看 per-instance contacts(按 feishu open_id)。
    返回 '[发送者备注] XXX'，空则空串。

    Phase 4:删除 unified_contacts 后备路径 — 决策 3 表明 per-instance notes
    就够模型识别真人了,不再维护跨 app 同步的备注表。
    """
    if not sender_id:
        return ""
    try:
        from domain.contacts.store import _lookup_contact
        c = _lookup_contact("feishu", sender_id)
        if c:
            notes = (c.get("notes") or "").strip()
            if notes:
                return f"[发送者备注] {notes}"
    except Exception:
        pass
    return ""


def _emit_l4_human_event(
    text: str,
    *,
    is_group: bool = False,
    sender_name: str = "",
    sender_id: str = "",
    chat_name: str = "",
    chat_id: str = "",
    mentions_bot: bool = False,
    mention_names: list = None,
    mention_map: list = None,
    msg_id: str = "",
    app_id: str = "",
    platform: str = "feishu",
    merged_texts: list = None,
    adapter=None,
    attachment_summaries: list = None,
    attachment_ids: list = None,
) -> int:
    """发出人类消息事件——消息入口的最后一步。
    流程：
      1. 群聊 → emit_event("group_message", channel="gateway:lark:group")
      2. 私聊 → emit_event("message", channel="gateway:lark:user")

    返回 event_id（-1 表示失败）。

    注：精力系统不挂消息入站钩子。前置版本的 apply_nurture(parse_message(text))
    实际只对 play 类关键词扣 -2 energy 而已,而且 NURTURE_KINDS 表设计本身就不
    是为了影响 energy（目标维度是 satiety/mood/bond,这些维度未实现）,所以本
    调用是个伪调用。精力只通过两个通道影响：consume_energy(agent LLM call）
    和 nurture_energy(前端加鸡腿）。
    """
    # 平台前缀（source/channel 用）：feishu→feishu（旧版曾归一为 lark，现在统一直观）
    pf = "feishu" if platform == "feishu" else platform

    # 已废弃的精力入站钩子残留——前置版本会在这里调 apply_nurture(parse_message(text))
    # 得到 kinds/deltas，但精力系统重构后这个调用被判定为"伪调用"删掉了。
    # 历史 BUG：删除时只删了 parse 调用，没把 kinds/deltas 也清空 → emit_event
    # payload 字段引用未定义变量 → NameError → 外层 except return -1 → 全部群消息
    # event_id=-1（无事件、空 wake、channel 错位"多个事件同时触发了"假警报）。
    # 14:12 wake #1220 / 15:06 / 15:08 复现链路就是这个 BUG 一路吃下去的。
    kinds: list = []
    deltas: dict = {}

    # 同函数内还引用了 now_iso() —— 也被当时的清理一并移除了 import 但留下了引用。
    # 不引入模块级 import 避免循环依赖，按需局部导入。
    from domain.lifecycle.clock import now_iso

    # 身份统一层：sender_id 从 per-app open_id 转为跨实例稳定的 unified_id。
    # Phase 4:删除 resolve_unified_id 调用——平台视角的 open_id 自洽,永不跨实例
    # 输出(决策 3)。下游 всех消费者(events / messages.db / wake_prompt)看到的
    # sender_id 就是平台 open_id;per-instance contacts.notes 让模型识别真人是谁。
    # 旧 unified_contacts 体系是无中生有的复杂度。
    try:
        from domain.lifecycle.events import emit_event

        if is_group:
            # 标注发送者岗位（如果在某个项目中）
            sender_position = ""
            try:
                from domain.project.loader import resolve_assignee_position
                pos_info = resolve_assignee_position(sender_id) if sender_id else None
                if pos_info:
                    sender_position = f"（{pos_info[0]} / {pos_info[1]}）"
            except Exception:
                pass
            # batch 历史渲染（如果 adapter 群消息 buffer 合并了多条群消息，
            # 见 interfaces/ingress/group_buffer.py，merged_texts 是 [{sender, text}]）
            _mt = merged_texts or []
            _mt_block = ""
            if _mt:
                lines = ["[近 30 秒合并群消息]"]
                for item in _mt:
                    s = (item.get("sender") if isinstance(item, dict) else "") or "?"
                    t = (item.get("text") if isinstance(item, dict) else "") or ""
                    lines.append(f"  {s}：{t}")
                _mt_block = "\n".join(lines)
            event_id = emit_event(
                kind="group_message",
                payload={
                    "text": text,
                    "sender_name": sender_name,
                    "sender_id": sender_id,
                    "sender_position": sender_position,
                    "sender_notes_block": _sender_notes(sender_id, sender_name),
                    "chat_name": chat_name,
                    "chat_id": chat_id,
                    "mentions_bot": mentions_bot,
                    "mention_names": "、".join(mention_names or []),
                    "source": f"gateway:{pf}",
                    "nurture_kinds": kinds,
                    "deltas": deltas,
                    "at": now_iso(),
                    "gateway_handled": True,
                    # batch 历史（adapter 30s + offset buffer 合并出来的,可能空）
                    "_merged_texts": _mt,
                    "_merged_texts_block": _mt_block,
                    # 多模态附件摘要（轻量，不含 local_path）——让模型在 wake prompt 看到"有图"
                    "attachments": attachment_summaries or [],
                },
                channel=f"gateway:{pf}:group",
            )
            logger.info("L4 group event emitted: chat_id=%s sender=%s @=%s", chat_id, sender_name, mentions_bot)
            # 记录入站消息到 conversation_log（之前在这里 return 漏写了，导致只有 out 没有 in）
            try:
                from domain.lifecycle.conversation_log import log_conversation
                log_conversation(
                    platform=pf,
                    conversation_id=chat_id,
                    chat_type="group",
                    direction="in",
                    text=text,
                    sender_name=sender_name,
                )
            except Exception:
                pass
            # 记录到聚合库（让所有实例看到这条 in）
            # sender_id 已在函数顶部统一为 unified_id；msg_id 透传做 UNIQUE 去重。
            try:
                from domain.conversations import record_inbound_message
                record_inbound_message(
                    chat_id,
                    sender_id=sender_id,
                    sender_name=sender_name,
                    text=text,
                    msg_id=msg_id,
                    sender_kind="human",
                    attachments=attachment_ids if attachment_ids else None,
                )
            except Exception as exc:
                # 之前是 except: pass + 引用了未定义的 msg 变量，导致聚合库 5 天 0 条 human 消息。
                # 改为 warning 暴露根因，便于定位。
                logger.warning("record_inbound_message failed (chat=%s sender=%s): %s",
                               chat_id[:16], sender_name, exc, exc_info=True)
            return event_id

        event_id = emit_event(
            kind="message",
            payload={
                "text": text,
                "sender_name": sender_name,
                "sender_id": sender_id,
                "sender_notes_block": _sender_notes(sender_id, sender_name),
                "chat_id": chat_id,
                "source": f"gateway:{pf}",
                "nurture_kinds": kinds,
                "deltas": deltas,
                "at": now_iso(),
                "gateway_handled": True,
                # 多模态附件摘要（与 group_message 一致）
                "attachments": attachment_summaries,
            },
            channel=f"gateway:{pf}:user",
        )
        logger.info("L4 human event emitted: nurture=%s", kinds)
        # 记录到对话日志 + 设置当前对话上下文
        try:
            from domain.lifecycle.runtime_context import set_current_conversation_id
            set_current_conversation_id(chat_id)
        except Exception:
            pass
        try:
            from domain.lifecycle.conversation_log import log_conversation
            log_conversation(
                platform=pf,
                conversation_id=chat_id,
                chat_type="group" if is_group else "dm",
                direction="in",
                text=text,
                sender_name=sender_name,
            )
        except Exception:
            pass
        return event_id
    except Exception as exc:
        logger.warning("Failed to emit l4 human event: %s", exc)
        return -1
