"""web_search 工具契约：配置门控、博查请求、结果格式化、错误兜底。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture()
def ws_env(tmp_path, monkeypatch):
    monkeypatch.setattr("infrastructure.config.get_project_root", lambda: tmp_path)
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True)

    def patch_cfg(cfg: dict):
        import yaml

        (cfg_dir / "local.yaml").write_text(
            yaml.safe_dump({"web_search": cfg}, allow_unicode=True), encoding="utf-8")

    from interfaces.tools import web_search_tool as wst

    wst.register(__import__("interfaces.tools.registry", fromlist=["registry"]).registry)
    return wst, patch_cfg


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def test_hidden_without_key(ws_env, monkeypatch):
    wst, patch_cfg = ws_env
    patch_cfg({"provider": "bocha"})  # 无 key
    assert wst._configured() is False


def test_search_formats_results(ws_env, monkeypatch):
    wst, patch_cfg = ws_env
    patch_cfg({"provider": "bocha", "api_key": "test-key"})
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = json
        return _FakeResp({"code": 200, "data": {"webPages": {"value": [
            {"name": "若羽臣年报", "url": "https://example.cn/a",
             "summary": "2026H1 营收 22.86 亿", "siteName": "巨潮",
             "datePublished": "2026-03-01T00:00:00Z"},
        ]}}})

    monkeypatch.setattr("requests.post", fake_post)
    out = json.loads(wst._handle_web_search({"query": "若羽臣 2026 年报", "freshness": "oneYear"}))
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["body"]["summary"] is True
    assert captured["body"]["freshness"] == "oneYear"
    assert out["results"][0]["title"] == "若羽臣年报"
    assert out["results"][0]["date"] == "2026-03-01"


def test_bad_freshness_falls_back(ws_env, monkeypatch):
    wst, patch_cfg = ws_env
    patch_cfg({"provider": "bocha", "api_key": "k"})
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["freshness"] = json["freshness"]
        return _FakeResp({"code": 200, "data": {"webPages": {"value": []}}})

    monkeypatch.setattr("requests.post", fake_post)
    out = json.loads(wst._handle_web_search({"query": "x", "freshness": "tomorrow"}))
    assert seen["freshness"] == "noLimit"
    assert out["results"] == []


def test_http_error_returns_tool_error(ws_env, monkeypatch):
    wst, patch_cfg = ws_env
    patch_cfg({"provider": "bocha", "api_key": "k"})

    def fake_post(url, **kw):
        return _FakeResp({}, status=401)

    monkeypatch.setattr("requests.post", fake_post)
    out = wst._handle_web_search({"query": "x"})
    assert '"error"' in out
