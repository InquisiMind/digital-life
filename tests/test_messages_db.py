"""Phase 1 回归:每实例独立 messages.db 的核心行为锁定。

设计参见 docs/architecture/decentralized-message-bus.md。
"""
from __future__ import annotations

from pathlib import Path


def _setup_module(tmp_path, monkeypatch):
    """让 domain.messages 用 tmp_path 作实例 data 目录。"""
    import domain.messages as M
    monkeypatch.setattr(M, "_INSTANCE_DATA_PATH_OVERRIDE", tmp_path)
    M._ensure_schema()
    return M


def test_record_inbound_and_list_roundtrip(tmp_path, monkeypatch):
    M = _setup_module(tmp_path, monkeypatch)

    rid = M.record_inbound(chat_id="oc_test", sender_id="ou_human",
                           sender_name="张三", text="hello",
                           msg_id="om_001", source="lark")
    assert rid > 0

    msgs = M.list_messages("oc_test", limit=10)
    assert len(msgs) == 1
    m = msgs[0]
    assert m["direction"] == "in"
    assert m["source"] == "lark"
    assert m["sender_name"] == "张三"
    assert m["sender_role"] == "human"
    assert m["text"] == "hello"
    assert m["msg_ref"] == "om_001"
    assert m["platform_sender"] == "ou_human"


def test_dedup_same_source_and_msg_ref(tmp_path, monkeypatch):
    M = _setup_module(tmp_path, monkeypatch)

    r1 = M.record_inbound(chat_id="oc_t", sender_id="ou_h", sender_name="张三",
                          text="hi", msg_id="om_x", source="lark")
    r2 = M.record_inbound(chat_id="oc_t", sender_id="ou_h", sender_name="张三",
                          text="hi", msg_id="om_x", source="lark")
    assert r1 == r2, "相同 (source, msg_ref) 必须去重,返回同一 row id"

    # 但不同 source 不去重(例如同 msg_id 在 lark vs broadcast: 视为两条不同消息)
    r3 = M.record_broadcast_in(chat_id="oc_t", from_display_name="alpha",
                                from_instance_id="uuid_a", text="hi",
                                msg_ref="om_x")
    assert r3 != r1, "source 不同即便 msg_ref 相同,应是不同行(lark msg_id 与 broadcast msg_id 各自独立)"


def test_chat_id_isolation(tmp_path, monkeypatch):
    M = _setup_module(tmp_path, monkeypatch)
    M.record_inbound(chat_id="oc_A", sender_id="ou_a", sender_name="A说", text="msg A")
    M.record_inbound(chat_id="oc_B", sender_id="ou_b", sender_name="B说", text="msg B")
    M.record_inbound(chat_id="oc_A", sender_id="ou_a", sender_name="A说2", text="msg A2")

    a_msgs = M.list_messages("oc_A", limit=10)
    b_msgs = M.list_messages("oc_B", limit=10)
    assert len(a_msgs) == 2
    assert len(b_msgs) == 1
    assert all(m["chat_id"] == "oc_A" for m in a_msgs)
    assert all(m["chat_id"] == "oc_B" for m in b_msgs)


def test_outbound_marks_self(tmp_path, monkeypatch):
    M = _setup_module(tmp_path, monkeypatch)
    rid = M.record_outbound(chat_id="oc_x", self_display_name="zero",
                             self_instance_id="uuid_zero",
                             text="hi all", msg_id="om_out_1", source="lark")
    assert rid > 0
    m = M.list_messages("oc_x", limit=1)[0]
    assert m["direction"] == "out"
    assert m["sender_role"] == "self"
    assert m["sender_name"] == "zero"
    assert m["platform_sender"] == "uuid_zero"


def test_broadcast_in_marks_bot_broadcast(tmp_path, monkeypatch):
    M = _setup_module(tmp_path, monkeypatch)
    rid = M.record_broadcast_in(chat_id="oc_x",
                                 from_display_name="alpha",
                                 from_instance_id="uuid_alpha",
                                 text="alpha said",
                                 msg_ref="om_alpha_1")
    assert rid > 0
    m = M.list_messages("oc_x", limit=1)[0]
    assert m["direction"] == "in"
    assert m["source"] == "broadcast:uuid_alpha"
    assert m["sender_role"] == "bot-broadcast"
    assert m["sender_name"] == "alpha"


def test_backward_compat_field_aliases(tmp_path, monkeypatch):
    """list_messages 返回的 dict 含旧 chat_messages 字段名,让上层无感切换."""
    M = _setup_module(tmp_path, monkeypatch)
    M.record_inbound(chat_id="oc_x", sender_id="ou_h", sender_name="张三",
                     text="hello", msg_id="om_1")
    m = M.list_messages("oc_x", limit=1)[0]
    # 旧字段别名(mesh 让 chat_stream 渲染层无缝兼容)
    assert m.get("msg_id") == "om_1"
    assert m.get("sender_id") == "ou_h"
    assert m.get("sender_kind") == "human"  # = sender_role
    assert m.get("created_at") is not None   # = ts


def test_list_plain_text_rendering(tmp_path, monkeypatch):
    M = _setup_module(tmp_path, monkeypatch)
    M.record_inbound(chat_id="oc_x", sender_id="ou_1", sender_name="张三",
                     text="hello", msg_id="om_1")
    M.record_outbound(chat_id="oc_x", self_display_name="zero",
                      self_instance_id="uuid_zero", text="hi", msg_id="om_2")
    M.record_broadcast_in(chat_id="oc_x", from_display_name="alpha",
                          from_instance_id="uuid_alpha",
                          text="alpha reply", msg_ref="om_3")

    pt = M.list_plain_text("oc_x", limit=10)
    assert "张三：hello" in pt
    assert "zero：hi" in pt
    assert "alpha：alpha reply" in pt


def test_record_skips_empty_chat_or_text(tmp_path, monkeypatch):
    M = _setup_module(tmp_path, monkeypatch)
    assert M.record_message(direction="in", source="lark", chat_id="",
                            text="x") is None
    assert M.record_message(direction="in", source="lark", chat_id="oc_x",
                            text="") is None


def test_instance_data_isolation(tmp_path, monkeypatch):
    """每实例 messages.db 路径独立 — 不同 CONTEXT 不同 db 文件。"""
    M = _setup_module(tmp_path, monkeypatch)
    p1 = M.messages_db_path()
    assert p1 == tmp_path / "messages.db"

    # 切到另一个目录(模拟另一个实例)
    tmp2 = tmp_path / "other_instance"
    monkeypatch.setattr(M, "_INSTANCE_DATA_PATH_OVERRIDE", tmp2)
    p2 = M.messages_db_path()
    assert p2 != p1
    assert p2 == tmp2 / "messages.db"
