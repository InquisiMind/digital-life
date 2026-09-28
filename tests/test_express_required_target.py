# -*- coding: utf-8 -*-
"""express_to_human 必填制测试（2026-09-24 zhp 定稿）。

三起误发事故（9/16、9/22、9/24）根治：留空 chat_id/channel 一律报错并附
「猜你想发的窗口」清单；不再走 LRU/事件源隐式路由。语音唤醒例外→voice:speaker。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from interfaces.tools.action_tools import _express_one


class _RT:
    """runtime_context 打桩：可设事件源窗口/平台。"""

    def __init__(self, chat_id="", platform="feishu"):
        self.chat_id, self.platform = chat_id, platform

    def get_current_event_chat_id(self):
        return self.chat_id

    def get_current_event_platform(self):
        return self.platform


def _patch_rt(monkeypatch, rt):
    import domain.lifecycle.runtime_context as rtc
    monkeypatch.setattr(rtc, "get_current_event_chat_id", rt.get_current_event_chat_id, raising=False)
    monkeypatch.setattr(rtc, "get_current_event_platform", rt.get_current_event_platform, raising=False)


def _patch_chats(monkeypatch, rows):
    import domain.contacts as contacts_mod
    monkeypatch.setattr(contacts_mod, "list_chats", lambda limit=50: rows, raising=False)


ROWS_3 = [
    {"chat_id": "oc_0d5fAAA", "name": "张浩普和他的跟班", "type": "group", "notes": "", "updated_at": "2026-09-24T14:58:00"},
    {"chat_id": "oc_818fBBB", "name": "张浩普", "type": "p2p", "notes": "", "updated_at": "2026-09-24T15:02:00"},
    {"chat_id": "oc_f2f6CCC", "name": "demo 协作群", "type": "group", "notes": "", "updated_at": "2026-09-24T11:02:00"},
]


def test_empty_target_errors_with_window_list(monkeypatch):
    _patch_rt(monkeypatch, _RT(chat_id="oc_0d5fAAA", platform="feishu"))
    _patch_chats(monkeypatch, ROWS_3)
    out = json.loads(_express_one({"text": "hi"}, channel=""))
    assert out.get("sent") is False
    assert "必填" in out["error"]
    assert "猜你想发的窗口" in out["error"]
    # 事件源标 ★ 且排首位
    first = out["candidates"][0]
    assert first["chat_id"] == "oc_0d5fAAA"
    assert out["error"].splitlines()[1].startswith("★ oc_0d5fAAA")
    # ≤6 窗口全列
    assert len(out["candidates"]) == 3


def test_more_than_six_takes_recent_five(monkeypatch):
    rows = [
        {"chat_id": "oc_w%03d" % i, "name": "群%d" % i, "type": "group", "notes": "",
         "updated_at": "2026-09-24T1%d:00:00" % i}
        for i in range(8)
    ]
    _patch_rt(monkeypatch, _RT(chat_id="", platform="feishu"))
    _patch_chats(monkeypatch, rows)
    out = json.loads(_express_one({"text": "hi"}, channel=""))
    assert out.get("sent") is False
    assert len(out["candidates"]) == 5  # 最近活跃 5 个


def test_voice_wake_uses_speaker(monkeypatch):
    _patch_rt(monkeypatch, _RT(chat_id="", platform="voice"))
    _patch_chats(monkeypatch, ROWS_3)
    out = json.loads(_express_one({"text": "hi"}, channel=""))
    # 语音例外：不进必填报错（channel 解析为 voice:speaker，后续 voice 渲染）
    assert not (out.get("sent") is False and "必填" in out.get("error", ""))


def test_default_marker_errors_not_lru(monkeypatch):
    _patch_rt(monkeypatch, _RT(chat_id="oc_818fBBB", platform="feishu"))
    _patch_chats(monkeypatch, ROWS_3)
    out = json.loads(_express_one({"text": "hi"}, channel="feishu:default"))
    assert out.get("sent") is False
    assert "已拦截" in out["error"]
    assert "猜你想发的窗口" in out["error"]


def test_explicit_chat_id_unaffected(monkeypatch):
    _patch_rt(monkeypatch, _RT(chat_id="oc_0d5fAAA", platform="feishu"))
    _patch_chats(monkeypatch, ROWS_3)
    out = json.loads(_express_one({"text": "hi", "chat_id": "oc_818fBBB"}, channel=""))
    # 显式 chat_id 正常走渠道解析，不进必填报错
    assert "必填" not in (out.get("error") or "")
