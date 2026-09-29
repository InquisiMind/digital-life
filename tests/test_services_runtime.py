"""服务型改造测试 — registry / lease / 路径 / messaging / worker / 调度循环。

刀 1 验收的自动化版：echo 定义 → 创建服务 → emit 落服务库（不叫醒）→
worker 隔离跑完 → 归档后再发被拒。LLM 调用一律 mock（wake_digital_life）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import types
from pathlib import Path

import pytest

from infrastructure.persistence import services_registry


def _restore_default_runtime_hooks() -> None:
    """恢复 affairs runtime 默认钩子。

    既有 e2e 测试（test_e2e_event_lifecycle 等）用 configure_runtime_hooks
    全局改写 _db_path_hook 且不还原（该函数只进不出），本文件之后的测试
    会把事件写进它们遗留的 tmp 库。进门前先恢复"走路径中枢"的默认钩子。
    """
    from domain.lifecycle.affairs import runtime as affair_runtime

    affair_runtime._db_path_hook = lambda: Path(
        affair_runtime._resolve_default_state_db()
    )
    affair_runtime._now_iso_hook = affair_runtime._default_now_iso
    affair_runtime._now_dt_hook = affair_runtime._default_now_dt
    affair_runtime._parse_iso_hook = affair_runtime._default_parse_iso


@pytest.fixture()
def service_env(tmp_path, monkeypatch):
    """隔离环境：services.db 重定向 + 项目根指向 tmp + 缓存清零 + 钩子还原。"""
    db_path = tmp_path / "data" / "services.db"
    monkeypatch.setenv("DIGITAL_LIFE_SERVICES_DB", str(db_path))
    monkeypatch.setattr("infrastructure.config.get_project_root", lambda: tmp_path)
    _restore_default_runtime_hooks()
    services_registry.reset_for_test()
    yield tmp_path
    # worker 会写 DIGITAL_LIFE_INSTANCE_ID，清掉避免泄漏给后续测试
    os.environ.pop("DIGITAL_LIFE_INSTANCE_ID", None)
    services_registry.reset_for_test()
    _restore_default_runtime_hooks()


def _make_def(root: Path, def_id: str = "echo-def") -> Path:
    def_dir = root / "apps" / def_id
    (def_dir / "config").mkdir(parents=True, exist_ok=True)
    (def_dir / "persona").mkdir(parents=True, exist_ok=True)
    (def_dir / "config" / "app.yaml").write_text(
        "runtime_kind: definition\ndisplay_name: echo-def\nactive: false\n",
        encoding="utf-8",
    )
    (def_dir / "persona" / "LIFE_PERSONA.md").write_text("# echo\n", encoding="utf-8")
    return def_dir


# ── registry / lease ─────────────────────────────────────────────────


def test_service_crud_and_status(service_env):
    from domain.service import archive_service, create_service

    _make_def(service_env)
    svc = create_service("echo-def", display_name="回声")
    assert svc["service_id"].startswith("svc-")
    assert svc["status"] == "active"
    assert svc["subscriptions"] == ["message"]

    assert services_registry.lookup_service(svc["service_id"]) is not None
    assert archive_service(svc["service_id"]) is True
    assert services_registry.lookup_service(svc["service_id"])["status"] == "archived"
    # 归档服务仍在注册表（路径解析/历史可查），只是不被调度
    assert services_registry.resolve_service_def(svc["service_id"]) == "echo-def"


def test_create_service_requires_definition_marker(service_env):
    from domain.service import create_service

    root = service_env
    plain = root / "apps" / "not-a-def"
    (plain / "config").mkdir(parents=True)
    (plain / "config" / "app.yaml").write_text("display_name: x\n", encoding="utf-8")
    with pytest.raises(ValueError, match="runtime_kind"):
        create_service("not-a-def")
    with pytest.raises(ValueError, match="定义不存在"):
        create_service("missing-def")


def test_lease_serial_gate(service_env):
    from infrastructure.persistence import services_registry as sr

    assert sr.try_acquire_lease("svc-a", "master:1", stale_seconds=3600) is True
    # 他人持有未超时 → 抢不到
    assert sr.try_acquire_lease("svc-a", "worker:2", stale_seconds=3600) is False
    # 同 holder → 续租成功
    assert sr.try_acquire_lease("svc-a", "master:1", stale_seconds=3600) is True
    # 超时 → 可抢占
    assert sr.try_acquire_lease("svc-a", "worker:3", stale_seconds=0) is True
    assert sr.release_lease("svc-a", "worker:3") is True
    assert sr.get_lease("svc-a") is None
    # 只能释放自己的租
    sr.try_acquire_lease("svc-a", "master:1", stale_seconds=3600)
    assert sr.release_lease("svc-a", "worker:9") is False


# ── 路径中枢 ─────────────────────────────────────────────────────────


def test_path_resolution_instance_unchanged(service_env):
    from infrastructure.config import (
        get_instance_dir,
        get_instance_state_db_path,
        is_instance_active,
    )

    root = service_env
    inst = root / "apps" / "11111111-1111-1111-1111-111111111111"
    (inst / "config").mkdir(parents=True)
    (inst / "config" / "app.yaml").write_text("display_name: legacy\n", encoding="utf-8")
    # 无服务注册 → 实例路径行为与改造前完全一致
    assert get_instance_dir("11111111-1111-1111-1111-111111111111") == inst
    assert (
        get_instance_state_db_path("11111111-1111-1111-1111-111111111111")
        == inst / "data" / "state.db"
    )
    assert is_instance_active("11111111-1111-1111-1111-111111111111") is True


def test_path_resolution_service_and_def_assets(service_env):
    from infrastructure.config import (
        get_instance_config_path,
        get_instance_dir,
        get_instance_env_path,
        get_instance_persona_path,
        get_instance_state_db_path,
        get_workspace_dir,
        is_instance_active,
        is_registered_instance,
        resolve_runtime_dir,
    )

    root = service_env
    _make_def(root)
    from domain.service import create_service

    svc = create_service("echo-def")
    sid = svc["service_id"]

    expected_dir = root / "apps" / "echo-def" / "services" / sid
    assert resolve_runtime_dir(sid) == expected_dir
    assert get_instance_dir(sid) == expected_dir
    assert get_instance_state_db_path(sid) == expected_dir / "data" / "state.db"
    # 定义层资产二跳：persona/配置/密钥共享自 def，不落服务目录
    assert get_instance_persona_path(sid) == root / "apps" / "echo-def" / "persona" / "LIFE_PERSONA.md"
    assert get_instance_config_path(sid) == root / "apps" / "echo-def" / "config" / "app.yaml"
    assert get_instance_env_path(sid) == root / "apps" / "echo-def" / "config" / "secrets.env"
    # 运行时数据按服务隔离：workspace 固定在服务目录内
    assert get_workspace_dir(sid) == expected_dir / "workspace"
    assert (expected_dir / "workspace").is_dir()
    # 注册与发现语义
    assert is_registered_instance(sid) is True
    assert is_instance_active(sid) is True  # 服务默认 active（services 表 status）
    # 定义目录不被当实例
    assert is_instance_active("echo-def") is False

    from infrastructure.config import _load_registry

    assert "echo-def" not in _load_registry()
    assert sid not in _load_registry()


# ── messaging：emit 落服务库 + 闸 ────────────────────────────────────


def test_emit_to_service_lands_in_service_db(service_env, monkeypatch):
    from domain.service import create_service, emit_to_service

    root = service_env
    _make_def(root)
    svc = create_service("echo-def")
    sid = svc["service_id"]

    def _no_wake(event_id):  # 服务事件绝不允许触发 master 进程内叫醒
        raise AssertionError("_wake_or_inject must be suppressed for services")

    monkeypatch.setattr("domain.lifecycle.events._wake_or_inject", _no_wake)

    event_id = emit_to_service(sid, "message", {"text": "你好", "chat_id": "c1"})
    assert event_id > 0

    db = root / "apps" / "echo-def" / "services" / sid / "data" / "state.db"
    assert db.exists()
    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT kind, channel, payload FROM events WHERE event_id = ?", (event_id,)
    ).fetchone()
    conn.close()
    assert row is not None
    kind, channel, payload = row
    assert kind == "message"
    assert channel.startswith(f"instance:{sid}")
    assert json.loads(payload)["text"] == "你好"


def test_emit_gate_archived_and_unsubscribed(service_env):
    from domain.service import archive_service, create_service, emit_to_service

    _make_def(service_env)
    svc = create_service("echo-def")
    sid = svc["service_id"]

    # 未订阅种类被拒（默认订阅只有 message）
    assert emit_to_service(sid, "alarm_due", {"x": 1}) == 0
    # 消息类永远放行
    assert emit_to_service(sid, "message", {"text": "hi"}) > 0
    # 归档后连消息也拒（服务开关）
    archive_service(sid)
    assert emit_to_service(sid, "message", {"text": "hi"}) == 0
    # 未注册服务拒
    assert emit_to_service("svc-notexist", "message", {"text": "hi"}) == 0


# ── worker：隔离跑完 + 归档不跑 ──────────────────────────────────────


def test_worker_processes_due_events_isolated(service_env, monkeypatch):
    from domain.service import create_service, emit_to_service

    root = service_env
    _make_def(root)
    svc = create_service("echo-def")
    sid = svc["service_id"]
    emit_to_service(sid, "message", {"text": "第一条"})

    wake_calls: list[dict] = []

    def _fake_wake(affair_id, reason="", extra="", pending_events=None, **kw):
        wake_calls.append({"affair_id": affair_id, "reason": reason})
        return {"woke": True}

    monkeypatch.setattr("domain.lifecycle.scheduler.wake_digital_life", _fake_wake)

    from gateway.service_worker import run_service_worker

    code = run_service_worker(sid)
    assert code == 0
    assert len(wake_calls) == 1
    assert wake_calls[0]["reason"] == "message"
    # affair 落在服务库（隔离验证）
    db = root / "apps" / "echo-def" / "services" / sid / "data" / "state.db"
    conn = sqlite3.connect(str(db))
    affairs = conn.execute("SELECT COUNT(*) FROM affairs").fetchone()[0]
    conn.close()
    assert affairs >= 1
    # worker 结束释放 lease
    assert services_registry.get_lease(sid) is None


def test_worker_skips_archived_and_empty(service_env, monkeypatch):
    from domain.service import archive_service, create_service

    _make_def(service_env)
    svc = create_service("echo-def")
    sid = svc["service_id"]
    archive_service(sid)

    def _fail(*a, **kw):
        raise AssertionError("archived service must not wake")

    monkeypatch.setattr("domain.lifecycle.scheduler.wake_digital_life", _fail)
    from gateway.service_worker import run_service_worker

    assert run_service_worker(sid) == 0  # 归档：直接退出，不 wake
    assert run_service_worker("svc-notexist") == 1  # 未注册：报错退出


def test_worker_respects_lease_of_other_holder(service_env, monkeypatch):
    from domain.service import create_service, emit_to_service
    from infrastructure.persistence import services_registry

    _make_def(service_env)
    svc = create_service("echo-def")
    sid = svc["service_id"]
    emit_to_service(sid, "message", {"text": "hi"})

    assert services_registry.try_acquire_lease(sid, "master:999", stale_seconds=3600)

    def _fail(*a, **kw):
        raise AssertionError("must not wake while another holder owns the lease")

    monkeypatch.setattr("domain.lifecycle.scheduler.wake_digital_life", _fail)
    from gateway.service_worker import run_service_worker

    assert run_service_worker(sid) == 0
    assert services_registry.get_lease(sid)["holder"] == "master:999"


# ── 调度循环 ─────────────────────────────────────────────────────────


class _FakeProc:
    def __init__(self, pid: int = 4242):
        self.pid = pid
        self._code: int | None = None

    def poll(self):
        return self._code

    def terminate(self):
        self._code = 0


def test_loop_spawns_on_due_and_releases_on_exit(service_env):
    from domain.service import archive_service, create_service, emit_to_service
    from infrastructure.scheduler.service_runner import ServiceWorkerLoop

    root = service_env
    _make_def(root)
    svc = create_service("echo-def")
    sid = svc["service_id"]
    other = create_service("echo-def")

    spawned: list[str] = []

    def _fake_spawn(service_id: str) -> _FakeProc:
        spawned.append(service_id)
        return _FakeProc()

    loop = ServiceWorkerLoop(stale_seconds=3600, spawn=_fake_spawn)

    # 无到期事件 → 不 spawn
    assert loop.tick() == []
    assert spawned == []

    # 到期 → spawn + 持租
    emit_to_service(sid, "message", {"text": "触发"})
    assert loop.tick() == [sid]
    assert spawned == [sid]
    lease = services_registry.get_lease(sid)
    assert lease is not None and lease["holder"].startswith("master:")

    # worker 还在跑（poll()=None）→ 不重复 spawn
    assert loop.tick() == []

    # 归档的服务即使有到期事件也不 spawn
    emit_to_service(other["service_id"], "message", {"text": "x"})
    archive_service(other["service_id"])
    # worker 退出（含失败退出）→ reap 释放租约；事件未消费 → 重拉（崩溃重试语义）
    loop._procs[sid]._code = 1
    assert loop.tick() == [sid]
    assert spawned == [sid, sid]
    assert services_registry.get_lease(sid) is not None
    # worker 正常结束且事件已消费 → 彻底停拉
    loop._procs[sid]._code = 0
    db = root / "apps" / "echo-def" / "services" / sid / "data" / "state.db"
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE events SET consumed_at = datetime('now')")
    conn.commit()
    conn.close()
    assert loop.tick() == []
    assert services_registry.get_lease(sid) is None


def test_loop_spawn_failure_releases_lease(service_env):
    from domain.service import create_service, emit_to_service
    from infrastructure.scheduler.service_runner import ServiceWorkerLoop

    _make_def(service_env)
    svc = create_service("echo-def")
    sid = svc["service_id"]
    emit_to_service(sid, "message", {"text": "x"})

    def _boom(_sid):
        raise RuntimeError("no fork")

    loop = ServiceWorkerLoop(stale_seconds=3600, spawn=_boom)
    assert loop.tick() == []
    # spawn 失败必须放回租约，下一轮还能重试
    assert services_registry.get_lease(sid) is None


# ── 刀 2：capability 闸 + 服务闹钟 ────────────────────────────────────


def test_capability_resolution(service_env):
    from domain.service import capability_enabled, create_service
    from infrastructure.persistence import services_registry as sr

    _make_def(service_env)
    # 实例型：全部能力恒开
    assert capability_enabled("vitals", "11111111-1111-1111-1111-111111111111") is True
    assert capability_enabled("routines", "11111111-1111-1111-1111-111111111111") is True

    svc = create_service("echo-def")
    sid = svc["service_id"]
    # 服务默认关
    assert capability_enabled("vitals", sid) is False
    assert capability_enabled("routines", sid) is False
    # 显式打开（将来"拟人化节奏"只改配置）
    sr.update_service_fields(sid, capabilities={"vitals": True})
    assert capability_enabled("vitals", sid) is True
    assert capability_enabled("routines", sid) is False  # 未声明仍默认关


def test_vitals_disabled_no_write(service_env):
    from domain.service import create_service
    from domain.vital.state import consume_energy, get_current_vitals, touch_activity

    root = service_env
    _make_def(root)
    sid = create_service("echo-def")["service_id"]

    from infrastructure.config import set_current_instance_id, reset_current_instance_id

    token = set_current_instance_id(sid)
    try:
        snap = consume_energy(500.0, reason="llm_call")
        assert snap.energy == 70.0  # 恒定默认快照
        get_current_vitals(persist=True)  # tick 路径也不落盘
        touch_activity()
    finally:
        reset_current_instance_id(token)

    db = root / "apps" / "echo-def" / "services" / sid / "data" / "state.db"
    conn = sqlite3.connect(str(db))
    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    # 表结构随 state.db 标准建表（affairs init_db）存在是正常的——闸的语义是
    # "不生效而非没有"：零数据行（真实验收：刀2前三号位 vitals 1 行+nurture_log
    # 27 行，刀2后全部为 0）
    if "vitals" in tables:
        assert conn.execute("SELECT COUNT(*) FROM vitals").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM nurture_log").fetchone()[0] == 0
    conn.close()


def test_gated_tools_hidden_for_service(service_env):
    from domain.service import create_service, gated_tool_names

    _make_def(service_env)
    sid = create_service("echo-def")["service_id"]

    blocked = gated_tool_names(sid)
    assert {"sense_vitals", "sense_nurture_log", "sense_schedule"} <= blocked
    # 内部项目/全局待办全家（D2：实例型共享面，服务默认不见）
    assert {
        "todo", "todo_plan", "todo_note", "todo_trigger", "sense_todos",
        "sense_projects", "project_todo", "project_deliver", "project_bootstrap",
    } <= blocked
    # 实例型：恒空集（零行为变化）
    assert gated_tool_names("11111111-1111-1111-1111-111111111111") == set()


def test_memory_index_skips_internal_projects_for_service(service_env):
    from domain.service import create_service

    _make_def(service_env)
    sid = create_service("echo-def")["service_id"]

    from infrastructure.config import set_current_instance_id, reset_current_instance_id

    token = set_current_instance_id(sid)
    try:
        from domain.memory.memory.recall.unified.normalizers import (
            index_projects_and_todos,
        )

        assert index_projects_and_todos() == 0  # 闸关：不读不写
    finally:
        reset_current_instance_id(token)


# ── 刀 4：项目层（CustomerProject + 共享工作区 + 协作） ───────────────


@pytest.fixture()
def project_template(service_env):
    """写一个两角色测试模版（复用 echo-def 定义）。"""
    tpl_dir = service_env / "config" / "project_templates"
    tpl_dir.mkdir(parents=True, exist_ok=True)
    (tpl_dir / "duo.yaml").write_text(
        "name: 双人测试\nroles:\n  - def: echo-def\n    name: 甲\n  - def: echo-def\n    name: 乙\n",
        encoding="utf-8",
    )
    return "duo"


def test_create_customer_project(service_env, project_template):
    from domain.project.customer import (
        create_customer_project,
        get_project_of_service,
        project_workspace_dir,
    )
    from infrastructure.persistence import services_registry as sr

    _make_def(service_env)
    result = create_customer_project("验收项目", project_template)
    pid = result["project"]["project_id"]
    assert pid.startswith("prj-")
    assert len(result["services"]) == 2
    # 工作区已建
    ws = project_workspace_dir(pid)
    assert ws is not None and ws.is_dir()
    assert ws == service_env / "projects" / pid / "workspace"
    # 服务挂项目、角色名正确
    a, b = result["services"]
    assert a["project_id"] == pid and b["project_id"] == pid
    assert a["display_name"] == "甲" and b["display_name"] == "乙"
    assert get_project_of_service(a["service_id"])["project_id"] == pid
    assert len(sr.list_services_by_project(pid, status="active")) == 2


def test_project_workspace_shared_and_anchored(service_env, project_template):
    from domain.project.customer import (
        WorkspaceEscapeError,
        create_customer_project,
        resolve_workspace_path,
    )
    from infrastructure.config import get_workspace_dir

    _make_def(service_env)
    result = create_customer_project("共享验收", project_template)
    a, b = result["services"]
    pid = result["project"]["project_id"]

    # 挂项目的服务：workspace 指向项目共享区（两服务同一目录）
    ws_a = get_workspace_dir(a["service_id"])
    ws_b = get_workspace_dir(b["service_id"])
    assert ws_a == ws_b == service_env / "projects" / pid / "workspace"

    # 锚定解析：区内放行
    p = resolve_workspace_path(a["service_id"], "客户资料/背景.md")
    assert p == ws_a / "客户资料" / "背景.md"
    # 越界拒绝（../ 逃逸）
    with pytest.raises(WorkspaceEscapeError):
        resolve_workspace_path(a["service_id"], "../越界.txt")
    with pytest.raises(WorkspaceEscapeError):
        resolve_workspace_path(a["service_id"], "客户资料/../../越界.txt")
    # symlink 逃逸拒绝：工作区内 symlink 指向区外
    outside = service_env / "outside-secret.txt"
    outside.write_text("秘密", encoding="utf-8")
    link = ws_a / "逃逸链接"
    link.symlink_to(outside)
    with pytest.raises(WorkspaceEscapeError):
        resolve_workspace_path(a["service_id"], "逃逸链接")
    # 未挂项目的服务：锚定到私有 workspace（同样拦越界）
    from domain.service import create_service

    solo = create_service("echo-def")["service_id"]
    assert get_workspace_dir(solo) != ws_a
    with pytest.raises(WorkspaceEscapeError):
        resolve_workspace_path(solo, "../x")


def test_project_file_tools_gating_and_collab(service_env, project_template):
    from domain.project.customer import create_customer_project
    from domain.service import emit_to_service
    from domain.service.capabilities import SERVICE_ONLY_TOOLS, service_only_tools_hidden

    _make_def(service_env)
    result = create_customer_project("协作验收", project_template)
    a, b = result["services"]

    # service-only 工具：服务可见、实例隐藏
    assert service_only_tools_hidden(a["service_id"]) is False
    assert service_only_tools_hidden("11111111-1111-1111-1111-111111111111") is True
    assert SERVICE_ONLY_TOOLS >= {"send_to_peer", "project_file_write"}

    # 工具实调：A 写共享文件 → B 可读（隔离验证：B 的库无 A 的事件）
    from infrastructure.config import set_current_instance_id, reset_current_instance_id

    import interfaces.tools.service_collab_tools as collab  # noqa: F401 注册

    token = set_current_instance_id(a["service_id"])
    try:
        w = collab._handle_project_file_write(
            {"path": "客户资料/需求.md", "content": "客户想优化商品经营"}
        )
        assert '"written": true' in w.lower()
    finally:
        reset_current_instance_id(token)

    token = set_current_instance_id(b["service_id"])
    try:
        r = collab._handle_project_file_read({"path": "客户资料/需求.md"})
        assert "商品经营" in r
        # 越界写在工具层被拒（不抛异常，返回 tool_error 文案）
        bad = collab._handle_project_file_write(
            {"path": "../escape.txt", "content": "x"}
        )
        assert "越界" in bad or "escape" in bad.lower() or "失败" in bad
        # 协作消息：B → A 走事件队列
        send = collab._handle_send_to_peer(
            {"peer_service_id": a["service_id"], "text": "请复核需求"}
        )
        assert '"sent": true' in send.lower()
    finally:
        reset_current_instance_id(token)

    # A 的队列收到协作事件（B 的库没有）
    import sqlite3 as _sq

    def _last_text(db, sid):
        conn = _sq.connect(str(db))
        rows = conn.execute(
            "SELECT payload FROM events WHERE channel LIKE ? ORDER BY event_id DESC LIMIT 1",
            (f"instance:{sid}%",),
        ).fetchall()
        conn.close()
        return rows

    root = service_env
    db_a = root / "apps" / "echo-def" / "services" / a["service_id"] / "data" / "state.db"
    last_a = _last_text(db_a, a["service_id"])
    assert last_a and "请复核需求" in last_a[0][0]
    db_b = root / "apps" / "echo-def" / "services" / b["service_id"] / "data" / "state.db"
    if db_b.exists():  # B 只发不收，库可能整个不存在——不存在即无泄漏
        last_b = _last_text(db_b, b["service_id"])
        assert not (last_b and "请复核需求" in last_b[0][0])


def test_loop_fires_due_alarms_respects_subscription(service_env):
    from domain.lifecycle.clock import now_dt
    from domain.service import create_service
    from infrastructure.persistence import services_registry as sr
    from infrastructure.scheduler.service_runner import ServiceWorkerLoop, _fire_service_alarms

    root = service_env
    _make_def(root)
    sid = create_service("echo-def")["service_id"]

    # 在服务上下文里设一个已到期的 timer（模拟 agent 自设 rest 闹钟到点）
    from infrastructure.config import set_current_instance_id, reset_current_instance_id
    from domain.lifecycle.events import set_instance_context, reset_instance_context

    cfg = set_current_instance_id(sid)
    evt = set_instance_context(sid)
    try:
        from domain.lifecycle.affairs.runtime import init_db

        init_db()
        from domain.lifecycle.alarms import set_alarm

        set_alarm("timer", fire_at=now_dt().isoformat(timespec="seconds"), payload={"note": "到点"})
    finally:
        reset_instance_context(evt)
        reset_current_instance_id(cfg)

    # 闹钟转换：timer → events 表新事件
    _fire_service_alarms(sid)
    db = root / "apps" / "echo-def" / "services" / sid / "data" / "state.db"
    conn = sqlite3.connect(str(db))
    timer_events = conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='timer' AND consumed_at IS NULL"
    ).fetchone()[0]
    conn.close()
    assert timer_events == 1

    # 订阅闸（默认只认消息类）：timer 到期也不拉起
    spawned: list[str] = []
    loop = ServiceWorkerLoop(stale_seconds=3600, spawn=lambda s: (spawned.append(s), _FakeProc())[1])
    assert loop.tick() == []
    assert spawned == []

    # 订阅 timer 后 → 拉起
    sr.update_service_fields(sid, subscriptions=["message", "timer"])
    assert loop.tick() == [sid]
    assert spawned == [sid]
