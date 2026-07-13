"""Phase 3 回归:广播机制 + urgency 分流 + 订阅配置锁定行为。

设计参见 docs/architecture/decentralized-message-bus.md 决策 1-5。
"""
from __future__ import annotations

from pathlib import Path

import domain.messages as M
import domain.messages.broadcast as B


def _setup_messages(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "_INSTANCE_DATA_PATH_OVERRIDE", tmp_path)
    M._ensure_schema()
    return M


def test_peer_subscription_roundtrip(tmp_path, monkeypatch):
    """sync_subscriptions 应该从 instances.yaml + chat_ids 推导生成。

    用 monkeypatch 简化:不走真实 instances.yaml,直接测 save/load。"""
    subs_in = {
        "oc_test_group": B.Subscription(
            chat_id="oc_test_group",
            platform="lark",
            peers=[B.Peer(uuid="other-iid-1234",
                          endpoint="http://localhost:8642/internal/message-broadcast")],
            name="测试群",
        )
    }
    B.save_subscriptions("test-self-iid", subs_in)
    subs_out = B.load_subscriptions("test-self-iid")
    assert "oc_test_group" in subs_out
    s = subs_out["oc_test_group"]
    assert s.platform == "lark"
    assert s.name == "测试群"
    assert len(s.peers) == 1
    assert s.peers[0].uuid == "other-iid-1234"
    assert "internal/message-broadcast" in s.peers[0].endpoint


def test_broadcast_outbound_fire_and_forget(tmp_path, monkeypatch):
    """广播调用失败不应抛异常,只 log warning。"""
    _setup_messages(tmp_path, monkeypatch)
    # 不存在任何 peer(无 subscriptions.yaml 加载),broadcast_outbound 应返回 0 不抛
    n = B.broadcast_outbound(
        from_instance_id="self-iid",
        from_display_name="zero",
        chat_id="oc_x",
        text="hi",
        msg_ref="om_1",
    )
    assert n == 0


