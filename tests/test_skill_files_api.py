"""skill 附属文档管理 API——下载 zip / 文件清单 / 单文件详情（只读）。

路径穿越防护：name 必须是合法 skill 名，file?path= 不允许绝对路径与 ..；
下载 zip 与上传格式对等（SKILL.md + 附属文档）。
"""

from __future__ import annotations

import asyncio
import io
import json
import zipfile

import pytest


class _FakeRequest:
    def __init__(self, match_info=None, query=None):
        self.match_info = match_info or {}
        self.query = query or {}


@pytest.fixture()
def skill_env(tmp_path, monkeypatch):
    monkeypatch.setattr("infrastructure.config.get_project_root", lambda: tmp_path)
    d = tmp_path / "apps" / "r-def" / "skills" / "demo-skill"
    (d / "references").mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: demo-skill\n---\n正文\n", encoding="utf-8")
    (d / "references" / "api-notes.md").write_text("# 附属文档\n细节内容", encoding="utf-8")
    return tmp_path


def _get(handler_name: str, *, match: dict, query: dict | None = None):
    from business.consulting import agent_admin_routes as r

    resp = asyncio.run(getattr(r, handler_name)(_FakeRequest(match, query)))
    return resp


def test_files_list_includes_docs(skill_env):
    resp = _get("handle_def_skill_files", match={"def_id": "r-def", "name": "demo-skill"})
    d = json.loads(resp.text)
    assert d["ok"] and d["name"] == "demo-skill"
    paths = [f["path"] for f in d["files"]]
    assert "SKILL.md" in paths and "references/api-notes.md" in paths


def test_file_detail_reads_doc(skill_env):
    resp = _get("handle_def_skill_file",
                match={"def_id": "r-def", "name": "demo-skill"},
                query={"path": "references/api-notes.md"})
    d = json.loads(resp.text)
    assert d["ok"] and "附属文档" in d["content"]


def test_file_detail_blocks_traversal(skill_env):
    for bad in ("../../persona/LIFE_PERSONA.md", "/config/app.yaml", "a/../../b"):
        resp = _get("handle_def_skill_file",
                    match={"def_id": "r-def", "name": "demo-skill"},
                    query={"path": bad})
        assert json.loads(resp.text)["ok"] is False, bad


def test_download_zip_mirrors_directory(skill_env):
    resp = _get("handle_def_skill_download",
                match={"def_id": "r-def", "name": "demo-skill"})
    zf = zipfile.ZipFile(io.BytesIO(resp.body))
    names = set(zf.namelist())
    assert "SKILL.md" in names and "references/api-notes.md" in names
    assert resp.content_type == "application/zip"


def test_bad_skill_name_rejected(skill_env):
    resp = _get("handle_def_skill_files", match={"def_id": "r-def", "name": ".."})
    assert json.loads(resp.text)["ok"] is False
