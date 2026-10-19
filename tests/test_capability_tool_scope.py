"""自注册工具（register_tool / load_registered_tools）的可见范围。

规则（2026-10-16 zhp）：作者写的作者用；shared 层"全实例可选启用"按字面
落实——注册者注册时即授予，其他实例/服务显式授予才可见，不漫灌。
personal/project 由 manifest 文件布局天然限定，不另设闸。
"""

import importlib
import json

import pytest

from domain import tool_visibility as tv
from infrastructure.persistence import tool_visibility as store


@pytest.fixture()
def cap_env(tmp_path, monkeypatch):
    """capability 根目录与授予库都指到 tmp；清理测试期注册进真实 registry 的 app_* 工具。"""
    monkeypatch.setenv("DIGITAL_LIFE_TOOL_VISIBILITY_DB", str(tmp_path / "tv.db"))
    store.reset_for_test()
    tv._wake_grants.clear()
    monkeypatch.setattr("domain.capability.lifecycle.project_root", lambda: tmp_path)
    monkeypatch.setattr("domain.capability.store.project_root", lambda: tmp_path)
    reg = importlib.import_module("interfaces.tools.registry").registry
    before = set(reg.get_all_tool_names(include_retired=True))
    yield tmp_path
    for name in set(reg.get_all_tool_names(include_retired=True)) - before:
        reg.deregister(name)
    store.reset_for_test()
    tv._wake_grants.clear()


def _write_tool(root, rel_dir, name, body="return 'ok'"):
    d = root / rel_dir
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.py").write_text(
        "from typing import Any, Dict\n\n\ndef handler(args: Dict[str, Any], **kwargs: Any) -> str:\n"
        f"    {body}\n",
        encoding="utf-8",
    )
    return d / f"{name}.py"


def test_hot_load_writes_capability_scope_metadata(cap_env):
    from domain.capability.lifecycle import _hot_load_tool

    path = _write_tool(cap_env, "shared/tools", "probe_meta")
    r = _hot_load_tool(path, "app_probe_meta", "d", {"type": "object", "properties": {}}, "🔧", "shared")
    assert r["ok"] is True
    reg = importlib.import_module("interfaces.tools.registry").registry
    assert reg.get_tool_metadata("app_probe_meta").get("capability_scope") == "shared"
    assert reg.get_schema("app_probe_meta") is not None


def test_register_tool_shared_grants_author_only(cap_env):
    from domain.capability import lifecycle

    r = lifecycle.register_tool(
        name="probe_shared",
        description="探针",
        parameters={"type": "object", "properties": {}},
        code="return 'hi'",
        scope="shared",
        instance_id="inst-author",
    )
    assert r["ok"] is True and r["tool_name"] == "app_probe_shared"
    assert tv.durable_grants("inst-author") == {"shared_tool:probe_shared": ["app_probe_shared"]}
    assert tv.durable_grants("inst-other") == {}

    # 装配：作者可见（授予）、他人默认面排除
    reg = importlib.import_module("interfaces.tools.registry").registry
    base = ["plain", "app_probe_shared"]
    assert "app_probe_shared" in tv.assemble(base=base, runtime_id="inst-author")
    assert "app_probe_shared" not in tv.assemble(base=base, runtime_id="inst-other")
    assert reg.get_tool_metadata("app_probe_shared")["capability_scope"] == "shared"


def test_register_tool_personal_no_grant_needed_stays_visible(cap_env):
    """personal 由布局限定（只装进作者进程），不打 shared 旗标、默认面保留。"""
    from domain.capability import lifecycle

    r = lifecycle.register_tool(
        name="probe_personal",
        description="探针",
        parameters={"type": "object", "properties": {}},
        code="return 'hi'",
        scope="personal",
        instance_id="inst-author",
    )
    assert r["ok"] is True
    assert tv.durable_grants("inst-author") == {}
    assert "app_probe_personal" in tv.assemble(base=["app_probe_personal"], runtime_id="inst-author")


def test_load_registered_tools_annotates_shared_scope_from_layout(cap_env):
    """旧 manifest 条目没有 scope 字段——读侧按所在 manifest 标注，shared 照样被闸。"""
    from domain.capability.lifecycle import load_registered_tools

    _write_tool(cap_env, "shared/tools", "legacy_fetch")
    manifest = cap_env / "shared" / "tools" / "tools_manifest.json"
    manifest.write_text(json.dumps({"items": [{
        "name": "legacy_fetch",
        "file": "shared/tools/legacy_fetch.py",
        "description": "旧条目，无 scope 字段",
        "parameters": {"type": "object", "properties": {}},
    }]}, ensure_ascii=False), encoding="utf-8")

    r = load_registered_tools("inst-x")
    assert r["loaded"] == 1
    reg = importlib.import_module("interfaces.tools.registry").registry
    assert reg.get_tool_metadata("app_legacy_fetch").get("capability_scope") == "shared"
    assert "app_legacy_fetch" not in tv.assemble(base=["app_legacy_fetch"], runtime_id="inst-x")
    tv.grant("inst-x", ["app_legacy_fetch"], source="shared_tool:legacy_fetch")
    assert "app_legacy_fetch" in tv.assemble(base=["app_legacy_fetch"], runtime_id="inst-x")
