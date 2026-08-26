"""跨通道消息去重(发送侧根治)的核心行为锁定。

背景: 飞书群里有多个数字生命实例时, Alpha 发的消息会通过两条路径到达 Zero:
  1) 飞书 WS: 被 @ 的机器人, 飞书单独推一份给它
  2) 实例间 HTTP 广播: 我们自己 fan-out 给所有订阅兄弟
当 Alpha @ 了某个机器人, 飞书已送达它, 再广播就是重复。本方案在发送侧
根据 contacts.kind=='bot' 判定"@ 到了机器人", 跳过本次广播。

设计参见 docs/design/digital-life-system-design.md 及
docs/architecture/decentralized-message-bus.md。
"""
from __future__ import annotations

from types import SimpleNamespace


# ── 1. contacts.get_or_create_stub: kind 参数 + 升级 ──────────────────────


def _setup_contacts(tmp_path, monkeypatch):
    """让 domain.contacts.store 用 tmp_path/state.db。"""
    import domain.contacts.store as cs

    monkeypatch.setattr(cs, "_state_db_path", lambda: tmp_path / "state.db")
    cs.ensure_schema()
    return cs


def test_stub_new_with_bot_kind(tmp_path, monkeypatch):
    """新建联系人时 kind 直接生效。"""
    cs = _setup_contacts(tmp_path, monkeypatch)
    out = cs.get_or_create_stub("feishu", "ou_bot1", kind="bot")
    assert out is not None
    assert out["kind"] == "bot"

    from domain.contacts import lookup_kind
    assert lookup_kind("feishu", "ou_bot1") == "bot"


def test_stub_upgrade_human_to_bot(tmp_path, monkeypatch):
    """已有 human(stub)记录, 飞书 sender_type 揭示它是 bot → 升级 kind。"""
    cs = _setup_contacts(tmp_path, monkeypatch)
    # 先以 human 注册(模拟发消息时还没识别出是机器人)
    cs.get_or_create_stub("feishu", "ou_bot2", kind="human")
    assert cs.lookup_kind("feishu", "ou_bot2") == "human"

    # 后来飞书事件带 sender_type='app', 升级为 bot
    cs.get_or_create_stub("feishu", "ou_bot2", kind="bot")
    assert cs.lookup_kind("feishu", "ou_bot2") == "bot"


def test_stub_no_downgrade_bot_to_human(tmp_path, monkeypatch):
    """已识别为 bot, 后续默认调用不应该降回 human。"""
    cs = _setup_contacts(tmp_path, monkeypatch)
    cs.get_or_create_stub("feishu", "ou_bot3", kind="bot")
    cs.get_or_create_stub("feishu", "ou_bot3")  # 默认 human
    assert cs.lookup_kind("feishu", "ou_bot3") == "bot"


def test_any_id_is_bot(tmp_path, monkeypatch):
    cs = _setup_contacts(tmp_path, monkeypatch)
    cs.get_or_create_stub("feishu", "ou_human1", kind="human")
    cs.get_or_create_stub("feishu", "ou_bot4", kind="bot")
    assert cs.any_id_is_bot("feishu", ["ou_human1", "ou_bot4"]) is True
    assert cs.any_id_is_bot("feishu", ["ou_human1"]) is False
    assert cs.any_id_is_bot("feishu", []) is False


# ── 2. publish_chat_message: broadcast 开关 ──────────────────────────────


def test_publish_chat_message_skips_broadcast_when_flag_off(tmp_path, monkeypatch):
    """broadcast=False 时, 仍写本地 messages.db, 但不调 broadcast_outbound。"""
    import domain.messages as M
    monkeypatch.setattr(M, "_INSTANCE_DATA_PATH_OVERRIDE", tmp_path)
    M._ensure_schema()

    # 拦截 broadcast: 若被调用即失败
    called = {"n": 0}

    def _fake_broadcast(**kwargs):
        called["n"] += 1
        return 0

    import domain.messages.broadcast as B
    monkeypatch.setattr(B, "broadcast_outbound", _fake_broadcast)

    # 让 config 拿到稳定 instance id / display name
    import infrastructure.config as cfg
    monkeypatch.setattr(cfg, "get_app_instance_id", lambda: "uuid_self")
    monkeypatch.setattr(cfg, "get_instance_display_name", lambda: "self")

    from domain.conversations import publish_chat_message
    rid = publish_chat_message(
        chat_id="oc_g", sender_id="uuid_self", sender_name="self",
        text="hi @bot", msg_id="", sender_kind="bot", broadcast=False,
    )
    assert rid is not None
    assert called["n"] == 0, "broadcast=False 时不应触发 broadcast_outbound"

    # 出站记录仍写入
    m = M.list_messages("oc_g", limit=1)[0]
    assert m["direction"] == "out"
    assert m["text"] == "hi @bot"