def _setup_master_fake(tmp_path, monkeypatch, from_iid, peer_iid, chat="oc_group"):
    """构造 master 进程上下文:两个 fake 实例 + 双向订阅表 + 无 ContextVar。

    返回 peer_iid(tests 借此验证只写入 peer,不回写 from)。
    """
    import os
    import infrastructure.config as cfg

    for iid, other in [(from_iid, peer_iid), (peer_iid, from_iid)]:
        instance_dir = tmp_path / "apps" / iid
        (instance_dir / "data").mkdir(parents=True)
        (instance_dir / "config").mkdir(parents=True)
        (instance_dir / "config" / "subscriptions.yaml").write_text(
            f"subscriptions:\n"
            f"  {chat}:\n"
            f"    platform: lark\n"
            f"    name: test\n"
            f"    peers:\n"
            f"    - uuid: {other}\n"
            f"      endpoint: http://127.0.0.1:8642/internal/message-broadcast\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(cfg, "get_project_root", lambda: tmp_path)
    monkeypatch.setattr(cfg, "get_instance_dir",
                        lambda iid=None: tmp_path / "apps" / (iid or ""))
    monkeypatch.setattr(cfg, "get_instance_data_dir",
                        lambda iid=None: (tmp_path / "apps" / (iid or "") / "data"))
    monkeypatch.setattr(cfg, "discover_active_instances",
                        lambda: [from_iid, peer_iid])
    for k in ("DIGITAL_LIFE_INSTANCE_ID", "L4_AGENT_ID", "DIGITAL_LIFE_EMPLOYEE_ID"):
        os.environ.pop(k, None)
    return peer_iid


def test_receive_broadcast_writes_inbound_and_returns_ok(tmp_path, monkeypatch):
    """receive_broadcast 应该 peer 消息入 messages.db,sender_role='bot-broadcast'。"""
    from_iid, peer_iid = "alpha-uuid-test", "beta-uuid-test"
    _setup_master_fake(tmp_path, monkeypatch, from_iid, peer_iid, chat="oc_group")
    result = B.receive_broadcast({
        "from_instance_id": from_iid,
        "from_display_name": "alpha",
        "chat_id": "oc_group",
        "text": "hello from alpha",
        "msg_ref": "om_from_alpha_1",
        "source_platform": "lark",
    })
    assert result["ok"] is True
    assert result["delivered"] == 1, result

    # 验证 peer(beta)库里有这条(from(alpha)库不应有)
    import sqlite3
    peer_db = sqlite3.connect(tmp_path / "apps" / peer_iid / "data" / "messages.db")
    peer_db.row_factory = sqlite3.Row
    try:
        rows = peer_db.execute(
            "SELECT source, sender_name, sender_role, text FROM messages "
            "WHERE chat_id=? ORDER BY id DESC LIMIT 5", ("oc_group",)
        ).fetchall()
    finally:
        peer_db.close()
    assert any(r["sender_name"] == "alpha"
               and r["sender_role"] == "bot-broadcast"
               and r["source"] == f"broadcast:{from_iid}"
               and "hello from alpha" in r["text"]
               for r in rows), f"广播消息没进 peer 库: {rows}"


def test_receive_broadcast_idempotent(tmp_path, monkeypatch):
    """同一 msg_ref 重复广播去重(UNIQUE 约束)——只入一次 peer 的 messages.db。"""
    from_iid, peer_iid = "from-iid-x", "peer-iid-x"
    _setup_master_fake(tmp_path, monkeypatch, from_iid, peer_iid, chat="oc_dup")
    payload = {
        "from_instance_id": from_iid,
        "from_display_name": "alpha",
        "chat_id": "oc_dup",
        "text": "dup msg",
        "msg_ref": "om_dup_1",
    }
    B.receive_broadcast(payload)
    B.receive_broadcast(payload)
    B.receive_broadcast(payload)
    # messages.db 只有一条(emit_event 那段可能重复尝试;我们只关心 messages.db 去重)
    import sqlite3
    peer_db = sqlite3.connect(tmp_path / "apps" / peer_iid / "data" / "messages.db")
    try:
        n = peer_db.execute(
            "SELECT COUNT(*) FROM messages WHERE text='dup msg'"
        ).fetchone()[0]
    finally:
        peer_db.close()
    assert n == 1, f"peer messages.db 应只有 1 条,实际 {n}"


def test_receive_broadcast_rejects_missing_fields(tmp_path, monkeypatch):
    _setup_messages(tmp_path, monkeypatch)
    result = B.receive_broadcast({"text": "no chat_id"})
    assert result["ok"] is False
    assert "missing" in result["reason"]


def test_broadcast_outbound_message_does_not_double_back(tmp_path, monkeypatch):
    """广播出去后到达 peer, peer 的 messages.db 应为 direction='in'(而非 out)。
    即:peer 入库后调用 emit_event 然后 cron 想 wake 时不会再次循环广播回 sender。
    """
    from_iid, peer_iid = "zero-uuid", "alpha-uuid"
    _setup_master_fake(tmp_path, monkeypatch, from_iid, peer_iid, chat="oc_xxx")
    B.receive_broadcast({
        "from_instance_id": from_iid,
        "from_display_name": "zero",
        "chat_id": "oc_xxx",
        "text": "zero says hi",
        "msg_ref": "om_z_1",
    })
    # 只看 peer 库的 direction
    import sqlite3
    peer_db = sqlite3.connect(tmp_path / "apps" / peer_iid / "data" / "messages.db")
    try:
        rows = peer_db.execute(
            "SELECT direction FROM messages WHERE chat_id='oc_xxx'"
        ).fetchall()
    finally:
        peer_db.close()
    assert rows and all(d == "in" for (d,) in rows), \
        f"peer 收到的广播必须是 direction='in',实际 {rows}"


def test_receive_broadcast_routes_to_correct_subscribers(
    tmp_path, monkeypatch
):
    """master 中转:payload 不含'接收方是谁',但 master 必须按 group→subscribers
    订阅表反查,写到所有非 from 的订阅者各自库——不能写到 from 自己。

    历史 BUG:receive_broadcast 走 master 进程 ContextVar 默认实例,导致 alpha→zero
    广播被错写到 alpha(master 默认)库,zero 永远收不到。
    """
    import os
    import sqlite3

    # 构造 mock apps/<id>/ 目录树,每个实例都有 data/ 和 config/subscriptions.yaml
    fake_root = tmp_path
    alpha_iid = "alpha-iid-0001"
    zero_iid = "zero-iid-0002"
    chat = "oc_test_group"

    for iid, peer_iid in [(alpha_iid, zero_iid), (zero_iid, alpha_iid)]:
        instance_dir = fake_root / "apps" / iid
        (instance_dir / "data").mkdir(parents=True)
        (instance_dir / "config").mkdir(parents=True)
        (instance_dir / "config" / "subscriptions.yaml").write_text(
            f"subscriptions:\n"
            f"  {chat}:\n"
            f"    platform: lark\n"
            f"    name: test\n"
            f"    peers:\n"
            f"    - uuid: {peer_iid}\n"
            f"      endpoint: http://127.0.0.1:8642/internal/message-broadcast\n",
            encoding="utf-8",
        )

    # 让项目根 = fake_root,这样 all instance discovery 都映射到这里
    import infrastructure.config as cfg
    import domain.messages as msgs

    monkeypatch.setattr(cfg, "get_project_root", lambda: fake_root)
    monkeypatch.setattr(cfg, "get_instance_dir",
                        lambda iid=None: fake_root / "apps" / (iid or ""))
    monkeypatch.setattr(cfg, "get_instance_data_dir",
                        lambda iid=None: (fake_root / "apps" / (iid or "") / "data"))
    # discover_active_instances 扫描 apps/ 子目录
    monkeypatch.setattr(cfg, "discover_active_instances",
                        lambda: [alpha_iid, zero_iid])

    # 模拟 master 进程:不设 ContextVar / DIGITAL_LIFE_INSTANCE_ID
    for k in ("DIGITAL_LIFE_INSTANCE_ID", "L4_AGENT_ID", "DIGITAL_LIFE_EMPLOYEE_ID"):
        os.environ.pop(k, None)

    # --- 测试用例 1: zero -> alpha 广播 ---
    r = B.receive_broadcast({
        "from_instance_id": zero_iid,
        "from_display_name": "zero",
        "chat_id": chat,
        "text": "ZERO_TO_ALPHA_PROBE",
        "msg_ref": "test_route_1",
    })
    assert r["ok"] and r["delivered"] == 1, r

    # alpha 库应该有,zero 库应该没有(广播不能回写 from)
    a_msgs = sqlite3.connect(fake_root / "apps" / alpha_iid / "data" / "messages.db")
    z_msgs = sqlite3.connect(fake_root / "apps" / zero_iid / "data" / "messages.db")
    try:
        a_has = a_msgs.execute(
            "SELECT COUNT(*) FROM messages WHERE text='ZERO_TO_ALPHA_PROBE'"
        ).fetchone()[0]
        # zero 库的 messages 表可能压根没创建(广播根本没路由到它,自然不建表)
        # 所以这里容忍 "no such table" 当作 0 条
        try:
            z_has = z_msgs.execute(
                "SELECT COUNT(*) FROM messages WHERE text='ZERO_TO_ALPHA_PROBE'"
            ).fetchone()[0]
        except sqlite3.OperationalError:
            z_has = 0
        assert a_has == 1, f"alpha 应当收到广播,实际 {a_has}"
        assert z_has == 0, f"zero 不应回写自己,实际 {z_has}"
    finally:
        a_msgs.close(); z_msgs.close()

    # --- 测试用例 2: alpha -> zero 广播(历史 BUG 场景) ---
    r = B.receive_broadcast({
        "from_instance_id": alpha_iid,
        "from_display_name": "alpha",
        "chat_id": chat,
        "text": "ALPHA_TO_ZERO_PROBE",
        "msg_ref": "test_route_2",
    })
    assert r["ok"] and r["delivered"] == 1, r

    a_msgs = sqlite3.connect(fake_root / "apps" / alpha_iid / "data" / "messages.db")
    z_msgs = sqlite3.connect(fake_root / "apps" / zero_iid / "data" / "messages.db")
    try:
        a_has = a_msgs.execute(
            "SELECT COUNT(*) FROM messages WHERE text='ALPHA_TO_ZERO_PROBE'"
        ).fetchone()[0]
        z_has = z_msgs.execute(
            "SELECT COUNT(*) FROM messages WHERE text='ALPHA_TO_ZERO_PROBE'"
        ).fetchone()[0]
        assert a_has == 0, f"alpha 不应回写自己,实际 {a_has}"
        assert z_has == 1, f"zero 应当收到广播,实际 {z_has}"
    finally:
        a_msgs.close(); z_msgs.close()
