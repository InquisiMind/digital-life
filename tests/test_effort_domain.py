"""端点 reasoning_effort 值域学习库测试（infrastructure/ai/effort_domain.py）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from infrastructure.ai import effort_domain as ed


@pytest.fixture()
def store_env(tmp_path, monkeypatch):
    monkeypatch.setattr("infrastructure.config.get_project_root", lambda: tmp_path)
    ed._CACHE = None  # 重置模块缓存，隔离用例
    yield tmp_path
    ed._CACHE = None


def test_endpoint_key_normalizes_path(store_env):
    assert ed.endpoint_key("https://gw.example/v1/chat/completions") == "https://gw.example"
    assert ed.endpoint_key("https://gw.example/v1") == ed.endpoint_key("https://gw.example/other")


def test_record_and_get_roundtrip(store_env):
    assert ed.get_fix("https://gw.example/v1/chat/completions", "medium") is None
    ed.record_fix("https://gw.example/v1/chat/completions", "medium", "high")
    assert ed.get_fix("https://gw.example/v2/anything", "medium") == "high"
    assert ed.get_fix("https://other.example/v1", "medium") is None  # 端点隔离


def test_persisted_across_module_cache_reset(store_env):
    ed.record_fix("https://gw.example/v1", "medium", "high")
    ed._CACHE = None  # 模拟新进程（worker 每 wake 重生）
    assert ed.get_fix("https://gw.example/v1/chat/completions", "medium") == "high"
    assert (store_env / "var" / "llm_effort_domains.json").is_file()


def test_near_mapping_table(store_env):
    assert ed.NEAR["medium"] == "high" and ed.NEAR["minimal"] == "low" and ed.NEAR["xhigh"] == "max"
