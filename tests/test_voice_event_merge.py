"""语音事件合批（perception_signal accumulate 防抖）测试。

2026-10-30 zhp 需求：模型思考期间连续到来的语音句（事件 2、3……）应合并进
同一条事件一次注入，而不是一句一个事件逐条 midsession 注入。

机制 = 复用既有防抖合并（``_apply_debounce``，按 event_types.yaml 逐事件配置）：
perception_signal 开 ``debounce_window_s: 8`` + ``merge_policy: accumulate``。
窗口由消费闸门自然封口——事件被消费后合并链断开。

语义锚点：
- 窗口内第二条语音句 → 合并进同一条事件（_merged_texts 逐条带 at 时间）
- 事件被消费后再来一句 → 新事件（消费即封口）
- 唤醒模板 {_merged_texts_block} 渲染出合并的逐条内容（含时间标注）
- 消费竞态守卫：UPDATE 只打未消费行（rowcount=0 → 让位给新 INSERT）
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest


def _make_env(instance_id: str) -> tuple[str, Path]:
    tmp = tempfile.mkdtemp()
    db_path = Path(tmp) / "state.db"
    os.environ["DIGITAL_LIFE_INSTANCE_ID"] = instance_id
    os.environ["DIGITAL_LIFE_RUNTIME_HOME"] = tmp

    from domain.lifecycle.affairs.runtime import configure_runtime_hooks, init_db

    configure_runtime_hooks(db_path=db_path)
    init_db()
    return tmp, db_path


def _cleanup(tmp: str) -> None:
    shutil.rmtree(tmp, ignore_errors=True)
    os.environ.pop("DIGITAL_LIFE_INSTANCE_ID", None)
    os.environ.pop("DIGITAL_LIFE_RUNTIME_HOME", None)


@pytest.fixture()
def voice_env():
    tmp, db_path = _make_env("test-voice-merge")
    yield db_path
    _cleanup(tmp)


def _ctx(instance_id: str):
    from domain.lifecycle.events import set_instance_context

    return set_instance_context(instance_id)


def _reset(token) -> None:
    from domain.lifecycle.events import reset_instance_context

    reset_instance_context(token)


def _voice_payload(text: str, at: str = "2026-10-30T11:00:00+08:00") -> dict:
    return {
        "source": "voice_session",
        "summary": text,
        "transcript": text,
        "ok": True,
        "reply_channel": "voice",
        "at": at,
    }


def test_voice_events_merge_into_one(voice_env):
    """窗口内第二句合并进同一事件：_merged_texts 逐条带时间，主字段留最新。"""
    from domain.lifecycle.events import emit_event, peek_unconsumed_event

    token = _ctx("test-voice-merge")
    try:
        eid1 = emit_event("perception_signal", payload=_voice_payload("你好", "2026-10-30T11:00:01+08:00"))
        assert eid1  # 订阅闸放行（测试实例走默认全订阅）

        eid2 = emit_event("perception_signal", payload=_voice_payload("你在么", "2026-10-30T11:00:05+08:00"))
        assert eid2 == eid1, "窗口内第二句应合并进同一条事件"

        ev = peek_unconsumed_event(eid1)
        assert ev is not None
        payload = ev["payload"]
        assert payload["_merged_count"] == 2
        assert payload["transcript"] == "你在么", "主字段保留最新一条"
        assert payload["at"] == "2026-10-30T11:00:05+08:00"
        texts = payload["_merged_texts"]
        assert [t["text"] for t in texts] == ["你好", "你在么"]
        assert all(not t.get("sender") for t in texts), "语音无 sender，条目不带发话者"
        assert texts[0]["at"] == "2026-10-30T11:00:01+08:00"
        assert texts[1]["at"] == "2026-10-30T11:00:05+08:00"

        # 第三句继续并进同一链（created_at 滚动刷新，链不断）
        eid3 = emit_event("perception_signal", payload=_voice_payload("今天怎么样", "2026-10-30T11:00:09+08:00"))
        assert eid3 == eid1
        payload3 = peek_unconsumed_event(eid1)["payload"]
        assert payload3["_merged_count"] == 3
        assert [t["text"] for t in payload3["_merged_texts"]] == ["你好", "你在么", "今天怎么样"]
    finally:
        _reset(token)


def test_consumed_event_breaks_merge_chain(voice_env):
    """消费即封口：事件被模型接走（consume）后再来一句 → 全新事件。"""
    from domain.lifecycle.events import consume_event, emit_event, peek_unconsumed_event

    token = _ctx("test-voice-merge")
    try:
        eid1 = emit_event("perception_signal", payload=_voice_payload("你好"))
        consume_event(eid1)

        eid2 = emit_event("perception_signal", payload=_voice_payload("下一轮的这句是新事件"))
        assert eid2 != eid1, "已消费目标不可再作合并宿主"

        payload2 = peek_unconsumed_event(eid2)["payload"]
        assert "_merged_texts" not in payload2
        assert payload2["transcript"] == "下一轮的这句是新事件"
    finally:
        _reset(token)


def test_merged_block_rendered_in_wake_prompt(voice_env):
    """唤醒模板 {_merged_texts_block}：合并的逐句内容 + 逐条时间标注可见。"""
    from domain.lifecycle.events import emit_event, peek_unconsumed_event
    from domain.lifecycle.heartbeat import _resolve_event_prompt

    token = _ctx("test-voice-merge")
    try:
        eid = emit_event("perception_signal", payload=_voice_payload("你好", "2026-10-30T11:00:01+08:00"))
        emit_event("perception_signal", payload=_voice_payload("今天怎么样", "2026-10-30T11:00:09+08:00"))

        ev = peek_unconsumed_event(eid)
        rendered = _resolve_event_prompt("perception_signal", [ev])
        assert "今天怎么样" in rendered, "主行 = 最新一条"
        assert "之前合并的 1 条消息" in rendered
        assert "你好" in rendered, "合并历史里的旧句也要可见"
        assert "11:00:01" in rendered, "逐条时间标注（zhp：合并行要有 xx时间）"
        assert "？：" not in rendered and "?:" not in rendered, "语音无 sender，行首不应有发话者前缀"
    finally:
        _reset(token)


def test_group_message_merge_still_has_sender(voice_env):
    """群消息 accumulate 回归：sender 正常入档、带前缀渲染（不被语音改动破坏）。"""
    from domain.lifecycle.events import emit_event, peek_unconsumed_event
    from domain.lifecycle.heartbeat import _resolve_event_prompt

    token = _ctx("test-voice-merge")
    try:
        eid = emit_event("group_message", payload={
            "text": "第一条", "sender_name": "合伙人", "chat_id": "oc_test",
            "at": "2026-10-30T11:00:01+08:00",
        })
        emit_event("group_message", payload={
            "text": "第二条", "sender_name": "质控", "chat_id": "oc_test",
            "at": "2026-10-30T11:00:03+08:00",
        })
        payload = peek_unconsumed_event(eid)["payload"]
        assert payload["_merged_count"] == 2
        senders = [t.get("sender") for t in payload["_merged_texts"]]
        assert senders == ["合伙人", "质控"]

        rendered = _resolve_event_prompt("group_message", [peek_unconsumed_event(eid)])
        assert "合伙人：第一条" in rendered
        assert "质控：第二条" in rendered
    finally:
        _reset(token)
