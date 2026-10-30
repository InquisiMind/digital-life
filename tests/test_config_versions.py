"""配置文件版本管理（versioned_io + 管理端点）契约测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests.test_customer_routes import customer_env  # noqa: F401  复用演示 defs 环境


@pytest.fixture()
def vfile(tmp_path, monkeypatch):
    """隔离的版本库与目标文件。"""
    f = tmp_path / "apps" / "demo-def" / "persona" / "LIFE_PERSONA.md"
    f.parent.mkdir(parents=True)
    f.write_text("v1 内容", encoding="utf-8")
    monkeypatch.setattr(
        "infrastructure.filesystem.versioned_io._project_root", lambda: tmp_path)
    return f


def test_write_versioned_snapshots_old_content(vfile):
    from infrastructure.filesystem import versioned_io

    versioned_io.write_versioned(vfile, "v2 内容", note="第一次编辑")
    assert vfile.read_text(encoding="utf-8") == "v2 内容"
    versions = versioned_io.list_versions(vfile)
    assert len(versions) == 1
    assert versions[0]["note"].startswith("改前自动快照")
    assert versioned_io.read_version(vfile, versions[0]["version"]) == "v1 内容"


def test_restore_is_reversible(vfile):
    from infrastructure.filesystem import versioned_io

    versioned_io.write_versioned(vfile, "v2 内容")
    versions = versioned_io.list_versions(vfile)
    v1 = versions[0]["version"]

    # 回滚到 v1 → 回滚前当前内容(v2)自动快照
    assert versioned_io.restore_version(vfile, v1, note="测试回滚")
    assert vfile.read_text(encoding="utf-8") == "v1 内容"
    now_versions = versioned_io.list_versions(vfile)
    assert now_versions[0]["note"].startswith("回滚到")
    v2_id = now_versions[0]["version"]
    assert versioned_io.read_version(vfile, v2_id) == "v2 内容"

    # 再回滚一次 = 撤销回滚
    versioned_io.restore_version(vfile, v2_id)
    assert vfile.read_text(encoding="utf-8") == "v2 内容"


def test_prune_keeps_recent_versions(vfile, monkeypatch):
    from infrastructure.filesystem import versioned_io

    # 让保留窗口变小，免得真写 30+ 份
    monkeypatch.setattr(versioned_io, "_KEEP", 3)
    for i in range(6):
        versioned_io.write_versioned(vfile, f"内容 {i}")
    versions = versioned_io.list_versions(vfile)
    assert len(versions) == 3
    # 最旧的被淘汰，最新内容可读
    assert versioned_io.read_version(vfile, versions[0]["version"]) == "内容 4"


def test_missing_version_and_missing_file(vfile):
    from infrastructure.filesystem import versioned_io

    assert versioned_io.read_version(vfile, "v20990101-000000-000000") is None
    assert versioned_io.restore_version(vfile, "v20990101-000000-000000") is False
    ghost = vfile.parent / "GHOST.md"
    assert versioned_io.snapshot(ghost) is None  # 不存在的文件 no-op


def test_def_file_versions_endpoints(customer_env, monkeypatch):
    """管理端点契约：list → content → restore，kind 由服务端映射路径。"""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from business.consulting.services_admin_routes import add_services_admin_routes

    app = web.Application()
    add_services_admin_routes(app)

    async def _run():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            # 造点历史：直接改 persona 两次（走 write_versioned 同款路径）
            from infrastructure.filesystem import versioned_io
            from infrastructure.config import get_project_root

            f = Path(str(customer_env)) / "apps" / "consulting-partner-def" / "persona" / "LIFE_PERSONA.md"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("第一版人设", encoding="utf-8")
            versioned_io.write_versioned(f, "第二版人设", note="测试")

            url = "/api/admin/agent-defs/consulting-partner-def/file-versions?kind=persona"
            d = await (await client.get(url)).json()
            assert d["ok"] and len(d["versions"]) == 1
            vid = d["versions"][0]["version"]

            c = await (await client.get(
                "/api/admin/agent-defs/consulting-partner-def/file-versions/content"
                f"?kind=persona&version={vid}")).json()
            assert c["ok"] and c["content"] == "第一版人设"

            r = await (await client.post(
                "/api/admin/agent-defs/consulting-partner-def/file-versions/restore",
                json={"kind": "persona", "version": vid})).json()
            assert r["ok"] and r["restored"] == vid
            assert f.read_text(encoding="utf-8") == "第一版人设"

            # 非法 kind → 404，不开任意路径读
            bad = await client.get(
                "/api/admin/agent-defs/consulting-partner-def/file-versions?kind=../../secrets")
            assert bad.status == 404
        finally:
            await client.close()

    asyncio.run(_run())


def test_tool_face_hides_express_for_services_only(monkeypatch):
    """express_to_human 只对服务隐藏（host_only 旗标）；协作工具只对实例隐藏（service_only 旗标）。"""
    import importlib

    from domain import tool_visibility as tv
    from interfaces.tools import ensure_loaded

    ensure_loaded()
    reg = importlib.import_module("interfaces.tools.registry").registry
    assert reg.get_tool_metadata("express_to_human").get("host_only") is True
    assert reg.get_tool_metadata("send_chat_message").get("service_only") is True
    assert reg.get_tool_metadata("sense_project_peers").get("service_only") is True

    monkeypatch.setattr(
        importlib.import_module("infrastructure.persistence.services_registry"),
        "resolve_service_def",
        lambda rid: "def-x" if rid == "svc-1" else None,
    )

    # 服务侧：express 被摘（对客户/群发言统一 send_chat_message）
    out = tv.assemble(
        base=["send_chat_message", "express_to_human", "terminal", "sense_project_peers"],
        runtime_id="svc-1",
    )
    assert "express_to_human" not in out and "terminal" in out

    # 实例侧：协作工具整组隐藏，express（宿主工具）保留——实例飞书线要用
    out = tv.assemble(base=["express_to_human", "sense_project_peers"], runtime_id="inst-1")
    assert "express_to_human" in out and "sense_project_peers" not in out
