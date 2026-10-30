"""后台子代理测试 — spawn / 汇报双对端 / 追问复活 / 并发上限 / key 继承指针。

领域层直测（不经工具 dispatch）：delegate + registry + profile 解析。
LLM 调用一律不涉及（spawn/汇报都是纯库操作 + 事件）。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from infrastructure.persistence import services_registry

OWNER_IID = "11111111-1111-1111-1111-111111111111"
OWNER2_IID = "22222222-2222-2222-2222-222222222222"


def _restore_default_runtime_hooks() -> None:
    from domain.lifecycle.affairs import runtime as affair_runtime

    affair_runtime._db_path_hook = lambda: Path(
        affair_runtime._resolve_default_state_db()
    )
    affair_runtime._now_iso_hook = affair_runtime._default_now_iso
    affair_runtime._now_dt_hook = affair_runtime._default_now_dt
    affair_runtime._parse_iso_hook = affair_runtime._default_parse_iso


@pytest.fixture()
def delegate_env(tmp_path, monkeypatch):
    """隔离环境：全局库重定向 + 项目根指向 tmp + 缓存清零 + 钩子还原。"""
    monkeypatch.setenv("DIGITAL_LIFE_SERVICES_DB", str(tmp_path / "data" / "services.db"))
    monkeypatch.setenv("DIGITAL_LIFE_PROJECTS_DB", str(tmp_path / "data" / "projects.db"))
    monkeypatch.setenv(
        "DIGITAL_LIFE_TOOL_VISIBILITY_DB", str(tmp_path / "data" / "tool_visibility.db")
    )
    monkeypatch.setattr("infrastructure.config.get_project_root", lambda: tmp_path)
    _restore_default_runtime_hooks()
    services_registry.reset_for_test()
    yield tmp_path
    os.environ.pop("DIGITAL_LIFE_INSTANCE_ID", None)
    services_registry.reset_for_test()
    _restore_default_runtime_hooks()


def _make_subagent_def(root: Path) -> Path:
    def_dir = root / "apps" / "subagent-def"
    (def_dir / "config").mkdir(parents=True, exist_ok=True)
    (def_dir / "config" / "app.yaml").write_text(
        "runtime_kind: definition\ndisplay_name: 通用子代理\nactive: false\n",
        encoding="utf-8",
    )
    return def_dir


def _make_instance(root: Path, iid: str = OWNER_IID, *, with_model: bool = False) -> Path:
    cfg = root / "apps" / iid / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    model = (
        "model:\n  name: glm-test\n  base_url: https://gw.test/v1\n"
        if with_model
        else ""
    )
    (cfg / "app.yaml").write_text(f"display_name: 测试实例\n{model}", encoding="utf-8")
    if with_model:
        (cfg / "secrets.env").write_text("LLM_API_KEY=sk-owner-key\n", encoding="utf-8")
    return cfg.parent


def _as(iid: str):
    """身份切换上下文管理器（模拟对应运行体进程内执行）。"""
    from contextlib import contextmanager
    from infrastructure.config import (
        reset_current_instance_id,
        set_current_instance_id,
    )

    @contextmanager
    def _ctx():
        token = set_current_instance_id(iid)
        try:
            yield
        finally:
            reset_current_instance_id(token)

    return _ctx()


# ── spawn ─────────────────────────────────────────────────────────────


def test_spawn_creates_service_task_event_and_ttl(delegate_env):
    root = delegate_env
    _make_subagent_def(root)
    _make_instance(root)

    with _as(OWNER_IID):
        from domain.service import delegate

        r = delegate.spawn_subagent("统计 tests 目录的代码行数并汇报", ttl_hours=2)
    assert r["ok"], r
    sid = r["contact_id"]
    assert sid.startswith("svc-")
    assert r["expires_at"], r

    # 注册表行：委托关系 + def 绑定
    row = services_registry.lookup_service(sid)
    assert row["agent_def_id"] == "subagent-def"
    assert row["owner_ref"] == f"instance:{OWNER_IID}"
    assert row["status"] == "active"

    # key 继承指针（自有配置，不下沉密钥）
    own_cfg = root / "apps" / "subagent-def" / "services" / sid / "config" / "app.yaml"
    assert own_cfg.exists()
    assert f"agent:{OWNER_IID}" in own_cfg.read_text(encoding="utf-8")

    # 首条任务 message 事件落在子代理自己的 state.db
    db = root / "apps" / "subagent-def" / "services" / sid / "data" / "state.db"
    conn = sqlite3.connect(str(db))
    ev = conn.execute(
        "SELECT kind, channel, payload FROM events WHERE kind='message'"
    ).fetchone()
    timer = conn.execute(
        "SELECT payload_json FROM timers WHERE event_kind='timer'"
    ).fetchone()
    conn.close()
    assert ev is not None
    assert ev[1].startswith(f"instance:{sid}")
    assert "统计 tests 目录" in ev[2]
    assert timer is not None and "subagent_ttl" in timer[0]

    # 委托人联系人簿登记（sense_contacts 可见）
    from domain.contacts.store import lookup_chat, lookup_name

    with _as(OWNER_IID):
        assert lookup_chat(f"subagent:{sid}") is not None
        assert lookup_name("subagent", sid) == r["display_name"]


def test_spawn_requires_definition(delegate_env):
    with _as(OWNER_IID):
        from domain.service import delegate

        r = delegate.spawn_subagent("任务")
    assert not r["ok"]
    assert "定义" in r["error"]


def test_spawn_concurrency_limit(delegate_env):
    root = delegate_env
    _make_subagent_def(root)
    with _as(OWNER_IID):
        from domain.service import delegate

        for i in range(delegate.DEFAULT_MAX_ACTIVE):
            assert delegate.spawn_subagent(f"任务{i}：{'x' * 20}")["ok"]
        full = delegate.spawn_subagent("第四个任务超出上限")
    assert not full["ok"]
    assert "上限" in full["error"]

    # 别的委托人不受影响
    _make_instance(root, OWNER2_IID)
    with _as(OWNER2_IID):
        from domain.service import delegate

        assert delegate.spawn_subagent("另一委托人的任务")["ok"]


def test_spawn_rejects_bad_ttl(delegate_env):
    _make_subagent_def(delegate_env)
    with _as(OWNER_IID):
        from domain.service import delegate

        assert not delegate.spawn_subagent("t", ttl_hours=0)["ok"]
        assert not delegate.spawn_subagent("t", ttl_hours=10_000)["ok"]


# ── 汇报：两对端路由 ──────────────────────────────────────────────────


def test_report_to_instance_owner_lands_and_archives(delegate_env, monkeypatch):
    root = delegate_env
    _make_subagent_def(root)
    _make_instance(root)

    with _as(OWNER_IID):
        from domain.service import delegate

        sid = delegate.spawn_subagent("做个调研")["contact_id"]

    def _no_wake(event_id):  # 子代理 worker 是 ephemeral 进程，绝不 in-process 叫醒实例
        raise AssertionError("instance wake must not run inside subagent worker")

    monkeypatch.setattr("domain.lifecycle.events._wake_or_inject", _no_wake)

    with _as(sid):  # 模拟子代理 worker 进程
        from domain.service import delegate

        r = delegate.report_to_delegator("调研完成：结论是……", done=True)
    assert r["ok"] and r["archived"] and r["delegator"] == f"instance:{OWNER_IID}"

    # 委托人收到：messages.db 入站 + message 事件（窗口 subagent:{sid}）
    msg_db = root / "apps" / OWNER_IID / "data" / "messages.db"
    conn = sqlite3.connect(str(msg_db))
    inbound = conn.execute(
        "SELECT direction, chat_id, text FROM messages ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert inbound and inbound[0] == "in" and inbound[1] == f"subagent:{sid}"
    assert "调研完成" in inbound[2]

    ev_db = root / "apps" / OWNER_IID / "data" / "state.db"
    conn = sqlite3.connect(str(ev_db))
    ev = conn.execute(
        "SELECT payload FROM events WHERE kind='message' ORDER BY event_id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert ev and "调研完成" in ev[0]

    # done=True → 归档封存 + TTL 闹钟清掉
    assert services_registry.lookup_service(sid)["status"] == "archived"
    data_dir = root / "apps" / "subagent-def" / "services" / sid / "data"
    conn = sqlite3.connect(str(data_dir / "state.db"))
    pending_timers = conn.execute(
        "SELECT COUNT(*) FROM timers WHERE event_kind='timer' AND fired_at IS NULL"
    ).fetchone()[0]
    conn.close()
    conn = sqlite3.connect(str(data_dir / "messages.db"))
    out = conn.execute(
        "SELECT direction, chat_id FROM messages ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert pending_timers == 0
    assert out and out[0] == "out" and out[1] == f"peer:{OWNER_IID}"


def test_report_to_service_owner_uses_peer_route(delegate_env):
    root = delegate_env
    # 建一个服务型委托人（echo-def）
    def_dir = root / "apps" / "echo-def"
    (def_dir / "config").mkdir(parents=True, exist_ok=True)
    (def_dir / "config" / "app.yaml").write_text(
        "runtime_kind: definition\ndisplay_name: echo\n", encoding="utf-8"
    )
    _make_subagent_def(root)
    from domain.service import create_service

    with _as(OWNER_IID):
        pm_sid = create_service("echo-def", display_name="PM")["service_id"]

    with _as(pm_sid):
        from domain.service import delegate

        r = delegate.spawn_subagent("整理事实底稿")
        assert r["ok"], r
        sid = r["contact_id"]
        row = services_registry.lookup_service(sid)
        assert row["owner_ref"] == f"service:{pm_sid}"

    with _as(sid):
        from domain.service import delegate

        r2 = delegate.report_to_delegator("底稿好了，见 shared/", done=True)
    assert r2["ok"], r2

    # 服务型委托人：入库 + 事件（同构同伴私聊）
    pm_db = root / "apps" / "echo-def" / "services" / pm_sid / "data"
    conn = sqlite3.connect(str(pm_db / "messages.db"))
    inbound = conn.execute(
        "SELECT direction, source, chat_id FROM messages ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert inbound and inbound[0] == "in" and inbound[1] == "subagent"
    assert inbound[2] == f"peer:{sid}"

    conn = sqlite3.connect(str(pm_db / "state.db"))
    ev = conn.execute(
        "SELECT payload FROM events WHERE kind='message' ORDER BY event_id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert ev and "底稿好了" in ev[0]
    assert services_registry.lookup_service(sid)["status"] == "archived"


def test_report_requires_delegation(delegate_env):
    root = delegate_env
    def_dir = root / "apps" / "echo-def"
    (def_dir / "config").mkdir(parents=True, exist_ok=True)
    (def_dir / "config" / "app.yaml").write_text(
        "runtime_kind: definition\ndisplay_name: echo\n", encoding="utf-8"
    )
    _make_subagent_def(root)
    from domain.service import create_service

    plain_sid = create_service("echo-def")["service_id"]

    with _as(plain_sid):
        from domain.service import delegate

        assert not delegate.report_to_delegator("x")["ok"]


# ── 追问：owner 校验 + 复活 ───────────────────────────────────────────


def test_message_subagent_owner_gate_and_revive(delegate_env):
    root = delegate_env
    _make_subagent_def(root)
    _make_instance(root)
    _make_instance(root, OWNER2_IID)

    with _as(OWNER_IID):
        from domain.service import delegate

        sid = delegate.spawn_subagent("长任务")["contact_id"]

    # 别人的子代理不能指挥
    with _as(OWNER2_IID):
        from domain.service import delegate

        r = delegate.message_subagent(sid, "改成别的")
    assert not r["ok"] and "自己的子代理" in r["error"]

    # 不是子代理不能 message
    with _as(OWNER_IID):
        from domain.service import delegate

        assert not delegate.message_subagent("svc-notexist", "hi")["ok"]

    # 归档后追问 → 自动复活 + 事件送达
    services_registry.set_service_status(sid, services_registry.SERVICE_STATUS_ARCHIVED)
    with _as(OWNER_IID):
        from domain.service import delegate

        r2 = delegate.message_subagent(sid, "补充一点：只要前半部分")
    assert r2["ok"] and r2["revived"] is True
    assert services_registry.lookup_service(sid)["status"] == "active"

    sub_db = root / "apps" / "subagent-def" / "services" / sid / "data" / "state.db"
    conn = sqlite3.connect(str(sub_db))
    ev = conn.execute(
        "SELECT payload FROM events WHERE kind='message' ORDER BY event_id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert ev and "前半部分" in ev[0]


# ── key 继承指针解析 ──────────────────────────────────────────────────


def test_agent_profile_resolution_inherits_owner_key(delegate_env):
    root = delegate_env
    _make_subagent_def(root)
    _make_instance(root, with_model=True)

    with _as(OWNER_IID):
        from domain.service import delegate

        sid = delegate.spawn_subagent("继承 key 的任务")["contact_id"]

    from infrastructure.ai.config import load_runtime_config, resolve_agent_model_profile

    entry = resolve_agent_model_profile(sid)
    assert entry is not None
    assert entry.get("api_key") == "sk-owner-key"
    assert entry.get("name") == "glm-test"
    assert entry.get("base_url") == "https://gw.test/v1"

    # 端到端：以子代理身份解析运行时配置，key 来自委托人
    with _as(sid):
        cfg = load_runtime_config()
    assert cfg["model"]["api_key"] == "sk-owner-key"
    assert cfg["model"]["name"] == "glm-test"

    # 环防护：深度超限返回 None
    from infrastructure.ai import config as ai_cfg

    assert ai_cfg.resolve_agent_model_profile(sid, _depth=9) is None
    assert ai_cfg.resolve_agent_model_profile("") is None
