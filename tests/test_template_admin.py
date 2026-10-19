"""模板管理契约（2026-10-16）：两类壳分类位、persona 结构化闸、停用切换与客户可见性。"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from infrastructure.persistence import services_registry
from business.consulting import projects_registry


@pytest.fixture()
def tpl_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DIGITAL_LIFE_SERVICES_DB", str(tmp_path / "data" / "services.db"))
    monkeypatch.setenv("DIGITAL_LIFE_PROJECTS_DB", str(tmp_path / "data" / "projects.db"))
    monkeypatch.setenv("DIGITAL_LIFE_SERVICE_SESSIONS_DB",
                       str(tmp_path / "data" / "service_sessions.db"))
    monkeypatch.setattr("infrastructure.config.get_project_root", lambda: tmp_path)
    services_registry.reset_for_test()
    projects_registry.reset_for_test()
    cfg = tmp_path / "apps" / "consulting-partner-def" / "config"
    cfg.mkdir(parents=True)
    (cfg / "app.yaml").write_text(
        "runtime_kind: definition\ndisplay_name: x\n", encoding="utf-8")
    tpl = tmp_path / "business_templates"
    tpl.mkdir()
    from business.consulting import api_admin as adm, projects as bp

    monkeypatch.setattr(bp, "templates_dir", lambda: tpl)
    monkeypatch.setattr(adm, "_templates_dir", lambda: tpl)
    yield tpl
    services_registry.reset_for_test()
    projects_registry.reset_for_test()


def _put(tid, body):
    from business.consulting import api_admin as adm

    resp = asyncio.run(adm.handle_template_put(
        _Req(match_info={"template_id": tid}, body=body)))
    return resp.status, json.loads(resp.text)


def _put_structured(tid, template):
    from business.consulting import api_admin as adm

    resp = asyncio.run(adm.handle_template_put_structured(
        _Req(match_info={"template_id": tid}, body={"template": template})))
    return resp.status, json.loads(resp.text)


def _archive(tid, archived):
    from business.consulting import api_admin as adm

    resp = asyncio.run(adm.handle_template_archive(
        _Req(match_info={"template_id": tid}, body={"archived": archived})))
    return json.loads(resp.text)


def _list():
    from business.consulting import api_admin as adm

    resp = asyncio.run(adm.handle_templates_list(_Req()))
    return json.loads(resp.text)["templates"]


def _customer_templates():
    from business.consulting import api_customer as cr

    resp = asyncio.run(cr.handle_list_templates(_Req()))
    return json.loads(resp.text)["templates"]


class _Req:
    def __init__(self, match_info=None, body=None):
        self.match_info = match_info or {}
        self._body = body or {}

    async def json(self):
        return self._body


SOLO = {
    "name": "单测 · 顾问",
    "roles": [{"def": "consulting-partner-def", "name": "顾问", "pm": True}],
}
TEAM = {
    "name": "演示团队",
    "roles": [{"def": "consulting-partner-def", "name": "合伙人", "pm": True}],
    "initial_todos": [{"title": "S01 研究", "assign_role": "合伙人"}],
}


def test_templates_list_flags_kind_and_archived(tpl_env):
    _put("demo", {"raw": "name: 演示团队\nroles:\n  - def: x\n"})
    _put("solo_adv", {"raw": "name: 单聊顾问\nroles:\n  - def: x\n"})
    _put("solo_off", {"raw": "archived: true\nname: 停用的\nroles:\n  - def: x\n"})
    items = {t["id"]: t for t in _list()}
    assert items["demo"]["solo"] is False and items["demo"]["archived"] is False
    assert items["solo_adv"]["solo"] is True
    assert items["solo_off"]["archived"] is True


def test_structured_persona_single_role_and_no_todos(tpl_env):
    # 两个角色 → 拒；带任务骨架 → 拒；单角色干净保存 → 无 initial_todos
    bad_roles = dict(SOLO, roles=[*SOLO["roles"], {"def": "consulting-partner-def",
                                                   "name": "第二人"}])
    assert _put_structured("solo_a", bad_roles)[1]["ok"] is False
    bad_todos = dict(SOLO, initial_todos=[{"title": "干活"}])
    assert _put_structured("solo_a", bad_todos)[1]["ok"] is False
    status, d = _put_structured("solo_a", SOLO)
    assert status == 200 and d["ok"]
    assert "initial_todos" not in yaml_safe(d["raw"])
    parsed = yaml_safe(d["raw"])
    assert len(parsed["roles"]) == 1 and parsed["watchdog"] is False


def test_project_template_structured_keeps_todos(tpl_env):
    status, d = _put_structured("demo", TEAM)
    assert status == 200
    parsed = yaml_safe(d["raw"])
    assert parsed["initial_todos"] and parsed["initial_todos"][0]["title"] == "S01 研究"


def test_archive_toggle_and_customer_visibility(tpl_env, monkeypatch):
    from business.consulting import projects as bp

    monkeypatch.setattr(bp, "templates_dir", lambda: tpl_env)
    _put("solo_adv", {"raw": "name: 单聊顾问\nroles:\n  - def: x\n"})
    assert [t["id"] for t in _customer_templates()] == ["solo_adv"]

    assert _archive("solo_adv", True)["ok"]
    assert _customer_templates() == []  # 停用：客户菜单不可见
    rows = [t for t in _list() if t["id"] == "solo_adv"]
    assert rows and rows[0]["archived"] is True  # 服务台仍可见且带停用位

    assert _archive("solo_adv", False)["ok"]
    assert [t["id"] for t in _customer_templates()] == ["solo_adv"]


def test_structured_save_preserves_archived_flag(tpl_env):
    _put("solo_adv", {"raw": "name: 单聊顾问\nroles:\n  - def: x\n"})
    _archive("solo_adv", True)
    _put_structured("solo_adv", SOLO)  # 表单重建不得丢停用位
    assert _list()[0]["archived"] is True


def yaml_safe(raw):
    import yaml

    return yaml.safe_load(raw) or {}
