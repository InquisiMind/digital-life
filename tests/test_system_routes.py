"""System-level API contract tests.

Covers /api/system/* endpoints: overview, instance metadata PATCH,
project filtering, skills catalog + subscribe, event-types CRUD,
and instance assets.

测试不依赖运行的 server——直接调用 handler 函数 + monkeypatch project_root
统一指向 tmp_path，避免污染真实 apps/projects/config 目录。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest


# ───────────────── fixtures ─────────────────


@pytest.fixture
def isolated_project(monkeypatch, tmp_path):
    """把整个项目根替换成 tmp。

    关键：要 patch 的是 ``infrastructure.config.get_project_root``（registry
    扫描和 instance paths 都从这里取根），不是 system_routes 自己的引用。
    """
    import infrastructure.config as config_module
    import application.api.system_routes as sys_api

    monkeypatch.setattr(config_module, "get_project_root", lambda: tmp_path)
    monkeypatch.setattr(sys_api, "get_project_root", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def app_with_one_instance(isolated_project):
    """构造一个 fake instance 'alpha'，含 app.yaml 元数据。"""
    iid = "test-iid-alpha"
    apps_dir = isolated_project / "apps" / iid
    (apps_dir / "config").mkdir(parents=True)
    (apps_dir / "data").mkdir(parents=True)
    (apps_dir / "assets").mkdir()
    (apps_dir / "assets" / "avatar.png").write_bytes(b"\x89PNG fake")
    (apps_dir / "config" / "app.yaml").write_text(
        "active: true\n"
        "display_name: alpha\n"
        "avatar: assets/avatar.png\n"
        "accent_color: '#00f0ff'\n"
        "tagline: 测试实例\n"
        "skills:\n  - daily_planner\n",
        encoding="utf-8",
    )
    return iid


def _make_request(method: str, path: str, *, payload: Any = None, match_info: Any = None):
    """Helper: 构造带 payload 和 match_info 的 mocked aiohttp Request.

    payload 接口上和写到 body 一致——但 make_mocked_request 对带流的 Payload 模拟
    不完美；这里改用 AsyncMock 把 ``request.json()`` stub 成预置 dict，handler
    用 ``await request.json()`` 时直接拿到，避免 aiohttp 内部流读取错误。
    """
    from unittest.mock import AsyncMock

    from aiohttp.test_utils import make_mocked_request

    kwargs: dict[str, Any] = {}
    if match_info is not None:
        kwargs["match_info"] = match_info
    request = make_mocked_request(method, path, **kwargs)
    if payload is not None:
        # json() 是 async，必须用 AsyncMock
        request.json = AsyncMock(return_value=payload)
    return request


# ───────────────── overview / instances ─────────────────


def test_overview_aggregates_instances_and_projects(isolated_project, app_with_one_instance, monkeypatch):
    from application.api.system_routes import _handle_overview

    pid = "demo-proj"
    fake_projects = isolated_project / "projects" / pid
    fake_projects.mkdir(parents=True)
    (fake_projects / "project.yaml").write_text(
        f"name: demo\nmanager: {app_with_one_instance}\npositions: []\n",
        encoding="utf-8",
    )
    # domain.project.loader 用 Path(__file__).parents[2] 直算项目根，
    #绕不到 isolated tmp —— stub load_all_projects 让其只看到 fake
    import domain.project.loader as loader

    class _Proj:
        def __init__(self, pid, name, manager):
            self.id = pid
            self.name = name
            self.manager = manager
            self.description = ""
            self.status = "active"
            self.group_chat_id = ""
            self.positions = []

    monkeypatch.setattr(
        loader, "load_all_projects", lambda: {pid: _Proj(pid, "demo", app_with_one_instance)}
    )

    request = _make_request("GET", "/api/system/overview")
    resp = asyncio.run(_handle_overview(request))
    assert resp.status == 200
    data = json.loads(resp.text)
    assert data["instance_count"] == 1
    assert data["instances"][0]["display_name"] == "alpha"
    assert data["instances"][0]["accent_color"] == "#00f0ff"
    assert data["instances"][0]["avatar"] == "assets/avatar.png"
    assert data["instances"][0]["tagline"] == "测试实例"
    assert data["project_count"] == 1
    # active=True 但没有 state.db / vitals → energy=0 → runtime=resting（精力<20 自动归类），
    # process=online → visual=resting（三维度复合）
    assert data["instances"][0]["status"] in ("resting", "idle")


def test_patch_instance_metadata_persists_to_app_yaml(isolated_project, app_with_one_instance):
    """PATCH /api/system/instances/{iid} 应把新 avatar/accent/tagline 写进 app.yaml。"""
    from application.api.system_routes import _handle_update_instance

    body = {"avatar": "assets/new.gif", "accent_color": "#ff00ff", "tagline": "更新后"}
    request = _make_request(
        "PATCH",
        f"/api/system/instances/{app_with_one_instance}",
        payload=body,
        match_info={"iid": app_with_one_instance},
    )
    resp = asyncio.run(_handle_update_instance(request))
    assert resp.status == 200
    payload = json.loads(resp.text)
    assert payload["instance"]["avatar"] == "assets/new.gif"
    assert payload["instance"]["accent_color"] == "#ff00ff"
    assert payload["instance"]["tagline"] == "更新后"

    cfg_path = isolated_project / "apps" / app_with_one_instance / "config" / "app.yaml"
    cfg = cfg_path.read_text(encoding="utf-8")
    assert "assets/new.gif" in cfg
    assert "#ff00ff" in cfg


def test_patch_instance_rejects_empty_display_name(isolated_project, app_with_one_instance):
    from application.api.system_routes import _handle_update_instance

    request = _make_request(
        "PATCH",
        f"/api/system/instances/{app_with_one_instance}",
        payload={"display_name": "  "},
        match_info={"iid": app_with_one_instance},
    )
    resp = asyncio.run(_handle_update_instance(request))
    assert resp.status == 400


def test_patch_instance_with_unknown_iid_404(isolated_project):
    from application.api.system_routes import _handle_update_instance

    # 用真实路径（tmp root）下的"unknown_iid" —— registry 里没有
    request = _make_request(
        "PATCH",
        "/api/system/instances/unknown_iid",
        payload={"tagline": "x"},
        match_info={"iid": "unknown_iid"},
    )
    resp = asyncio.run(_handle_update_instance(request))
    assert resp.status == 404


# ───────────────── projects ─────────────────


def test_projects_filtered_by_participating_instance(isolated_project, app_with_one_instance, monkeypatch):
    """projects?iid=xxx 仅返回 该实例作为 position.assignee 或 manager 参与的项目。

    domain.project.loader 用文件 parent 计算根路径无法直接 monkeypatch，
    用一个 fake loader 返回受控的 2 个 project，验证 handler 的过滤逻辑。
    """
    from application.api.system_routes import _handle_projects
    import domain.project.loader as loader

    class _Position:
        def __init__(self, name, assignees):
            self.id = name
            self.name = name
            self.assignees = assignees

    class _Proj:
        def __init__(self, pid, name, manager, positions):
            self.id = pid
            self.name = name
            self.manager = manager
            self.description = ""
            self.status = "active"
            self.group_chat_id = ""
            self.positions = positions

        def get_position_for_instance(self, iid):
            for p in self.positions:
                if iid in p.assignees:
                    return p
            return None

    fake_projects = {
        "proj-1": _Proj(
            "proj-1", "proj-1", "someone-else",
            [_Position("worker", [app_with_one_instance])],
        ),
        "proj-2": _Proj("proj-2", "proj-2", "someone-else", []),
    }
    monkeypatch.setattr(loader, "load_all_projects", lambda: fake_projects)

    iid_query = f"?iid={app_with_one_instance}"
    request = _make_request("GET", f"/api/system/projects{iid_query}")
    resp = asyncio.run(_handle_projects(request))
    assert resp.status == 200
    data = json.loads(resp.text)
    names = [p["name"] for p in data["projects"]]
    assert names == ["proj-1"]


# ───────────────── skills catalog + subscribe ─────────────────


def test_skills_catalog_lists_system_and_shared(isolated_project):
    sys_skill = isolated_project / "interfaces" / "skills" / "alpha_skill"
    sys_skill.mkdir(parents=True)
    (sys_skill / "SKILL.md").write_text(
        "---\nname: alpha_skill\ndescription: 系统技能描述\n---\n# Alpha\n", encoding="utf-8"
    )
    shared_skill = isolated_project / "shared" / "skills" / "beta_skill"
    shared_skill.mkdir(parents=True)
    (shared_skill / "SKILL.md").write_text(
        "---\nname: beta_skill\ndescription: 共享技能\n---\n", encoding="utf-8"
    )

    from application.api.system_routes import _build_skills_catalog

    catalog = _build_skills_catalog()
    names_scopes = {(c["name"], c["scope"]) for c in catalog}
    assert ("alpha_skill", "system") in names_scopes
    assert ("beta_skill", "shared") in names_scopes

    sys_meta = next(c for c in catalog if c["name"] == "alpha_skill")
    assert sys_meta["description"] == "系统技能描述"


def test_skill_subscribe_toggles_app_yaml_list(isolated_project, app_with_one_instance):
    """订阅写入 app.yaml 的 skills list，再退订从 list 删除。"""
    from application.api.system_routes import (
        _handle_skill_subscribe,
        _read_instance_skills,
    )

    request = _make_request(
        "POST",
        "/api/system/skills/subscribe",
        payload={"instance_id": app_with_one_instance, "skill": "new_skill", "subscribed": True},
    )
    resp = asyncio.run(_handle_skill_subscribe(request))
    assert resp.status == 200
    assert "new_skill" in _read_instance_skills(app_with_one_instance)

    request2 = _make_request(
        "POST",
        "/api/system/skills/subscribe",
        payload={"instance_id": app_with_one_instance, "skill": "new_skill", "subscribed": False},
    )
    resp2 = asyncio.run(_handle_skill_subscribe(request2))
    assert resp2.status == 200
    assert "new_skill" not in _read_instance_skills(app_with_one_instance)


# ───────────────── event types CRUD ─────────────────


def test_event_type_create_read_update_delete(isolated_project):
    from application.api.system_routes import (
        _handle_create_event_type,
        _handle_delete_event_type,
        _handle_list_event_types,
        _handle_update_event_type,
    )

    # Create
    body = {
        "type_id": "my_event",
        "display_name": "My Event",
        "trigger_type": "message",
        "allowed_tools": ["express_to_human", "record_thought"],
        "prompt": "test prompt",
    }
    request = _make_request("POST", "/api/system/event-types", payload=body)
    resp = asyncio.run(_handle_create_event_type(request))
    assert resp.status == 200

    manifest = isolated_project / "config" / "event-packages" / "my_event" / "manifest.yaml"
    assert manifest.exists()
    raw = manifest.read_text(encoding="utf-8")
    assert "type_id: my_event" in raw
    assert "express_to_human,record_thought" in raw

    # List
    request = _make_request("GET", "/api/system/event-types")
    resp = asyncio.run(_handle_list_event_types(request))
    assert resp.status == 200
    data = json.loads(resp.text)
    assert data["count"] == 1
    item = data["event_types"][0]
    assert item["type_id"] == "my_event"
    assert item["allowed_tools"] == ["express_to_human", "record_thought"]

    # Create conflict (POST 同 type_id)
    resp_conflict = asyncio.run(_handle_create_event_type(
        _make_request("POST", "/api/system/event-types", payload=body)
    ))
    assert resp_conflict.status == 409

    # Update
    put_req = _make_request(
        "PUT",
        "/api/system/event-types/my_event",
        payload={"display_name": "Updated Name"},
        match_info={"type_id": "my_event"},
    )
    resp_put = asyncio.run(_handle_update_event_type(put_req))
    assert resp_put.status == 200
    payload = json.loads(resp_put.text)
    assert payload["event_type"]["display_name"] == "Updated Name"

    # Delete
    del_req = _make_request(
        "DELETE",
        "/api/system/event-types/my_event",
        match_info={"type_id": "my_event"},
    )
    resp_del = asyncio.run(_handle_delete_event_type(del_req))
    assert resp_del.status == 200
    assert not manifest.exists()


def test_event_type_safe_relative_name_sanitizes():
    """_safe_relative_name 删除非法字符——越界操作被精简后变成安全的纯名字，
    不再能表示路径（'..' 经过字符过滤后变空、'a/b' 变 'ab'）。
    """
    from application.api.system_routes import _safe_relative_name

    # 越界尝试：'..' 删除后变空 → 拒绝
    assert _safe_relative_name("..") == ""
    assert _safe_relative_name("") == ""
    assert _safe_relative_name(".") == ""
    # 含路径分隔 → 被清理成扁平名（不能表达子目录）
    assert _safe_relative_name("a/b") == "ab"
    assert _safe_relative_name("../etc/passwd") == "etcpasswd"
    # 正常命名
    assert _safe_relative_name("valid-event_123") == "valid-event_123"


# ───────────────── assets 静态服务 ─────────────────


def test_instance_asset_serves_file(isolated_project, app_with_one_instance):
    from application.api.system_routes import _handle_instance_asset

    request = _make_request(
        "GET",
        f"/employee/{app_with_one_instance}/assets/avatar.png",
        match_info={"iid": app_with_one_instance, "filename": "avatar.png"},
    )
    resp = asyncio.run(_handle_instance_asset(request))
    assert resp.status == 200


def test_instance_asset_rejects_traversal(isolated_project, app_with_one_instance):
    """filename 含 '..' 或 '/' → 直接 400 拒绝（_is_safe_filename 检查）。"""
    from application.api.system_routes import _handle_instance_asset

    request = _make_request(
        "GET",
        f"/employee/{app_with_one_instance}/assets/../../etc/passwd",
        match_info={"iid": app_with_one_instance, "filename": "../../etc/passwd"},
    )
    resp = asyncio.run(_handle_instance_asset(request))
    assert resp.status == 400


def test_instance_asset_404_when_missing(isolated_project, app_with_one_instance):
    from application.api.system_routes import _handle_instance_asset

    request = _make_request(
        "GET",
        f"/employee/{app_with_one_instance}/assets/nonexistent.png",
        match_info={"iid": app_with_one_instance, "filename": "nonexistent.png"},
    )
    resp = asyncio.run(_handle_instance_asset(request))
    assert resp.status == 404


# ───────────────── runtime_state / has_active_wake ─────────────────


def _bootstrap_runtime_dbs(project_root, iid):
    """构造一个 fake instance 的 state.db + runtime_log.db，足以驱动
    _read_instance_runtime_state：affair RUNNING + vitals energy + wake/turn 表。
    返回 (state_db_path, runtime_db_path)。
    """
    import sqlite3

    data_dir = project_root / "apps" / iid / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    state_db = data_dir / "state.db"
    runtime_db = data_dir / "runtime_log.db"

    # state.db: vitals(energy=80) + affairs(RUNNING)
    conn = sqlite3.connect(str(state_db))
    try:
        conn.execute("CREATE TABLE vitals (energy REAL)")
        conn.execute("INSERT INTO vitals (energy) VALUES (80)")
        conn.execute(
            "CREATE TABLE affairs (affair_id TEXT PRIMARY KEY, goal TEXT, "
            "status TEXT, priority INTEGER, deadline TEXT, session_id TEXT, "
            "mental_context TEXT, history_digest TEXT, created_at TEXT, "
            "updated_at TEXT, completed_at TEXT, meta_json TEXT)"
        )
        conn.execute(
            "INSERT INTO affairs (affair_id, goal, status, priority, updated_at) "
            "VALUES ('a1', 'g', 'RUNNING', 0, '2026-06-25T12:00:00+08:00')"
        )
        # flow_event_log_events 空表 → 不会有 critical 事件
        conn.execute(
            "CREATE TABLE flow_event_log_events (id INTEGER PRIMARY KEY, "
            "severity TEXT, timestamp TEXT, summary TEXT)"
        )
        conn.commit()
    finally:
        conn.close()

    # runtime_log.db: wake + turn（无 error turn → health ok）
    conn = sqlite3.connect(str(runtime_db))
    try:
        conn.execute(
            "CREATE TABLE wake (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "instance_id TEXT NOT NULL, wake_seq INTEGER NOT NULL, "
            "meta_json TEXT NOT NULL DEFAULT '{}', started_at REAL NOT NULL, "
            "ended_at REAL, UNIQUE(instance_id, wake_seq))"
        )
        conn.execute(
            "CREATE TABLE turn (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "instance_id TEXT NOT NULL, wake_id INTEGER NOT NULL, "
            "wake_seq INTEGER NOT NULL, llm_call_seq INTEGER NOT NULL, "
            "position_in_call INTEGER NOT NULL DEFAULT 0, role TEXT NOT NULL, "
            "content TEXT, tool_name TEXT, tool_call_id TEXT, tool_calls TEXT, "
            "reasoning TEXT, finish_reason TEXT, token_count INTEGER, "
            "chat_id TEXT, error TEXT, timestamp REAL NOT NULL)"
        )
        # 最近一条 turn 是无 error 的 assistant turn → health ok
        conn.execute(
            "INSERT INTO turn (instance_id, wake_id, wake_seq, llm_call_seq, "
            "role, timestamp) VALUES (?, 1, 1, 1, 'assistant', 1.0)",
            (iid,),
        )
        conn.commit()
    finally:
        conn.close()

    return state_db, runtime_db


def test_runtime_state_idle_when_latest_wake_ended(isolated_project):
    """最新一条 wake 已 ended → idle（即使存在历史 ended_at IS NULL 孤儿）。

    回归 bug：旧逻辑用 "任何一条 ended_at IS NULL" 判 working，
    孤儿 wake 会把状态灯永久钉成 working。
    """
    import sqlite3

    iid = "test-iid-runtime"
    state_db, runtime_db = _bootstrap_runtime_dbs(isolated_project, iid)

    # wake 表：1 号（孤儿，ended_at NULL）+ 2 号（最新，已 ended）
    conn = sqlite3.connect(str(runtime_db))
    try:
        conn.execute(
            "INSERT INTO wake (instance_id, wake_seq, started_at) VALUES (?, 1, 1.0)",
            (iid,),
        )  # 孤儿：ended_at NULL
        conn.execute(
            "INSERT INTO wake (instance_id, wake_seq, started_at, ended_at) "
            "VALUES (?, 2, 2.0, 3.0)",
            (iid,),
        )  # 最新：已 ended
        conn.commit()
    finally:
        conn.close()

    from application.api.system_routes import _read_instance_runtime_state

    _energy, runtime_state, _process, _health, _reason = _read_instance_runtime_state(
        iid, active=True
    )
    assert runtime_state == "idle", "最新 wake 已结束但有孤儿 NULL，应为 idle"


def test_runtime_state_working_when_latest_wake_running(isolated_project):
    """最新一条 wake 仍 running（ended_at NULL）→ working。"""
    import sqlite3

    iid = "test-iid-runtime"
    state_db, runtime_db = _bootstrap_runtime_dbs(isolated_project, iid)

    # wake 表：1 号历史已 ended + 2 号最新仍 running
    conn = sqlite3.connect(str(runtime_db))
    try:
        conn.execute(
            "INSERT INTO wake (instance_id, wake_seq, started_at, ended_at) "
            "VALUES (?, 1, 1.0, 2.0)",
            (iid,),
        )
        conn.execute(
            "INSERT INTO wake (instance_id, wake_seq, started_at) VALUES (?, 2, 3.0)",
            (iid,),
        )  # 最新：未 ended
        conn.commit()
    finally:
        conn.close()

    from application.api.system_routes import _read_instance_runtime_state

    _energy, runtime_state, _process, _health, _reason = _read_instance_runtime_state(
        iid, active=True
    )
    assert runtime_state == "working", "最新 wake 仍在跑，应为 working"


# ───────────────── DELETE instance ─────────────────


class _FakeSupervisor:
    """轻量 stub：stop_instance 返回 bool，避免依赖InstanceState 真实实现。"""
    def __init__(self):
        self.stopped: list[str] = []

    async def stop_instance(self, instance_id):
        self.stopped.append(instance_id)
        return True


def test_delete_instance_removes_dir_and_stops_process(isolated_project, app_with_one_instance):
    """DELETE /api/system/instances/{iid}——彻底删除实例：停子进程 + 物理删目录。"""
    from application.api.system_routes import _handle_delete_instance
    from infrastructure.config import get_instance_dir

    inst_dir = get_instance_dir(app_with_one_instance)
    assert inst_dir.exists(), "fixture 应已创建实例目录"

    sup = _FakeSupervisor()
    request = _make_request(
        "DELETE",
        f"/api/system/instances/{app_with_one_instance}",
        match_info={"iid": app_with_one_instance},
    )
    # request.app 是 MagicMock，要 stub .get("supervisor") 直接返回我们的 sup 对象
    request.app.get = lambda key: sup if key == "supervisor" else None

    resp = asyncio.run(_handle_delete_instance(request))
    assert resp.status == 200
    payload = json.loads(resp.text)
    assert payload["ok"] is True
    assert payload["instance_id"] == app_with_one_instance
    assert payload["process"] == "已停止"
    assert sup.stopped == [app_with_one_instance]
    assert not inst_dir.exists(), "apps/{id}/ 物理目录必须被删除"


def test_delete_instance_404_when_unknown(isolated_project):
    """删除 registry 里不存在的实例 → 404。"""
    from application.api.system_routes import _handle_delete_instance

    request = _make_request(
        "DELETE",
        "/api/system/instances/no-such-iid",
        match_info={"iid": "no-such-iid"},
    )
    resp = asyncio.run(_handle_delete_instance(request))
    assert resp.status == 404


def test_delete_instance_survives_without_supervisor(isolated_project, app_with_one_instance):
    """没注入 supervisor 时也应继续删目录（进程靠自然回收/watch_loop 兜底）。"""
    from application.api.system_routes import _handle_delete_instance
    from infrastructure.config import get_instance_dir

    inst_dir = get_instance_dir(app_with_one_instance)
    request = _make_request(
        "DELETE",
        f"/api/system/instances/{app_with_one_instance}",
        match_info={"iid": app_with_one_instance},
    )
    # request.app 是 MagicMock；stub .get 显式返回 None 表示无 supervisor
    request.app.get = lambda key: None

    resp = asyncio.run(_handle_delete_instance(request))
    assert resp.status == 200
    assert not inst_dir.exists()


def test_delete_instance_symlinks_or_traversal_rejected(isolated_project, app_with_one_instance):
    """iid 不能用相对路径/绝对路径越界 apps/——根本走不到路由（{iid} 不含 / ），
    但 handler 的 resolve 检查是 defense-in-depth。这里直接验证 iid 形如 unknown
    但碰巧某段路径返回 404。"""
    from application.api.system_routes import _handle_delete_instance

    request = _make_request(
        "DELETE",
        "/api/system/instances/..%2Fetc",
        match_info={"iid": "..%2Fetc"},
    )
    resp = asyncio.run(_handle_delete_instance(request))
    # registry 里没有 → 404（registry 是动态扫 apps/{iid}/ 构造，路径遍历绕不过）
    assert resp.status == 404
