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