def test_publish_chat_message_broadcasts_when_flag_on(tmp_path, monkeypatch):
    """broadcast=True(默认) 时正常触发广播。"""
    import domain.messages as M
    monkeypatch.setattr(M, "_INSTANCE_DATA_PATH_OVERRIDE", tmp_path)
    M._ensure_schema()

    called = {"n": 0}

    def _fake_broadcast(**kwargs):
        called["n"] += 1
        return 1

    import domain.messages.broadcast as B
    monkeypatch.setattr(B, "broadcast_outbound", _fake_broadcast)

    import infrastructure.config as cfg
    monkeypatch.setattr(cfg, "get_app_instance_id", lambda: "uuid_self")
    monkeypatch.setattr(cfg, "get_instance_display_name", lambda: "self")

    from domain.conversations import publish_chat_message
    publish_chat_message(
        chat_id="oc_g", sender_id="uuid_self", sender_name="self",
        text="普通群消息没人@", msg_id="", sender_kind="bot",
    )
    assert called["n"] == 1


# ── 3. NormalizedMessage: sender_is_bot 默认 + feishu 提取 ────────────────


def test_open_id_regex_matches_underscore_and_hyphen():
    """action_tools fan-out 用正则从 send_text 提取被 @ 的 open_id。

    飞书 open_id 字符集含下划线/连字符（如 ou_alpha_bot、ou_7f9a-1b3d）。
    旧正则 ou_[a-zA-Z0-9]+ 无法匹配这类 ID → 提取为空 → any_id_is_bot 永远 False
    → @ 到 bot 仍误广播。锁定修正后的字符集。
    """
    import re

    # 与 action_tools._handle_express_to_human fan-out 分支同款正则
    pat = r'<at user_id="(ou_[a-zA-Z0-9_-]+)"></at>'

    cases = {
        "ou_eb5083ebd79e1135d2af74fb72ce758f": "纯 hex（最常见）",
        "ou_alpha_bot": "含下划线",
        "ou_7f9a4c2e-1b3d-4a5f": "含连字符",
    }
    for ou, desc in cases.items():
        text = f'<at user_id="{ou}"></at> 内容'
        found = re.findall(pat, text)
        assert found == [ou], f"[{desc}] 正则应提取出 {ou}, 实得 {found}"


def test_open_id_regex_empty_when_no_mention():
    """没有 <at> 标签时正则返回空，确保 fan-out 不误判普通消息。"""
    import re
    pat = r'<at user_id="(ou_[a-zA-Z0-9_-]+)"></at>'
    assert re.findall(pat, "今天天气不错") == []
    assert re.findall(pat, "@alpha 但不是飞书 at 标签") == []


def test_normalized_message_sender_is_bot_default_false():
    from interfaces.ingress.base import NormalizedMessage
    m = NormalizedMessage(
        platform="feishu", chat_id="oc_1", message_id="om_1",
        sender_id="ou_x", content="hi",
    )
    assert m.sender_is_bot is False


def test_feishu_normalize_detects_app_sender():
    """飞书 event.sender.sender_type == 'app' → NormalizedMessage.sender_is_bot True。"""
    from interfaces.ingress.feishu import FeishuAdapter

    # 构造 SDK 事件形状: event.event.message / event.event.sender
    sender_id = SimpleNamespace(open_id="ou_bot_app", union_id="", user_id="", app_id="")
    sender = SimpleNamespace(sender_id=sender_id, sender_type="app")
    message = SimpleNamespace(
        chat_id="oc_g", chat_type="group", message_id="om_1",
        content='{"text":"hi"}', mentions=[],
    )
    event = SimpleNamespace(event=SimpleNamespace(message=message, sender=sender))

    # 绕过 __init__(需凭证); 补齐 _normalize 依赖的实例属性, 并短路群名 HTTP 拉取。
    adapter = object.__new__(FeishuAdapter)
    adapter._app_id = "cli_test"
    adapter._app_secret = ""
    adapter._bot_name = ""
    adapter._chat_name_cache = {}
    adapter._user_name_cache = {}
    adapter._fetch_chat_name = lambda _cid: ""
    adapter._fetch_user_name = lambda _oid: ""
    nmsg = adapter._normalize(event)
    assert nmsg.sender_is_bot is True
    assert nmsg.sender_id == "ou_bot_app"


def test_feishu_normalize_human_sender_not_bot():
    sender_id = SimpleNamespace(open_id="ou_human", union_id="", user_id="", app_id="")
    sender = SimpleNamespace(sender_id=sender_id, sender_type="user")
    message = SimpleNamespace(
        chat_id="oc_g", chat_type="group", message_id="om_2",
        content='{"text":"hi"}', mentions=[],
    )
    event = SimpleNamespace(event=SimpleNamespace(message=message, sender=sender))

    from interfaces.ingress.feishu import FeishuAdapter
    adapter = object.__new__(FeishuAdapter)
    adapter._app_id = "cli_test"
    adapter._app_secret = ""
    adapter._bot_name = ""
    adapter._chat_name_cache = {}
    adapter._user_name_cache = {}
    adapter._fetch_chat_name = lambda _cid: ""
    adapter._fetch_user_name = lambda _oid: ""
    nmsg = adapter._normalize(event)
    assert nmsg.sender_is_bot is False
