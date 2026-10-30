"""协作台（员工平台）测试 — 建组/消息路由/任务交接/工具面/API 契约。

分层对齐 test_services_runtime 的隔离方式：全部库重定向 tmp、钩子还原、
注册表缓存清零。LLM/wake 一律不真跑（emit 捕获断言）。
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import contextmanager
from pathlib import Path

import pytest

from business.teamwork import registry as tw_reg
from infrastructure.persistence import services_registry

LEADER_ID = "leader-0000-0000-0000-000000000001"


def _restore_default_runtime_hooks() -> None:
    from domain.lifecycle.affairs import runtime as affair_runtime

    affair_runtime._db_path_hook = lambda: Path(
        affair_runtime._resolve_default_state_db()
    )
    affair_runtime._now_iso_hook = affair_runtime._default_now_iso
    affair_runtime._now_dt_hook = affair_runtime._default_now_dt
    affair_runtime._parse_iso_hook = affair_runtime._default_parse_iso


@pytest.fixture()
def teamwork_env(tmp_path, monkeypatch):
    db_path = tmp_path / "data" / "services.db"
    monkeypatch.setenv("DIGITAL_LIFE_SERVICES_DB", str(db_path))
    monkeypatch.setenv(
        "DIGITAL_LIFE_TEAMWORK_DB", str(tmp_path / "data" / "teamwork.db")
    )
    monkeypatch.setenv(
        "DIGITAL_LIFE_TOOL_VISIBILITY_DB", str(tmp_path / "data" / "tool_visibility.db")
    )
    monkeypatch.setattr("infrastructure.config.get_project_root", lambda: tmp_path)
    monkeypatch.setattr("infrastructure.config.discover_instances", lambda: [LEADER_ID])
    _restore_default_runtime_hooks()
    services_registry.reset_for_test()
    tw_reg.reset_for_test()
    from domain import tool_visibility as _tv
    from infrastructure.persistence import tool_visibility as _tvs

    _tvs.reset_for_test()
    _tv._wake_grants.clear()

    _make_def(tmp_path, "researcher-def", "研究员")
    _make_def(tmp_path, "coder-def", "工程师")
    _make_leader(tmp_path)

    yield tmp_path
    os.environ.pop("DIGITAL_LIFE_INSTANCE_ID", None)
    services_registry.reset_for_test()
    tw_reg.reset_for_test()
    _tvs.reset_for_test()
    _tv._wake_grants.clear()
    _restore_default_runtime_hooks()


def _make_def(root: Path, def_id: str, display: str) -> None:
    cfg = root / "apps" / def_id / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (root / "apps" / def_id / "persona").mkdir(parents=True, exist_ok=True)
    (cfg / "app.yaml").write_text(
        f"runtime_kind: definition\ndisplay_name: {display}\nactive: false\n"
        "description: 测试员工定义\nteamwork:\n  role: member\n",
        encoding="utf-8",
    )
    (root / "apps" / def_id / "persona" / "LIFE_PERSONA.md").write_text(
        "# def\n", encoding="utf-8"
    )


def _make_leader(root: Path) -> None:
    cfg = root / "apps" / LEADER_ID / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (root / "apps" / LEADER_ID / "persona").mkdir(parents=True, exist_ok=True)
    (cfg / "app.yaml").write_text(
        "display_name: 领导助手\nactive: true\nteamwork:\n  role: leader\n"
        "subscriptions_default:\n- message\n- group_message\n- timer\n",
        encoding="utf-8",
    )


class _Capture:
    """emit_to_service / emit_event 捕获器。"""

    def __init__(self):
        self.service_events: list[tuple[str, str, dict]] = []
        self.instance_events: list[tuple[str, dict, str | None]] = []

    def install(self, monkeypatch):
        async def _noop_async(*a, **k):  # pragma: no cover
            return 0

        def _fake_emit_to_service(service_id, kind, payload=None, **kw):
            self.service_events.append((service_id, kind, payload or {}))
            return len(self.service_events)

        def _fake_emit_event(kind, payload=None, fire_at=None, channel=None):
            self.instance_events.append((kind, payload or {}, channel))
            return len(self.instance_events)

        monkeypatch.setattr("domain.service.emit_to_service", _fake_emit_to_service)
        monkeypatch.setattr("domain.lifecycle.events.emit_event", _fake_emit_event)
        return self


def _build_group(monkeypatch, *, todos: list | None = None):
    from business.teamwork.groups import create_group

    return create_group(
        "官网改版",
        leader_id=LEADER_ID,
        brief="把官网首页改版",
        members=[
            {"def": "researcher-def", "name": "研究员"},
            {"def": "coder-def", "name": "工程师"},
        ],
        initial_todos=todos or [],
    )


# ── 员工目录 / 建组 ───────────────────────────────────────────────────


def test_employee_catalog_lists_definitions_only(teamwork_env):
    from business.teamwork.groups import employee_catalog

    (teamwork_env / "apps" / "some-instance" / "config").mkdir(parents=True)
    (teamwork_env / "apps" / "some-instance" / "config" / "app.yaml").write_text(
        "active: true\n", encoding="utf-8"
    )
    rows = employee_catalog()
    names = {r["def"] for r in rows}
    assert {"researcher-def", "coder-def"} <= names
    assert "some-instance" not in names
    assert all(r["name"] and r["def"] for r in rows)


def test_create_group_bootstraps_everything(teamwork_env, monkeypatch):
    cap = _Capture().install(monkeypatch)
    result = _build_group(
        monkeypatch,
        todos=[
            {"title": "调研", "assign_name": "研究员"},
            {"title": "改版", "assign_name": "工程师", "after": ["调研"]},
        ],
    )
    gid = result["group"]["group_id"]
    # 成员服务按 def 创建 + 挂组
    sids = [m["service_id"] for m in result["members"]]
    assert len(sids) == 2
    for sid in sids:
        svc = services_registry.lookup_service(sid)
        assert svc and svc["project_id"] == gid
    # 领导是当然成员（组织者）
    members = tw_reg.list_group_members(gid)
    lead_rows = [m for m in members if m["member_id"] == LEADER_ID]
    assert lead_rows and lead_rows[0]["is_leader"] == 1
    # 任务：有前置的 blocked，前置完成后解锁（在 todo 流程用例细验）
    todos = tw_reg.list_group_todos(gid)
    assert {t["title"]: t["status"] for t in todos} == {
        "调研": "open",
        "改版": "blocked",
    }
    # 目录骨架
    assert (teamwork_env / "groups" / gid / "shared").is_dir()
    for sid in sids:
        assert (teamwork_env / "groups" / gid / "members" / sid).is_dir()
    # 开场唤醒：group_joined 给每位服务成员
    kinds = [(sid, kind) for sid, kind, _ in cap.service_events]
    assert all(kind == "group_joined" for _, kind in kinds)
    assert {sid for sid, _ in kinds} == set(sids)


def test_create_group_warns_on_non_teamwork_def(teamwork_env, monkeypatch):
    """未接成员面的 def 不硬拦，但建组结果带明确警告（嘴没接、发不出消息）。"""
    _Capture().install(monkeypatch)
    (teamwork_env / "apps" / "outsider-def" / "config").mkdir(parents=True)
    (teamwork_env / "apps" / "outsider-def" / "config" / "app.yaml").write_text(
        "runtime_kind: definition\ndisplay_name: 局外人\n", encoding="utf-8")
    from business.teamwork.groups import create_group

    result = create_group(
        "警告组", leader_id=LEADER_ID,
        members=[{"def": "researcher-def", "name": "研究员"},
                 {"def": "outsider-def", "name": "局外人"}])
    assert result["warnings"] and "outsider-def" in result["warnings"][0]
    assert len(result["warnings"]) == 1  # 已接入的成员不告警
    # 建组本身成功（不硬闸）
    assert len(result["members"]) == 2


def test_create_group_rejects_bad_inputs(teamwork_env, monkeypatch):
    cap = _Capture().install(monkeypatch)
    from business.teamwork.groups import create_group

    with pytest.raises(ValueError):
        create_group("", leader_id=LEADER_ID, members=[{"def": "researcher-def"}])
    with pytest.raises(ValueError):
        create_group("空组", leader_id=LEADER_ID, members=[])
    with pytest.raises(ValueError):
        create_group("坏 def", leader_id=LEADER_ID, members=[{"def": "no-such-def"}])
    assert cap.service_events == []


# ── 消息路由 ──────────────────────────────────────────────────────────


def test_group_fanout_leader_wakes_all(teamwork_env, monkeypatch):
    cap = _Capture().install(monkeypatch)
    result = _build_group(monkeypatch)
    gid = result["group"]["group_id"]
    sids = [m["service_id"] for m in result["members"]]
    cap.service_events.clear()

    from business.teamwork.messenger import send

    r = send(LEADER_ID, f"teamgroup:{gid}", "各位，开始干活")
    assert r["ok"] and r["kind"] == "group"
    # 领导发言全员唤醒
    woken = [sid for sid, kind, _ in cap.service_events if kind == "group_message"]
    assert set(woken) == set(sids)
    # 台账一份、每人信箱一份、领导自己出站一份
    assert len(tw_reg.list_group_messages(gid)) == 1
    from business.teamwork.messenger import _runtime_context
    from domain.messages import list_messages

    with _runtime_context(sids[0]):
        inbox = list_messages(f"teamgroup:{gid}")
    assert inbox and inbox[-1]["text"] == "各位，开始干活"


def test_group_member_send_mention_only(teamwork_env, monkeypatch):
    cap = _Capture().install(monkeypatch)
    result = _build_group(monkeypatch)
    gid = result["group"]["group_id"]
    sids = [m["service_id"] for m in result["members"]]
    researcher, coder = sids[0], sids[1]
    cap.service_events.clear()

    from business.teamwork.messenger import send

    # 无 @：其他成员静默落库，领导（实例侧 emit_event）被叫醒
    r = send(researcher, f"teamgroup:{gid}", "我先看下资料")
    assert r["ok"]
    assert cap.service_events == []  # 工程师未被 @
    assert any(kind == "group_message" for kind, _, _ in cap.instance_events)
    # @ 角色：对应成员被唤醒
    cap.service_events.clear()
    cap.instance_events.clear()
    r = send(researcher, f"teamgroup:{gid}", "@工程师 接口方案定了吗")
    assert r["ok"]
    assert [sid for sid, kind, _ in cap.service_events if kind == "group_message"] == [
        coder
    ]


def test_boss_window_is_leader_only(teamwork_env, monkeypatch):
    _Capture().install(monkeypatch)
    result = _build_group(monkeypatch)
    member_sid = result["members"][0]["service_id"]

    from business.teamwork.messenger import send

    r = send(member_sid, f"boss:{LEADER_ID}", "我想跟老板聊聊")
    assert not r["ok"] and "boss" in r["error"]
    r = send(LEADER_ID, f"boss:{LEADER_ID}", "老板，进展汇报")
    assert r["ok"] and r["kind"] == "boss"
    from business.teamwork.messenger import _runtime_context
    from domain.messages import list_messages

    with _runtime_context(LEADER_ID):
        win = list_messages(f"boss:{LEADER_ID}")
    assert win and win[-1]["direction"] == "out"


def test_boss_message_to_leader_injects_instance_event(teamwork_env, monkeypatch):
    cap = _Capture().install(monkeypatch)
    from business.teamwork.messenger import boss_message_to_leader

    r = boss_message_to_leader(LEADER_ID, "帮我拉个组做官网改版", boss_name="张总")
    assert r["ok"] and r["delivered"]
    # 实例 message 事件（非服务通道）
    kinds = [(k, ch) for k, _, ch in cap.instance_events]
    assert kinds and kinds[-1][0] == "message" and kinds[-1][1] == "teamwork:web"
    # 领导信箱有入站
    from business.teamwork.messenger import _runtime_context, boss_chat_id
    from domain.messages import list_messages

    with _runtime_context(LEADER_ID):
        inbox = list_messages(boss_chat_id(LEADER_ID))
    assert inbox and inbox[-1]["sender_name"] == "张总"


# ── 任务交接 ──────────────────────────────────────────────────────────


def test_todo_assign_and_unlock_chain(teamwork_env, monkeypatch):
    cap = _Capture().install(monkeypatch)
    result = _build_group(monkeypatch)
    gid = result["group"]["group_id"]
    sids = [m["service_id"] for m in result["members"]]
    researcher = sids[0]
    cap.service_events.clear()

    todo = tw_reg.create_group_todo(
        gid, "写周报", assignee_id=researcher, created_by=LEADER_ID
    )
    dep = tw_reg.create_group_todo(
        gid,
        "收集数据",
        assignee_id=researcher,
        created_by=LEADER_ID,
        depends_on=["写周报"],
        status="blocked",
    )
    assert dep["status"] == "blocked"

    # 完成 → 解锁 + 通知解锁人
    ok, unlocked = tw_reg.update_group_todo(
        todo["todo_id"], status="done", actor=researcher
    )
    assert ok and [u["todo_id"] for u in unlocked] == [dep["todo_id"]]
    tw_reg.notify_unlocked(gid, unlocked, actor=researcher, done_title="写周报")
    join_notifies = [
        p
        for _, kind, p in cap.service_events
        if kind == "group_todo_assigned" and "解锁" in (p.get("detail") or "")
    ]
    assert join_notifies and join_notifies[0]["todo_id"] == dep["todo_id"]


def test_todo_update_notifies_new_assignee(teamwork_env, monkeypatch):
    """转派通知是工具层职责（registry 刻意只管事实，不管唤醒）。"""
    cap = _Capture().install(monkeypatch)
    result = _build_group(monkeypatch)
    gid = result["group"]["group_id"]
    sids = [m["service_id"] for m in result["members"]]
    todo = tw_reg.create_group_todo(gid, "起草方案", created_by=LEADER_ID)

    from business.teamwork import tools

    with _as_runtime(LEADER_ID):
        cap.service_events.clear()
        out = json.loads(
            tools._handle_group_todo_update(
                {"todo_id": todo["todo_id"], "assignee_id": sids[1]}
            )
        )
        assert out["updated"] is True
    assigned = [p for _, kind, p in cap.service_events if kind == "group_todo_assigned"]
    assert assigned and assigned[0]["action"] == "转派给你"
    assert assigned[0]["todo_id"] == todo["todo_id"]


# ── 工具面 / 领导识别 / 收队 ──────────────────────────────────────────


def test_face_grants_and_leader_detection(teamwork_env, monkeypatch):
    _Capture().install(monkeypatch)
    from business.teamwork.groups import create_group

    result = create_group(
        "面测试",
        leader_id=LEADER_ID,
        members=[{"def": "researcher-def", "name": "研究员"}],
    )
    sid = result["members"][0]["service_id"]
    from domain import tool_visibility

    granted = tool_visibility.grants_for(sid)
    assert {"sense_work_group", "send_team_message", "group_todo_update"} <= granted
    assert "create_work_group" not in granted  # 领导专属不发给成员

    from business.teamwork.face import (
        ensure_leader_face,
        is_group_leader,
        leader_speech_tool,
    )

    assert is_group_leader(LEADER_ID)
    assert not is_group_leader(sid)
    assert leader_speech_tool(LEADER_ID) == "send_group_message"
    assert leader_speech_tool(sid) is None

    ensure_leader_face(LEADER_ID)
    leader_granted = tool_visibility.grants_for(LEADER_ID)
    assert {"create_work_group", "send_group_message", "close_group"} <= leader_granted


def test_face_tools_registered_after_ensure_loaded():
    """面清单 ⊆ 注册表：岗位面发空枪 = 漂移，走真实装载路径拦住。"""
    from interfaces.tools import ensure_loaded

    ensure_loaded()
    from business.teamwork.face import LEADER_FACE_TOOLS, MEMBER_FACE_TOOLS
    from interfaces.tools.registry import registry

    missing = sorted(
        t
        for t in (MEMBER_FACE_TOOLS | LEADER_FACE_TOOLS)
        if registry.get_schema(t) is None
    )
    assert not missing, f"工具面声明了未注册的工具: {missing}"
    # 领导专属工具必须 service_only（不进任何默认面）
    for t in ("create_work_group", "close_group", "send_group_message"):
        assert registry.get_tool_metadata(t).get("service_only") is True


def test_close_group_archives_services(teamwork_env, monkeypatch):
    _Capture().install(monkeypatch)
    result = _build_group(monkeypatch)
    gid = result["group"]["group_id"]
    sids = [m["service_id"] for m in result["members"]]

    from business.teamwork.groups import close_group

    r = close_group(gid, reason="干完了", actor=LEADER_ID)
    assert r["ok"] and r["archived"] == 2
    assert tw_reg.lookup_group(gid)["status"] == "closed"
    for sid in sids:
        assert services_registry.lookup_service(sid)["status"] == "archived"

    from business.teamwork.messenger import send

    r = send(LEADER_ID, f"teamgroup:{gid}", "再说一句")
    assert not r["ok"] and "归档" in r["error"]


# ── 工具 handler（含越界与身份闸） ────────────────────────────────────


@contextmanager
def _as_runtime(runtime_id: str):
    """把「当前运行体」切到指定 id：走基座官方 ContextVar（worker 同款路径）。

    不能 monkeypatch get_app_instance_id——协作工具模块对它按模块顶层绑定
    （与 consulting 同款），且 ContextVar 优先级高于环境变量，前序测试泄漏
    的 ContextVar 会让 setattr 补丁失真。
    """
    from infrastructure.config import reset_current_instance_id, set_current_instance_id

    token = set_current_instance_id(runtime_id)
    old_env = os.environ.get("DIGITAL_LIFE_INSTANCE_ID")
    os.environ["DIGITAL_LIFE_INSTANCE_ID"] = runtime_id
    try:
        yield
    finally:
        reset_current_instance_id(token)
        if old_env is None:
            os.environ.pop("DIGITAL_LIFE_INSTANCE_ID", None)
        else:
            os.environ["DIGITAL_LIFE_INSTANCE_ID"] = old_env


def test_tool_create_group_leader_only(teamwork_env, monkeypatch):
    _Capture().install(monkeypatch)
    from business.teamwork import tools

    with _as_runtime(LEADER_ID):
        out = json.loads(
            tools._handle_create_work_group(
                {
                    "name": "工具建组",
                    "brief": "b",
                    "members": [{"def": "coder-def", "name": "工程师"}],
                }
            )
        )
        assert out["group"]["name"] == "工具建组"
        assert out["chat_id"].startswith("teamgroup:")

    gid = out["group"]["group_id"]
    member_sid = tw_reg.list_services_by_group(gid)[0]["service_id"]
    with _as_runtime(member_sid):
        err = tools._handle_create_work_group(
            {"name": "x", "members": [{"def": "coder-def"}]}
        )
        assert "领导" in err


def test_tool_group_file_isolation(teamwork_env, monkeypatch):
    _Capture().install(monkeypatch)
    from business.teamwork.groups import create_group

    result = create_group(
        "文件组",
        leader_id=LEADER_ID,
        members=[
            {"def": "researcher-def", "name": "研究员"},
            {"def": "coder-def", "name": "工程师"},
        ],
    )
    result["group"]["group_id"]
    sids = [m["service_id"] for m in result["members"]]
    a, b = sids
    from business.teamwork import tools

    with _as_runtime(a):
        out = json.loads(
            tools._handle_group_file_write(
                {"path": f"members/{a}/draft.md", "content": "草稿"}
            )
        )
        assert out["written"] is True
        # 写他人个人区 → 拒
        err = tools._handle_group_file_write(
            {"path": f"members/{b}/hack.md", "content": "x"}
        )
        assert "受限" in err or "越界" in err
        # 读他人个人区 → 拒；读 shared → 允许
        err = tools._handle_group_file_read({"path": f"members/{b}/draft.md"})
        assert "受限" in err or "越界" in err
        d = json.loads(tools._handle_deliver_group_file({"path": "draft.md"}))
        assert d["delivered"] == "draft.md"
    with _as_runtime(b):
        content = tools._handle_group_file_read({"path": "shared/draft.md"})
        assert content == "草稿"
        # 越出组根（词法逃逸）→ 拒
        err = tools._handle_group_file_read({"path": "../../secrets.yaml"})
        assert "越界" in err


def test_tool_todo_summary_and_assignee_resolution(teamwork_env, monkeypatch):
    _Capture().install(monkeypatch)
    from business.teamwork.groups import create_group

    result = create_group(
        "任务组",
        leader_id=LEADER_ID,
        members=[{"def": "researcher-def", "name": "研究员"}],
    )
    gid = result["group"]["group_id"]
    member_sid = result["members"][0]["service_id"]
    from business.teamwork import tools

    with _as_runtime(LEADER_ID):
        out = json.loads(
            tools._handle_group_todo_create(
                {"title": "调研竞品", "assignee_id": "研究员", "group_id": gid}
            )
        )
        assert out["assignee_id"] == member_sid
        # 坏 assignee
        err = tools._handle_group_todo_create(
            {"title": "x", "assignee_id": "不存在的人", "group_id": gid}
        )
        assert "不在本组" in err
    with _as_runtime(member_sid):
        summary = json.loads(tools._handle_group_todo_list({"scope": "summary"}))
        assert summary["total"] == 1 and summary["by_member"]["研究员"]["open"] == 1


# ── API 契约 ──────────────────────────────────────────────────────────


class _FakeRequest:
    def __init__(self, body=None, match_info=None, query=None):
        self._body = body or {}
        self.match_info = match_info or {}
        self.query = query or {}

    async def json(self):
        return self._body


def _run(coro):
    return asyncio.run(coro)


def test_api_bootstrap_and_boss_roundtrip(teamwork_env, monkeypatch):
    _Capture().install(monkeypatch)
    from business.teamwork import api

    boot = _run(api._handle_bootstrap(_FakeRequest()))
    assert boot.status == 200
    leaders = json.loads(boot.body)
    assert leaders["leaders"] and leaders["leaders"][0]["leader_id"] == LEADER_ID

    # 统一会话列表：领导对话（kind=service）在册
    rows = json.loads(_run(api.handle_list_sessions(_FakeRequest())).body)["projects"]
    lead_row = next(r for r in rows if r["kind"] == "service")
    assert lead_row["project_id"] == LEADER_ID

    # boss 发言 → 领导窗口回放（in/out 方向）
    resp = _run(api.handle_send(_FakeRequest(
        {"sid": LEADER_ID, "text": "第一个需求", "boss_name": "张总"})))
    assert json.loads(resp.body)["ok"] is True
    data = json.loads(
        _run(api.handle_get_messages(_FakeRequest(match_info={"sid": LEADER_ID}))).body
    )
    assert data["kind"] == "leader"
    assert data["messages"][-1]["from"] == "customer"  # in=boss 发言（前端契约）
    assert data["messages"][-1]["text"] == "第一个需求"

    # 不存在的会话 404
    resp = _run(api.handle_get_messages(_FakeRequest(match_info={"sid": "nope"})))
    assert resp.status == 404


def test_api_groups_flow(teamwork_env, monkeypatch):
    _Capture().install(monkeypatch)
    from business.teamwork import api
    from business.teamwork.groups import create_group

    result = create_group(
        "API组", leader_id=LEADER_ID, members=[{"def": "coder-def", "name": "工程师"}]
    )
    gid = result["group"]["group_id"]
    from business.teamwork.messenger import send

    send(LEADER_ID, f"teamgroup:{gid}", "开工")

    # 统一会话列表含组（kind=project）与最后消息摘要
    rows = json.loads(_run(api.handle_list_sessions(_FakeRequest())).body)["projects"]
    grow = next(r for r in rows if r["project_id"] == gid)
    assert grow["kind"] == "project" and grow["last_msg"]["text"] == "开工"

    # 组消息载荷：messages + team + todos + stage 同响应（前端四视图共用）
    data = json.loads(
        _run(api.handle_get_messages(_FakeRequest(match_info={"sid": gid}))).body
    )
    assert data["kind"] == "group" and data["stage"].startswith("进行中")
    assert len(data["messages"]) == 1
    assert data["messages"][0]["sender_sid"] == LEADER_ID
    assert any(m["is_pm"] for m in data["team"])  # 组长（领导）在团队里

    # 改名
    resp = _run(api.handle_patch_session(_FakeRequest({"name": "新组名"}, {"sid": gid})))
    assert json.loads(resp.body)["ok"] is True
    assert tw_reg.lookup_group(gid)["name"] == "新组名"

    close = _run(api.handle_close(_FakeRequest({}, {"sid": gid})))
    assert json.loads(close.body)["ok"] is True
    assert tw_reg.lookup_group(gid)["status"] == "closed"


def test_service_enricher_fills_group_name(teamwork_env, monkeypatch):
    _Capture().install(monkeypatch)
    from business.teamwork import api
    from business.teamwork.groups import create_group

    result = create_group(
        "补全组", leader_id=LEADER_ID, members=[{"def": "coder-def", "name": "工程师"}]
    )
    gid = result["group"]["group_id"]
    rows = [{"project_id": gid}]
    api.enrich_service_rows(rows)
    assert rows[0]["group_name"] == "补全组"
