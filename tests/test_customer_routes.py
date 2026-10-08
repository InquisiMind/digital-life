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
    tpl = tmp_path / "config" / "project_templates"
    tpl.mkdir(parents=True)
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
    def __init__(self, body=None, match_info=None, query=None):
        self._body = body or {}
        self.match_info = match_info or {}
        self.query = query or {}

    async def json(self):
        return self._body


def _start(body):
    from business.consulting import api_customer as cr

    resp = asyncio.run(cr.handle_start_session(_FakeRequest(body)))
    return json.loads(resp.text)


def _get_messages(customer_id):
    from business.consulting import api_customer as cr

    resp = asyncio.run(cr.handle_get_messages(_FakeRequest(match_info={"customer_id": customer_id})))
    return resp.status, json.loads(resp.text)


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
