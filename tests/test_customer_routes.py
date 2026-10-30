"""客户会话 API 契约测试（刀 3 最小闭环）。"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from infrastructure.persistence import services_registry
from business.consulting import projects_registry


@pytest.fixture()
def customer_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DIGITAL_LIFE_SERVICES_DB", str(tmp_path / "data" / "services.db"))
    monkeypatch.setenv("DIGITAL_LIFE_PROJECTS_DB", str(tmp_path / "data" / "projects.db"))
    monkeypatch.setenv("DIGITAL_LIFE_SERVICE_SESSIONS_DB", str(tmp_path / "data" / "service_sessions.db"))
    monkeypatch.setattr("infrastructure.config.get_project_root", lambda: tmp_path)
    from tests.test_services_runtime import _restore_default_runtime_hooks

    _restore_default_runtime_hooks()
    services_registry.reset_for_test()
    projects_registry.reset_for_test()
    # 演示 defs
    for d in ("consulting-partner-def", "consulting-researcher-def"):
        cfg = tmp_path / "apps" / d / "config"
        cfg.mkdir(parents=True)
        (cfg / "app.yaml").write_text(
            "runtime_kind: definition\ndisplay_name: x\n", encoding="utf-8"
        )
    tpl = tmp_path / "business_templates"
    tpl.mkdir(parents=True)
    from business.consulting import projects as _bp
    monkeypatch.setattr(_bp, "templates_dir", lambda: tpl)
    (tpl / "demo.yaml").write_text(
        "name: 演示\nroles:\n  - def: consulting-partner-def\n    name: 合伙人\n    pm: true\n"
        "  - def: consulting-researcher-def\n    name: 研究员\n",
        encoding="utf-8",
    )
    yield tmp_path
    services_registry.reset_for_test()
    from tests.test_services_runtime import _restore_default_runtime_hooks as _r

    _r()
    os.environ.pop("DIGITAL_LIFE_INSTANCE_ID", None)


class _FakeRequest:
    def __init__(self, body=None, match_info=None, query=None, cookies=None):
        self._body = body or {}
        self.match_info = match_info or {}
        self.query = query or {}
        self.cookies = cookies or {}

    async def json(self):
        return self._body


def _start(body):
    from business.consulting import api_customer as cr

    resp = asyncio.run(cr.handle_start_session(_FakeRequest(body)))
    return json.loads(resp.text)


def _get_messages(customer_id, project_id=""):
    from business.consulting import api_customer as cr

    query = {"project_id": project_id} if project_id else {}
    resp = asyncio.run(cr.handle_get_messages(
        _FakeRequest(match_info={"customer_id": customer_id}, query=query)))
    return resp.status, json.loads(resp.text)


def _drain_pm_inbox(project_id) -> int:
    """把 PM 信箱里未消费的事件全部标记已消费（模拟 PM 醒来读完客户发言）。

    suspend_project 守卫：客户有未读发言时拒绝收队——测试里 _start 刚发的
    客户消息还躺在信箱，要先"读掉"才能走挂起/完成的后续断言。返回标记条数。
    """
    import sqlite3

    from infrastructure.config import resolve_runtime_dir

    pm = (projects_registry.lookup_project(project_id) or {}).get("pm_id") or ""
    db = resolve_runtime_dir(pm) / "data" / "state.db"
    if not db.exists():
        return 0
    conn = sqlite3.connect(str(db))
    try:
        cur = conn.execute(
            "UPDATE events SET consumed_at = '2026-01-01T00:00:00+08:00'"
            " WHERE consumed_at IS NULL"
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def test_start_session_creates_project_and_routes(customer_env):
    d = _start({"customer_name": "李总", "text": "想做咨询", "template": "demo"})
    assert d["ok"] and d["created"] is True
    cid, pid = d["customer_id"], d["project_id"]

    from infrastructure.persistence import services_registry as sr

    project = projects_registry.lookup_project(pid)
    assert project["customer_id"] == cid
    assert project["pm_id"]

    # 幂等：同 customer_id 再发 → 不建新项目，直接投 PM
    d2 = _start({"customer_name": "李总", "text": "补充材料", "customer_id": cid,
                 "template": "demo"})
    assert d2["ok"] and d2["created"] is False and d2["project_id"] == pid


def test_get_messages_replay(customer_env):
    d = _start({"customer_name": "王总", "text": "启动", "template": "demo"})
    cid = d["customer_id"]
    pm = d["project_id"]

    from infrastructure.persistence import services_registry as sr

    pm_id = projects_registry.lookup_project(pm)["pm_id"]
    from business.consulting.social import _service_context

    with _service_context(pm_id):
        from domain.messages import record_outbound

        record_outbound(
            chat_id=f"customer:{cid}", self_display_name="咨询合伙人",
            self_instance_id=pm_id, text="已启动研究，稍后汇报。",
            source="customer",
        )

    from business.consulting import api_customer as cr

    status, data = _get_messages(cid)
    assert status == 200 and data["ok"]
    texts = [(m["from"], m["text"]) for m in data["messages"]]
    assert ("customer", "启动") in texts
    assert ("consultant", "已启动研究，稍后汇报。") in texts


def test_get_messages_unknown_session(customer_env):
    from business.consulting import api_customer as cr

    status, _ = _get_messages("cust-none")
    assert status == 404


def test_get_messages_archived_project_replay(customer_env):
    """归档会话也能回放（客户点历史咨询记录右侧不空白）。

    回归：handle_get_messages 旧实现只认 active 项目且无视 ?project_id=，
    客户批量结束后点任何历史记录都 404 → 右侧空白。
    """
    d = _start({"customer_name": "钱总", "text": "启动", "template": "demo"})
    cid = d["customer_id"]
    pid = d["project_id"]

    from business.consulting.governance import close_project

    assert close_project(pid, reason="customer_closed")["ok"]

    from business.consulting import api_customer as cr

    resp = asyncio.run(cr.handle_get_messages(_FakeRequest(
        match_info={"customer_id": cid}, query={"project_id": pid})))
    data = json.loads(resp.text)
    assert resp.status == 200 and data["ok"]
    texts = [(m["from"], m["text"]) for m in data["messages"]]
    assert ("customer", "启动") in texts

    # 不带 project_id 也回退到最近一个项目（看历史）
    resp2 = asyncio.run(cr.handle_get_messages(_FakeRequest(
        match_info={"customer_id": cid})))
    assert resp2.status == 200 and json.loads(resp2.text)["ok"]


def test_files_root_shares_and_personal_zones(customer_env):
    """交付物浏览根目录 = 📦 shared + 每位成员个人区；越界拒绝。"""
    import asyncio as _aio

    from business.consulting import api_customer as cr

    d = _start({"customer_name": "孙总", "text": "启动", "template": "demo"})
    cid = d["customer_id"]
    pid = d["project_id"]

    resp = _aio.run(cr.handle_files(_FakeRequest(match_info={"customer_id": cid})))
    data = json.loads(resp.text)
    assert resp.status == 200 and data["ok"]
    names = [e["name"] for e in data["entries"]]
    assert any("交付物" in n for n in names)
    assert sum(1 for n in names if "工作区" in n) >= 2  # 至少 PM+研究员两个人区
    shared_rel = next(e["rel"] for e in data["entries"] if "交付物" in e["name"])
    member_rel = next(e["rel"] for e in data["entries"] if "工作区" in e["name"])
    assert shared_rel == "shared" and member_rel.startswith("members/")

    # 成员个人区可浏览（目录已随建项目生成）
    resp2 = _aio.run(cr.handle_files(_FakeRequest(
        match_info={"customer_id": cid}, query={"project_id": pid, "path": member_rel})))
    assert resp2.status == 200 and json.loads(resp2.text)["kind"] == "dir"

    # 越界（跳出 shared/members 作用域）拒绝
    resp3 = _aio.run(cr.handle_files(_FakeRequest(
        match_info={"customer_id": cid}, query={"path": "../../etc"})))
    assert resp3.status == 403


def test_suspended_customer_message_resumes_not_duplicates(customer_env):
    """挂起项目的客户回来 → 落回原项目并自动复活，不误建新项目。"""
    d = _start({"customer_name": "郑总", "text": "启动", "template": "demo"})
    cid, pid = d["customer_id"], d["project_id"]

    from business.consulting.governance import suspend_project

    _drain_pm_inbox(pid)
    assert suspend_project(pid, reason="waiting_customer")["ok"]

    d2 = _start({"customer_name": "郑总", "customer_id": cid, "text": "我回来了"})
    assert d2["created"] is False and d2["project_id"] == pid

    from business.consulting import projects_registry as pr

    assert pr.lookup_project(pid)["status"] == "active"  # 自动复活
    assert len([p for p in pr.list_projects() if p.get("customer_id") == cid]) == 1
    # 复活发生在消息落信箱之后：这条"我回来了"已在 PM 信箱等待消费
    from business.consulting.governance import pending_customer_messages

    assert pending_customer_messages(pr.lookup_project(pid)["pm_id"]) == 1


def test_suspend_refused_while_customer_message_unread(customer_env):
    """自动恢复的另一端：客户刚说话还没人读 → 不能收队（2026-10-20 事故）。

    看门狗催 PM"无事可做就挂起"的同一秒客户正式需求入队；入口复活在投递前
    判断、看到的还是 active，消息随后被塞进挂起后的 wake，PM 全部项目工具报
    "未挂项目"却没有钥匙。守卫让挂起在"客户有未读发言"时直接不成立。
    """
    d = _start({"customer_name": "刘明君", "text": "正式需求来了", "template": "demo"})
    pid = d["project_id"]

    from business.consulting import projects_registry as pr
    from business.consulting.governance import (
        complete_project, pending_customer_messages, suspend_project,
    )

    pm = pr.lookup_project(pid)["pm_id"]
    assert pending_customer_messages(pm) == 1

    r = suspend_project(pid, reason="watchdog_said_nothing_to_do", actor=pm)
    assert r["ok"] is False and "1 条新发言" in r["error"]
    assert pr.lookup_project(pid)["status"] == "active"  # 没被挂起
    assert pending_customer_messages(pm) == 1            # 消息也没丢

    # 完成 = 挂起的运行行为，同样受守卫
    r2 = complete_project(pid, reason="done", actor=pm)
    assert r2["ok"] is False and "新发言" in r2["error"]

    # PM 读完客户发言后才可收队
    assert _drain_pm_inbox(pid) >= 1
    assert suspend_project(pid, reason="waiting_customer", actor=pm)["ok"]
    assert pr.lookup_project(pid)["status"] == "suspended"


def test_suspended_routed_even_when_other_customers_have_active_projects(customer_env):
    """若羽臣事故回归（2026-10-10 prj-c444f906ea3a）：别的客户有 active 项目时，
    挂起项目的客户回复仍须落回原项目。

    旧写法 `list_projects("active") or list_projects("suspended")` 只要系统里
    任何客户有 active 项目就短路跳过挂起名单——挂起项目的客户在原对话框回复
    （"abc是什么"）被误判为新客户，开了全新咨询。
    """
    # 客户 A：建项目 → 挂起（等客户选方向）
    a = _start({"customer_name": "若总", "text": "启动研究", "template": "demo"})
    from business.consulting.governance import suspend_project

    _drain_pm_inbox(a["project_id"])
    assert suspend_project(a["project_id"], reason="waiting_customer")["ok"]

    # 客户 B：建一个保持 active 的项目（旧 bug 的短路触发器）
    _start({"customer_name": "别家客户", "text": "我也要咨询", "template": "demo"})

    # 客户 A 回原对话框继续 → 必须落到原项目，不得新建
    d = _start({"customer_name": "若总", "customer_id": a["customer_id"],
                "text": "abc是什么", "template": "demo"})
    assert d["created"] is False and d["project_id"] == a["project_id"]

    from business.consulting import projects_registry as pr

    assert len([p for p in pr.list_projects()
                if p.get("customer_id") == a["customer_id"]]) == 1


def test_completed_project_reopens_on_customer_message(customer_env):
    """完成项目的客户回复 → 命中簿路由并重开回 active（完成行为=挂起）。"""
    d = _start({"customer_name": "周总", "text": "启动", "template": "demo"})
    cid, pid = d["customer_id"], d["project_id"]

    from business.consulting import projects_registry as pr
    from business.consulting.governance import complete_project

    _drain_pm_inbox(pid)
    assert complete_project(pid, reason="交付验收通说")["ok"]

    d2 = _start({"customer_name": "周总", "customer_id": cid, "text": "还有一个问题",
                 "template": "demo"})
    assert d2["created"] is False and d2["project_id"] == pid
    assert pr.lookup_project(pid)["status"] == "active"  # 自动重开


def test_archived_project_still_routes_via_contact_ledger(customer_env):
    """归档不摘除联系人簿登记——路由与项目状态无关（传输归传输，治理归治理）。

    客户在归档项目的对话框回复：消息仍投给建线时登记的 PM（团队如何应对
    是 agent 自治/治理层的事），绝不因状态改道建新。
    """
    d = _start({"customer_name": "陈总", "text": "启动", "template": "demo"})
    cid, pid = d["customer_id"], d["project_id"]

    from business.consulting import projects_registry as pr

    pr.update_project_fields(pid, status="archived")

    d2 = _start({"customer_name": "陈总", "customer_id": cid, "text": "还在吗",
                 "template": "demo"})
    assert d2["created"] is False and d2["project_id"] == pid


# ── 访问码会话（yl_sid cookie 替代 basic auth） ──────────────────────────


def test_login_sets_session_cookie(customer_env, monkeypatch):
    import infrastructure.ai.config as ai_config
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    monkeypatch.setattr(ai_config, "load_runtime_config", lambda: {"testers": {"satest": {}}})
    from business.consulting import api_customer as cr

    app = web.Application()
    app.add_subapp("/api/customer/", cr._ROUTER)

    async def _run():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            r = await client.post("/api/customer/login", json={"code": "satest"})
            data = await r.json()
            assert data["ok"] and data["customer_id"]
            raw = r.headers.get("Set-Cookie", "")
            assert "yl_sid=yls-" in raw and "HttpOnly" in raw

            # cookie 自动携带：verify 通过
            v = await client.get("/api/customer/verify")
            assert v.status == 200 and (await v.json())["customer_id"] == data["customer_id"]

            # 无效码：400 且不种会话
            bad = await client.post("/api/customer/login", json={"code": "nope"})
            assert bad.status == 400

            o = await client.post("/api/customer/logout")
            assert (await o.json())["ok"]
            v2 = await client.get("/api/customer/verify")
            assert v2.status == 401
        finally:
            await client.close()

    asyncio.run(_run())


def test_verify_logout_roundtrip(customer_env):
    from business.consulting import api_customer as cr

    token = projects_registry.create_login_session("satest", "cust-x")

    ok = asyncio.run(cr.handle_verify(_FakeRequest(cookies={"yl_sid": token})))
    assert ok.status == 200 and json.loads(ok.text)["customer_id"] == "cust-x"

    fake = asyncio.run(cr.handle_verify(_FakeRequest(cookies={"yl_sid": "yls-forged"})))
    assert fake.status == 401
    anon = asyncio.run(cr.handle_verify(_FakeRequest(cookies={})))
    assert anon.status == 401
    # 401 不带 WWW-Authenticate，浏览器才不会弹原生 basic auth 框
    assert "WWW-Authenticate" not in anon.headers

    out = asyncio.run(cr.handle_logout(_FakeRequest(cookies={"yl_sid": token})))
    assert json.loads(out.text)["ok"]
    assert projects_registry.get_login_session(token) is None


def test_login_session_expiry(customer_env):
    token = projects_registry.create_login_session("satest", "cust-y", ttl_hours=-1)
    assert projects_registry.get_login_session(token) is None
    # 空容错
    assert projects_registry.get_login_session("") is None
    assert projects_registry.create_login_session("satest", "") == ""


# ── 模版列表 + 单人测试模版 ──────────────────────────────────────────────


def test_templates_endpoint_lists_solo_series(customer_env, monkeypatch):
    from business.consulting import api_customer as cr
    from business.consulting import projects as bp
    from pathlib import Path

    tpl = Path(str(customer_env)) / "business_templates"
    (tpl / "solo_researcher.yaml").write_text(
        "name: 单测 · 研究员\nwatchdog: false\ndescription: 单人直聊\n"
        "roles:\n  - def: consulting-researcher-def\n    name: 研究员\n    pm: true\n",
        encoding="utf-8",
    )
    (tpl / "demo.yaml").write_text(
        "name: 演示\nroles:\n  - def: consulting-partner-def\n    name: 合伙人\n    pm: true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bp, "templates_dir", lambda: tpl)
    resp = asyncio.run(cr.handle_list_templates(_FakeRequest()))
    d = json.loads(resp.text)
    ids = [t["id"] for t in d["templates"]]
    assert "demo" in ids and "solo_researcher" in ids
    assert ids.index("solo_researcher") < ids.index("demo")  # 单测系列排在普通模版前
    solo = [t for t in d["templates"] if t["solo"]]
    assert [t["id"] for t in solo] == ["solo_researcher"]
    assert solo[0]["roles"] == 1


def test_solo_persona_creates_service_session(customer_env, monkeypatch):
    # 单聊壳（2026-10-15 定稿）：solo_ 前缀不再是"单人项目"——拉起无项目服务
    # + 绑客户通道，无群、无任务板、无项目行（与项目壳平行）
    from pathlib import Path
    from business.consulting import api_customer as cr, projects as bp
    from business.consulting import service_sessions

    tpl = Path(str(customer_env)) / "business_templates"
    (tpl / "solo_partner.yaml").write_text(
        "name: 单测 · 合伙人\nwatchdog: false\n"
        "roles:\n  - def: consulting-partner-def\n    name: 合伙人\n    pm: true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bp, "templates_dir", lambda: tpl)
    d = _start({"customer_name": "SA", "text": "逐个测你", "template": "solo_partner"})
    assert d["ok"] and d["created"] is True
    assert d["project_id"].startswith("sess-")

    # 不存在项目行——单聊壳不是项目
    assert projects_registry.lookup_project(d["project_id"]) is None
    sess = service_sessions.lookup(d["project_id"])
    assert sess and sess["customer_id"] == d["customer_id"]
    assert sess["status"] == "active"
    from infrastructure.persistence import services_registry as sr

    svc = sr.lookup_service(sess["service_id"])
    assert svc is not None and not svc.get("project_id")  # 未挂项目

    # 联系人簿里只有客户窗口，没有群
    from business.consulting.social import _service_context, customer_chat_id

    with _service_context(sess["service_id"]):
        from domain.contacts import store as contacts

        assert contacts.get_chat(customer_chat_id(d["customer_id"]))
        assert not contacts.get_chat(f"svcgroup:{d['project_id']}")


def test_solo_messages_payload_same_shape(customer_env, monkeypatch):
    from pathlib import Path
    from business.consulting import projects as bp

    tpl = Path(str(customer_env)) / "business_templates"
    (tpl / "solo_partner.yaml").write_text(
        "name: 单测 · 合伙人\n"
        "roles:\n  - def: consulting-partner-def\n    name: 合伙人\n    pm: true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bp, "templates_dir", lambda: tpl)
    d = _start({"customer_name": "SB", "text": "你好", "template": "solo_partner"})
    cid, handle = d["customer_id"], d["project_id"]

    status, data = _get_messages(cid, handle)
    assert status == 200 and data["ok"]
    assert data["project_id"] == handle
    assert len(data["team"]) == 1 and data["team"][0]["is_pm"] is False
    assert data["todos"] == [] and data["todo_events"] == []
    assert [m["from"] for m in data["messages"]] == ["customer"]
    assert data["stage"]


def test_solo_continue_and_close(customer_env, monkeypatch):
    from pathlib import Path
    from business.consulting import api_customer as cr, projects as bp
    from business.consulting import service_sessions

    tpl = Path(str(customer_env)) / "business_templates"
    (tpl / "solo_partner.yaml").write_text(
        "name: 单测 · 合伙人\n"
        "roles:\n  - def: consulting-partner-def\n    name: 合伙人\n    pm: true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bp, "templates_dir", lambda: tpl)
    d = _start({"customer_name": "SC", "text": "开工", "template": "solo_partner"})
    cid, handle = d["customer_id"], d["project_id"]

    # 带 handle 续聊 → 不建新壳，直投同一服务
    d2 = _start({"customer_name": "SC", "text": "继续", "customer_id": cid,
                 "project_id": handle})
    assert d2["ok"] and d2["created"] is False and d2["project_id"] == handle
    assert len(service_sessions.list_by_customer(cid)) == 1

    # 收工 → suspended（可复活，不归档服务）
    resp = asyncio.run(cr.handle_close_session(_FakeRequest(
        match_info={"customer_id": cid}, body={"project_id": handle})))
    assert json.loads(resp.text)["ok"]
    assert service_sessions.lookup(handle)["status"] == "suspended"

    # 客户回来即接续：再发消息 → 壳记录翻回 active
    d3 = _start({"customer_name": "SC", "text": "我又来了", "customer_id": cid,
                 "project_id": handle})
    assert d3["ok"] and d3["created"] is False
    assert service_sessions.lookup(handle)["status"] == "active"


def test_contact_ledger_routes_to_solo_session(customer_env, monkeypatch):
    # 默认路由（无 project_id、无 new）：簿命中单聊服务 → 返回壳 handle
    from pathlib import Path
    from business.consulting import projects as bp

    tpl = Path(str(customer_env)) / "business_templates"
    (tpl / "solo_partner.yaml").write_text(
        "name: 单测 · 合伙人\n"
        "roles:\n  - def: consulting-partner-def\n    name: 合伙人\n    pm: true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bp, "templates_dir", lambda: tpl)
    d = _start({"customer_name": "SD", "text": "开场", "template": "solo_partner"})
    cid, handle = d["customer_id"], d["project_id"]

    d2 = _start({"customer_name": "SD", "text": "又来了", "customer_id": cid})
    assert d2["ok"] and d2["created"] is False
    assert d2["project_id"] == handle


# ── 账号→多项目（2026-10-10 定稿：new:true 开新线，project_id 定向续聊） ──


def test_new_flag_creates_second_project_same_account(customer_env):
    d1 = _start({"customer_name": "李总", "text": "第一个", "template": "demo"})
    cid = d1["customer_id"]
    d2 = _start({"customer_name": "李总", "customer_id": cid, "text": "开第二个",
                 "template": "demo", "new": True})
    assert d2["ok"] and d2["created"] is True
    assert d2["customer_id"] == cid and d2["project_id"] != d1["project_id"]
    # 联系人簿解析迁到最新线
    from business.consulting.social import resolve_customer_channel_service
    _, latest_pid = resolve_customer_channel_service(cid)
    assert latest_pid == d2["project_id"]


def test_project_id_continues_old_thread(customer_env):
    from business.consulting import api_customer as cr

    d1 = _start({"customer_name": "李总", "text": "第一个", "template": "demo"})
    cid, old_pid = d1["customer_id"], d1["project_id"]
    d2 = _start({"customer_name": "李总", "customer_id": cid, "text": "第二个",
                 "template": "demo", "new": True})
    new_pid = d2["project_id"]

    # 续聊旧线：带 project_id 直投旧项目 PM
    d3 = _start({"customer_name": "李总", "customer_id": cid,
                 "text": "回旧线说一句", "project_id": old_pid})
    assert d3["ok"] and d3["created"] is False and d3["project_id"] == old_pid

    resp = asyncio.run(cr.handle_get_messages(
        _FakeRequest(match_info={"customer_id": cid}, query={"project_id": old_pid})))
    old_texts = [m.get("text") for m in json.loads(resp.text)["messages"]]
    assert "回旧线说一句" in old_texts

    resp2 = asyncio.run(cr.handle_get_messages(
        _FakeRequest(match_info={"customer_id": cid}, query={"project_id": new_pid})))
    new_texts = [m.get("text") for m in json.loads(resp2.text)["messages"]]
    assert "回旧线说一句" not in new_texts


def test_new_flag_without_customer_id_still_creates_fresh(customer_env):
    d = _start({"customer_name": "访客", "text": "初次", "template": "demo", "new": True})
    assert d["ok"] and d["created"] is True and d["customer_id"]


class _FakeField:
    def __init__(self, filename, content, ctype):
        self.name = "file"
        self.filename = filename
        self.headers = {"Content-Type": ctype}
        self._content = content.encode("utf-8") if isinstance(content, str) else content
        self._pos = 0

    async def read_chunk(self, n=65536):
        chunk = self._content[self._pos:self._pos + n]
        self._pos += n
        return chunk


class _FakeMultipartReader:
    def __init__(self, field):
        self._field = field
        self._done = False

    async def next(self):
        if self._done:
            return None
        self._done = True
        return self._field


class _AttachRequest:
    def __init__(self, cid, filename, content, ctype="text/plain"):
        self.match_info = {"customer_id": cid}
        self.query = {}
        self._reader = _FakeMultipartReader(_FakeField(filename, content, ctype))

    async def multipart(self):
        return self._reader


def test_attachment_becomes_file_address_message(customer_env):
    """附件以文件地址形态成为正常消息体（2026-10-13 zhp 定案）。

    回归点：row_text 为空时 record_inbound 落库被守卫拒绝 → inserted=False
    → customer_message_to_pm 直接 return 0，事件根本不发给 PM（附件躺盘上
    无人知晓）。现在行文本=地址，落库 + 唤醒一条不少。
    """
    d = _start({"customer_name": "钱总", "text": "启动", "template": "demo"})
    cid, pid = d["customer_id"], d["project_id"]

    from business.consulting import api_customer as cr

    resp = asyncio.run(cr.handle_attachment(
        _AttachRequest(cid, "调研提纲.txt", "客户调研提纲内容……")))
    data = json.loads(resp.text)
    assert resp.status == 200 and data["ok"]
    assert data["event_id"] > 0  # 回归核心：事件真的发出去了

    # 消息行=地址文本（模型后续 wake 的历史里持续可见）+ 附件字段
    _, mdata = _get_messages(cid)
    row = next(m for m in mdata["messages"] if (m.get("text") or "").startswith("[附件]"))
    assert "需求附件/" in row["text"] and "调研提纲.txt" in row["text"]
    assert row.get("attachments")

    # 文件本体落在项目 shared/需求附件/
    att_dir = customer_env / "projects" / pid / "shared" / "需求附件"
    assert any("调研提纲.txt" in p.name for p in att_dir.iterdir())


def _minimal_pdf(text: str = "Hello PDF extraction", pages: int = 1) -> bytes:
    """手工构造的文本 PDF（xref 偏移精确），供抽取链端到端测试；可多页。"""
    kids = " ".join(f"{3 + i} 0 R" for i in range(pages))
    objs = {1: b"<< /Type /Catalog /Pages 2 0 R >>",
            2: f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode()}
    for i in range(pages):
        stream = f"BT /F1 18 Tf 72 720 Td ({text} p{i + 1}) Tj ET".encode()
        objs[3 + i] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                       f"/Contents {3 + pages + i} 0 R /Resources "
                       f"<< /Font << /F1 {3 + 2 * pages} 0 R >> >> >>").encode()
        objs[3 + pages + i] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
    objs[3 + 2 * pages] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for i in sorted(objs):
        offsets[i] = len(out)
        out += f"{i} 0 obj\n".encode() + objs[i] + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {3 + 2 * pages + 1}\n0000000000 65535 f \n".encode()
    for i in sorted(objs):
        out += f"{offsets[i]:010d} 00000 n \n".encode()
    out += (b"trailer\n" + f"<< /Size {3 + 2 * pages + 1} /Root 1 0 R >>\n".encode()
            + b"startxref\n" + str(xref_pos).encode() + b"\n%%EOF\n")
    return bytes(out)


def test_pdf_attachment_extract_sidecar(customer_env):
    """PDF 上传 → .extract.md 旁路落盘：pdfminer 为主力引擎 + 分页标记保留。

    回归背景（2026-10-20 科拓招股书事故）：pypdf 对港交所系中文 PDF 的
    CMap 解析必产乱码，旁路一度把乱码文本喂给 agent。"""
    d = _start({"customer_name": "钱总", "text": "启动", "template": "demo"})
    cid, pid = d["customer_id"], d["project_id"]
    from business.consulting import api_customer as cr

    resp = asyncio.run(cr.handle_attachment(_AttachRequest(
        cid, "招股书.pdf", _minimal_pdf(), ctype="application/pdf")))
    assert resp.status == 200 and json.loads(resp.text)["ok"]

    att_dir = customer_env / "projects" / pid / "shared" / "需求附件"
    side = next(p for p in att_dir.iterdir() if p.name.endswith(".extract.md"))
    body = side.read_text(encoding="utf-8")
    assert "## 第 1 页" in body          # 分页标记保留（来源表引用页码）
    assert "Hello PDF extraction" in body  # 文本完整、非乱码

    # 消息行文本带上抽取件路径（agent 无需翻目录即可知）
    _, mdata = _get_messages(cid)
    row = next(m for m in mdata["messages"] if (m.get("text") or "").startswith("[附件]"))
    assert ".extract.md" in row["text"]


def test_pdf_extract_falls_back_to_pypdf(customer_env, monkeypatch):
    """pdfminer 异常时落 pypdf 兜底，抽取旁路不因主力引擎失败而失效。"""
    d = _start({"customer_name": "钱总", "text": "启动", "template": "demo"})
    cid, pid = d["customer_id"], d["project_id"]

    def _boom(*a, **kw):
        raise RuntimeError("pdfminer down")

    monkeypatch.setattr("pdfminer.high_level.extract_text", _boom)
    from business.consulting import api_customer as cr

    resp = asyncio.run(cr.handle_attachment(_AttachRequest(
        cid, "兜底.pdf", _minimal_pdf("fallback works"), ctype="application/pdf")))
    assert resp.status == 200 and json.loads(resp.text)["ok"]
    att_dir = customer_env / "projects" / pid / "shared" / "需求附件"
    side = next(p for p in att_dir.iterdir() if p.name.endswith(".extract.md"))
    assert "fallback works" in side.read_text(encoding="utf-8")


def test_pdf_extract_shards_by_pages(customer_env):
    """>100 页 PDF 按页分片：首片契约名不变、页码全局连续、消息列全路径。

    回归背景：旧实现整篇截断 200K 字符，招股书级长文只有前 1/3 可读。"""
    d = _start({"customer_name": "钱总", "text": "启动", "template": "demo"})
    cid, pid = d["customer_id"], d["project_id"]
    from business.consulting import api_customer as cr

    resp = asyncio.run(cr.handle_attachment(_AttachRequest(
        cid, "长报告.pdf", _minimal_pdf("dense page", pages=101),
        ctype="application/pdf")))
    assert resp.status == 200 and json.loads(resp.text)["ok"]

    att_dir = customer_env / "projects" / pid / "shared" / "需求附件"
    sides = sorted(p.name for p in att_dir.iterdir() if ".extract" in p.name)
    assert len(sides) == 2
    assert sides[0].endswith(".extract.2.md") and sides[1].endswith(".extract.md")
    first = (att_dir / sides[1]).read_text(encoding="utf-8")
    second = (att_dir / sides[0]).read_text(encoding="utf-8")
    assert "## 第 1 页" in first and "dense page p1" in first
    assert "## 第 100 页" in first           # 首片整 100 页
    assert "## 第 101 页" in second           # 页码连续，不从 1 重计
    assert "dense page p101" in second

    # 消息行文本列出全部片路径（模型无需翻目录）
    _, mdata = _get_messages(cid)
    row = next(m for m in mdata["messages"] if (m.get("text") or "").startswith("[附件]"))
    assert ".extract.md" in row["text"] and ".extract.2.md" in row["text"]
