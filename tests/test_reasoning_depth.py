"""思考深度 = 人的基本配置（2026-10-20 定稿）。

模型：depth(本次调用) = 这个人的 agent.reasoning_depth（quick/standard/deep），
运行时没有任何场景规则；厂商值由 provider 家族声明（见 test_providers_thinking_keep）。
本文件覆盖配置解析、控制台 schema 与服务台 def profile 读写三处接入面。
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
import yaml

from infrastructure.ai.config import parse_reasoning_depth
from infrastructure.ai.providers import (
    DEFAULT_REASONING_DEPTH,
    REASONING_DEPTH_LABELS,
    REASONING_DEPTHS,
)


# ── 配置解析 ──────────────────────────────────────────────────────────


def test_missing_config_falls_back_to_standard():
    assert parse_reasoning_depth(None) == {"depth": DEFAULT_REASONING_DEPTH}
    assert parse_reasoning_depth({}) == {"depth": "standard"}
    assert parse_reasoning_depth({"agent": {}}) == {"depth": "standard"}


@pytest.mark.parametrize("depth", REASONING_DEPTHS)
def test_valid_depth_passthrough(depth):
    assert parse_reasoning_depth({"agent": {"reasoning_depth": depth}}) == {"depth": depth}


def test_depth_is_case_insensitive_and_trimmed():
    assert parse_reasoning_depth({"agent": {"reasoning_depth": "  Deep "}})["depth"] == "deep"


def test_invalid_depth_warns_and_falls_back(caplog):
    """厂商值（medium/high/xhigh）不是产品档位——配置写错要响，不静默。"""
    with caplog.at_level(logging.WARNING):
        out = parse_reasoning_depth({"agent": {"reasoning_depth": "medium"}})
    assert out == {"depth": "standard"}
    assert any("reasoning_depth" in r.getMessage() for r in caplog.records)


def test_depth_map_override_is_filtered_to_known_levels():
    cfg = {
        "agent": {"reasoning_depth": "deep"},
        "model": {"reasoning_depth_map": {"deep": "max", "bogus": "x", "quick": "", "STANDARD": " high "}},
    }
    out = parse_reasoning_depth(cfg)
    assert out == {"depth": "deep", "depth_map": {"deep": "max", "standard": "high"}}


def test_depth_map_absent_or_empty_is_omitted():
    assert "depth_map" not in parse_reasoning_depth({"model": {"reasoning_depth_map": {}}})
    assert "depth_map" not in parse_reasoning_depth({"model": {"reasoning_depth_map": "max"}})


def test_labels_cover_every_level():
    assert set(REASONING_DEPTH_LABELS) == set(REASONING_DEPTHS)


# ── 控制台 schema（实例「任务策略」）──────────────────────────────────────


def test_config_center_exposes_depth_not_vendor_effort():
    from application.console.config_center import FIELDS, ConfigCenterWorkflow

    by_key = {f.key: f for f in FIELDS}
    assert "agent.reasoning_effort" not in by_key
    f = by_key["agent.reasoning_depth"]
    assert f.section == "tasks" and f.value_type == "select" and f.path == "agent.reasoning_depth"
    assert f.options == tuple(REASONING_DEPTHS)
    assert f.default == DEFAULT_REASONING_DEPTH
    schema = ConfigCenterWorkflow._field_schema(f)
    assert schema["options"] == list(REASONING_DEPTHS)
    assert schema["option_labels"] == dict(REASONING_DEPTH_LABELS)


# ── 服务台 def profile（咨询的"人"）────────────────────────────────────────


class _FakeRequest:
    def __init__(self, def_id: str, body: dict | None = None):
        self.match_info = {"def_id": def_id}
        self._body = body or {}

    async def json(self):
        return self._body


@pytest.fixture()
def def_env(tmp_path, monkeypatch):
    monkeypatch.setattr("infrastructure.config.get_project_root", lambda: tmp_path)
    d = tmp_path / "apps" / "strategy-writer" / "config"
    d.mkdir(parents=True)
    (d / "app.yaml").write_text(
        "runtime_kind: definition\ndisplay_name: 撰稿人\nmodel:\n  name: glm-5.3\n",
        encoding="utf-8",
    )
    (tmp_path / "apps" / "strategy-writer" / "persona").mkdir()
    return tmp_path


def _profile_get(def_id: str) -> dict:
    from business.consulting import agent_admin_routes as r

    resp = asyncio.run(r.handle_def_profile_get(_FakeRequest(def_id)))
    return json.loads(resp.text)


def _profile_put(def_id: str, body: dict) -> tuple[int, dict]:
    from business.consulting import agent_admin_routes as r

    resp = asyncio.run(r.handle_def_profile_put(_FakeRequest(def_id, body)))
    return resp.status, json.loads(resp.text)


def test_profile_depth_roundtrip_and_inherit(def_env):
    app_yaml = def_env / "apps" / "strategy-writer" / "config" / "app.yaml"

    # 未声明 = 空 = 沿用全局兜底
    assert _profile_get("strategy-writer")["agent"] == {"reasoning_depth": ""}

    status, d = _profile_put("strategy-writer", {"agent": {"reasoning_depth": "deep"}})
    assert status == 200 and d["ok"] and "reasoning_depth" in d["fields"]
    assert yaml.safe_load(app_yaml.read_text(encoding="utf-8"))["agent"] == {"reasoning_depth": "deep"}
    assert _profile_get("strategy-writer")["agent"]["reasoning_depth"] == "deep"
    # 运行时三层合并读到的就是这个人的档
    assert parse_reasoning_depth(yaml.safe_load(app_yaml.read_text(encoding="utf-8")))["depth"] == "deep"

    # 清空 = 恢复沿用全局，agent 段整体移除（不留空壳）
    status, d = _profile_put("strategy-writer", {"agent": {"reasoning_depth": ""}})
    assert status == 200 and d["ok"]
    cfg = yaml.safe_load(app_yaml.read_text(encoding="utf-8"))
    assert "agent" not in cfg
    assert cfg["model"] == {"name": "glm-5.3"}  # 其余配置不受影响


def test_profile_rejects_vendor_values(def_env):
    status, d = _profile_put("strategy-writer", {"agent": {"reasoning_depth": "xhigh"}})
    assert status == 400 and d["ok"] is False
    assert "quick/standard/deep" in d["error"]
