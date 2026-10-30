"""agent 类型复制（POST /api/admin/agent-defs 带 copy_from）——复制 = 带来源的创建。

整目录克隆来源 definition（人设/模型/Skill/头像），仅改 display_name、
置 active: false；头像引用（api: 端点 URL 含来源 id）改指新 id。
"""

from __future__ import annotations

import asyncio
import json

import pytest


class _FakeRequest:
    def __init__(self, body=None):
        self._body = body or {}

    async def json(self):
        return self._body


@pytest.fixture()
def defs_env(tmp_path, monkeypatch):
    monkeypatch.setattr("infrastructure.config.get_project_root", lambda: tmp_path)
    src = tmp_path / "apps" / "consulting-partner-def"
    (src / "config").mkdir(parents=True)
    (src / "config" / "app.yaml").write_text(
        "runtime_kind: definition\n"
        "active: true\n"
        "display_name: 咨询合伙人\n"
        "skills:\n"
        "  - report-writer\n",
        encoding="utf-8")
    (src / "persona").mkdir()
    (src / "persona" / "LIFE_PERSONA.md").write_text("# 咨询合伙人\n战略判断\n", encoding="utf-8")
    (src / "persona" / "agent.json").write_text(json.dumps(
        {"avatar": "api:/api/admin/agent-defs/consulting-partner-def/avatar"},
        ensure_ascii=False), encoding="utf-8")
    (src / "skills" / "report-writer").mkdir(parents=True)
    (src / "skills" / "report-writer" / "SKILL.md").write_text("# 报告\n", encoding="utf-8")
    # 来源 def 下挂着的实例运行时目录——复制必须排除（消息库是隐私面）
    (src / "services" / "svc-x").mkdir(parents=True)
    (src / "services" / "svc-x" / "data").mkdir()
    (src / "services" / "svc-x" / "data" / "messages.db").write_text("sqlite", encoding="utf-8")
    # 一个非 definition 的目录（服务实例）——复制来源安全闸的靶子
    inst = tmp_path / "apps" / "inst-abc"
    (inst / "config").mkdir(parents=True)
    (inst / "config" / "app.yaml").write_text("runtime_kind: service\n", encoding="utf-8")
    return tmp_path


def _create(body: dict) -> tuple[int, dict]:
    from business.consulting import agent_admin_routes as r

    resp = asyncio.run(r.handle_def_create(_FakeRequest(body)))
    return resp.status, json.loads(resp.text)


def test_copy_clones_tree_and_renames(defs_env):
    status, d = _create({"name": "咨询合伙人 副本", "copy_from": "consulting-partner-def"})
    assert status == 200 and d["ok"] and d["def_id"]
    dst = defs_env / "apps" / d["def_id"]
    assert (dst / "persona" / "LIFE_PERSONA.md").read_text(encoding="utf-8").startswith("# 咨询合伙人")
    assert (dst / "skills" / "report-writer" / "SKILL.md").is_file()
    import yaml

    cfg = yaml.safe_load((dst / "config" / "app.yaml").read_text(encoding="utf-8"))
    assert cfg["display_name"] == "咨询合伙人 副本"
    assert cfg["active"] is False  # 副本默认不激活
    assert cfg["skills"] == ["report-writer"]
    # 实例与运行时状态不入副本
    assert not (dst / "services").exists()
    assert not list(dst.rglob("*.db"))


def test_copy_rewrites_avatar_reference(defs_env):
    _, d = _create({"name": "合伙人二号", "copy_from": "consulting-partner-def"})
    meta = json.loads(
        (defs_env / "apps" / d["def_id"] / "persona" / "agent.json").read_text(encoding="utf-8"))
    assert meta["avatar"] == f"api:/api/admin/agent-defs/{d['def_id']}/avatar"
    # 来源不受影响
    src_meta = json.loads(
        (defs_env / "apps" / "consulting-partner-def" / "persona" / "agent.json")
        .read_text(encoding="utf-8"))
    assert src_meta["avatar"] == "api:/api/admin/agent-defs/consulting-partner-def/avatar"


def test_copy_name_defaults_to_source_plus_suffix(defs_env):
    status, d = _create({"copy_from": "consulting-partner-def"})
    assert status == 200
    import yaml

    cfg = yaml.safe_load(
        (defs_env / "apps" / d["def_id"] / "config" / "app.yaml").read_text(encoding="utf-8"))
    assert cfg["display_name"] == "咨询合伙人 副本"


def test_copy_rejects_missing_and_non_definition(defs_env):
    status, d = _create({"name": "x", "copy_from": "no-such-def"})
    assert status == 400 and "不存在" in d["error"]
    status, d = _create({"name": "x", "copy_from": "inst-abc"})
    assert status == 400 and "definition" in d["error"]
    assert not (defs_env / "apps" / "x").exists()  # 拒绝路径不落盘


def test_copy_id_collision_still_rejected(defs_env):
    status, d = _create({"name": "n", "def_id": "consulting-partner-def",
                         "copy_from": "consulting-partner-def"})
    assert status == 400 and "已存在" in d["error"]
