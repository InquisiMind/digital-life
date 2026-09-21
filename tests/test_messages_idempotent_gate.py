"""messages 幂等闸门 + 裸连接保护回归（9/21 重复事件 BUG 修复）。

背景: zhp 10:22 报事件重复触发。根因两层:
  1. messages.db 全链路唯一裸奔的库——4+1 处 sqlite3.connect() 无 busy_timeout,
     撞锁即 SQLITE_BUSY,发送成功但落库失败 → 事件不销账 → 重投。
  2. record_message 虽是 INSERT OR IGNORE + UNIQUE(source,msg_ref), 但
     cursor.rowcount 的"新插入 vs 重复"信号没传出, 冲突返回已有行 id 与
     lastrowid 无法区分; 且 handler 里 emit_event 在 record 之前, 平台重推
     同一 msg_id 时库幂等忽略但事件已投出。

修复语义:
  - record_message → (row_id, inserted): rowcount==1 新插入 / ==0 确认重复 /
    落库失败 → (None, False)
  - handler 闸门前移: 确认重复 → 跳过 emit 返回 0; 落库失败 → 照投(宁重复不丢失)
  - 所有 messages 连接 busy_timeout >= 5000ms
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


def _setup_messages(tmp_path: Path, monkeypatch):
    import domain.messages as M
    monkeypatch.setattr(M, "_INSTANCE_DATA_PATH_OVERRIDE", tmp_path)
    M._ensure_schema()
    return M


# ── 1. record_message 返回值三元语义 ────────────────────────────────────


def test_record_message_new_insert_returns_true(tmp_path, monkeypatch):
    M = _setup_messages(tmp_path, monkeypatch)
    row_id, inserted = M.record_message(
        direction="in", source="feishu", chat_id="oc_1",
        text="hi", msg_ref="om_1",
    )
    assert inserted is True and row_id > 0


def test_record_message_duplicate_returns_false_same_row(tmp_path, monkeypatch):
    M = _setup_messages(tmp_path, monkeypatch)
    rid1, ins1 = M.record_message(
        direction="in", source="feishu", chat_id="oc_1",
        text="hi", msg_ref="om_1",
    )
    rid2, ins2 = M.record_message(  # 同 source+msg_ref 重放
        direction="in", source="feishu", chat_id="oc_1",
        text="hi", msg_ref="om_1",
    )
    assert ins1 is True
    assert ins2 is False, "UNIQUE(source,msg_ref) 冲突应报 inserted=False"
    assert rid2 == rid1, "冲突时应返回已有行 id（不是 None）"


def test_record_message_db_failure_returns_none(tmp_path, monkeypatch):
    M = _setup_messages(tmp_path, monkeypatch)
    # 打桩内部连接,模拟 BUSY/损坏等落库失败
    def _boom(*a, **kw):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(M, "_connect", _boom)
    rid, inserted = M.record_message(
        direction="in", source="feishu", chat_id="oc_1",
        text="x", msg_ref="om_z",
    )
    assert rid is None and inserted is False


def test_record_inbound_outbound_pass_through(tmp_path, monkeypatch):
    """record_inbound / record_outbound 应原样透传 (row_id, inserted)。"""
    M = _setup_messages(tmp_path, monkeypatch)
    r1 = M.record_inbound(
        chat_id="oc_1", sender_id="ou_x", sender_name="x",
        text="in", msg_id="om_in",
    )
    r2 = M.record_inbound(
        chat_id="oc_1", sender_id="ou_x", sender_name="x",
        text="in", msg_id="om_in",
    )  # 重复
    assert r1[1] is True and r2[1] is False and r2[0] == r1[0]
    o1 = M.record_outbound(
        chat_id="oc_1", text="out", msg_id="om_out",
        self_display_name="zero", self_instance_id="i-0",
    )
    o2 = M.record_outbound(  # 重复
        chat_id="oc_1", text="out", msg_id="om_out",
        self_display_name="zero", self_instance_id="i-0",
    )
    assert o1[1] is True and o2[1] is False


def test_publish_chat_message_keeps_int_contract(tmp_path, monkeypatch):
    """conversations.publish_chat_message 对外仍返回 int(内部解包)。"""
    M = _setup_messages(tmp_path, monkeypatch)
    import domain.messages.broadcast as B
    monkeypatch.setattr(B, "broadcast_outbound", lambda **kw: 0)
    import infrastructure.config as cfg
    monkeypatch.setattr(cfg, "get_app_instance_id", lambda: "uuid_self")
    monkeypatch.setattr(cfg, "get_instance_display_name", lambda: "self")
    from domain.conversations import publish_chat_message
    rid = publish_chat_message(
        chat_id="oc_1", sender_id="uuid_self", sender_name="self",
        text="hi", msg_id="", sender_kind="bot", broadcast=False,
    )
    assert isinstance(rid, int) and rid > 0
    # 出站幂等: 同 msg_id 重复发布, 返回已有行 id 仍是 int
    rid2 = publish_chat_message(
        chat_id="oc_1", sender_id="uuid_self", sender_name="self",
        text="hi", msg_id="", sender_kind="bot", broadcast=False,
    )
    assert isinstance(rid2, int) and rid2 > 0


# ── 2. messages 连接 busy_timeout 保护 ─────────────────────────────────


def test_messages_connections_have_busy_timeout(tmp_path, monkeypatch):
    """域层经 _connect 打开的 messages 连接应带 busy_timeout>=5000。

    messages.db 是全链路唯一曾裸奔(无 WAL 配置/无 busy_timeout)的库,
    9/21 重复事件 BUG 的撞锁现场。WAL 已由 alpha 库级打好,这里锁连接级。
    """
    M = _setup_messages(tmp_path, monkeypatch)
    # 任取一个写路径触发连接
    M.record_message(
        direction="in", source="feishu", chat_id="oc_bt",
        text="x", msg_ref="om_bt",
    )
    conn = sqlite3.connect(str(tmp_path / "messages.db"))
    try:
        # 连接级 PRAGMA 只对设置它的连接可见 → 直接读域层 helper 的行为
        c2 = M._connect(M.messages_db_path())
        try:
            val = c2.execute("PRAGMA busy_timeout;").fetchone()[0]
            assert val >= 5000, f"busy_timeout={val}, 应 >=5000ms"
        finally:
            c2.close()
    finally:
        conn.close()


def test_module_has_no_bare_connect_left():
    """domain/messages/__init__.py 里不应再有裸 sqlite3.connect(不走 _connect)。"""
    import domain.messages as M
    import inspect
    src = inspect.getsource(M)
    allowed = {"conn = sqlite3.connect(str(p))"}  # _connect 本体唯一允许行
    bare = [
        ln.strip() for ln in src.splitlines()
        if "sqlite3.connect(" in ln and ln.strip() not in allowed
        and not ln.strip().startswith(('#', '"', 'f"', '...'))
        and "没设" not in ln  # docstring 提及
    ]
    assert not bare, f"仍有裸连接绕过 _connect: {bare}"


# ── 3. handler 闸门: 重复跳过 emit / 失败照投 ────────────────────────────


def _emit_l4(monkeypatch, record_ret):
    """调 _emit_l4_human_event, 打桩 record 与 emit_event, 返回 (ret, emit_calls)。"""
    import application.ingress.handler as H
    import domain.lifecycle.events as EV
    import domain.conversations as CV

    calls = {"emit": 0}

    def _fake_record_inbound(*a, **kw):
        return record_ret

    def _fake_emit(*, kind, payload, channel, **kw):
        calls["emit"] += 1
        return 12345

    monkeypatch.setattr(CV, "record_inbound_message", _fake_record_inbound)
    monkeypatch.setattr(EV, "emit_event", _fake_emit)
    ret = H._emit_l4_human_event(
        "hi", is_group=True, sender_name="u", sender_id="ou_x",
        chat_name="g", chat_id="oc_1", mentions_bot=True,
        mention_names=[], msg_id="om_1", platform="feishu",
    )
    return ret, calls["emit"]


def test_gate_skips_emit_on_confirmed_duplicate(monkeypatch):
    """确认重复(inserted=False) → 跳过 emit, 返回 0, 不产生新事件。"""
    ret, n = _emit_l4(monkeypatch, record_ret=(7, False))
    assert ret == 0
    assert n == 0, "重复消息不应再投事件"


def test_gate_emits_on_db_failure(monkeypatch):
    """落库失败(row_id=None) → 照投不误(宁重复不丢失)。"""
    ret, n = _emit_l4(monkeypatch, record_ret=(None, False))
    assert n == 1, "落库失败必须照投,否则丢消息"
    assert ret == 12345


def test_gate_emits_on_first_insert(monkeypatch):
    """首次入库(inserted=True) → 正常投递。"""
    ret, n = _emit_l4(monkeypatch, record_ret=(7, True))
    assert n == 1 and ret == 12345
