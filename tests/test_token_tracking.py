"""Token tracking + 精力-token 耦合的集成测试（设计文档 15.4 / 二十三）。

覆盖：
  1. TokenUsageTracker：record 后能正确按小时/天聚合
  2. 配置系数（ENERGY_PER_KTOKEN_INPUT/OUTPUT）默认值正确、env 可覆盖
  3. 实际接通端到端：AIAgent._record_token_usage 把 raw usage 转成
     精力消耗 + 写入预算追踪器
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def isolated_runtime(tmp_path, monkeypatch):
    """每个测试一个独立的 token_tracker（DB 完全隔离）。

    生产里 get_runtime_state_db_path() 解析的是 instance-scoped 路径，不
    受 DIGITAL_LIFE_RUNTIME_HOME 影响；所以测试直接构造独立 tracker，不
    走 get_token_tracker() 的单例 dispatch（否则会串到实例目录下的真实 DB）。
    """
    from infrastructure.budget.token_tracker import reset_token_tracker_for_test
    reset_token_tracker_for_test()
    # patch get_token_tracker 让它每次返回独立 tmp_path 的实例
    from infrastructure.budget import token_tracker as tt_mod

    def _factory(_db_path=None):
        # 忽略传入的 db_path，每次用 tmp_path 子文件
        return tt_mod.TokenUsageTracker(tmp_path / "state.db")

    # 也 patch infrastructure.budget 顶层 export 的 get_token_tracker
    import infrastructure.budget as budget_mod
    monkeypatch.setattr(budget_mod, "get_token_tracker", _factory)
    monkeypatch.setattr(tt_mod, "get_token_tracker", _factory)
    yield tmp_path


def test_token_tracker_records_and_aggregates(isolated_runtime):
    """record 一笔 token → usage_last_hour / usage_today 能正确累加。"""
    from infrastructure.budget import get_token_tracker
    from infrastructure.config import set_current_instance_id
    set_current_instance_id("test-tracker")

    t = get_token_tracker()
    # 起点应该是 0
    assert t.usage_last_hour("test-tracker") == 0
    assert t.usage_today("test-tracker") == 0

    # 记两笔
    t.record(instance_id="test-tracker", input_tokens=1000, output_tokens=200,
             session_id="s1")
    t.record(instance_id="test-tracker", input_tokens=3000, output_tokens=500,
             session_id="s1")

    # total = (1000+200) + (3000+500) = 4700
    assert t.usage_last_hour("test-tracker") == 4700
    assert t.usage_today("test-tracker") == 4700
    # 重置时间格式正确（北京时 ISO）
    assert "T" in t.hour_resets_at()
    assert "T" in t.day_resets_at()


def test_token_tracker_zero_records_skipped(isolated_runtime):
    """全 0 token 的记录应被静默丢掉。"""
    from infrastructure.budget import get_token_tracker
    from infrastructure.config import set_current_instance_id
    set_current_instance_id("test-zero")
    t = get_token_tracker()
    t.record(instance_id="test-zero", input_tokens=0, output_tokens=0,
             session_id="s")
    assert t.usage_today("test-zero") == 0


def test_energy_token_coefficient_defaults_and_env_override(isolated_runtime, monkeypatch):
    """默认:input=0.005, output=0.05（10× 比，output 贵）；env override 后值变化。"""
    # 清掉可能的 env 干扰
    monkeypatch.delenv("DIGITAL_LIFE_ENERGY_PER_KTOKEN_INPUT", raising=False)
    monkeypatch.delenv("DIGITAL_LIFE_ENERGY_PER_KTOKEN_OUTPUT", raising=False)
    import importlib
    import domain.vital.simulation.engine as eng
    importlib.reload(eng)
    eng._resolve_energy_token_constants()
    assert eng.ENERGY_PER_KTOKEN_INPUT == 0.005
    assert eng.ENERGY_PER_KTOKEN_OUTPUT == 0.05

    monkeypatch.setenv("DIGITAL_LIFE_ENERGY_PER_KTOKEN_INPUT", "0.15")
    monkeypatch.setenv("DIGITAL_LIFE_ENERGY_PER_KTOKEN_OUTPUT", "0.9")
    eng._resolve_energy_token_constants()
    assert eng.ENERGY_PER_KTOKEN_INPUT == 0.15
    assert eng.ENERGY_PER_KTOKEN_OUTPUT == 0.9


def test_agent_records_token_usage_from_raw_response(isolated_runtime):
    """AIAgent._record_token_usage 解析 raw.usage，累计到 session 字段 + 写预算追踪器。"""
    from infrastructure.ai.agent import AIAgent
    from infrastructure.budget import get_token_tracker
    from infrastructure.config import set_current_instance_id
    set_current_instance_id("test-agent")

    ag = AIAgent(model="x", base_url="http://x", session_id="s1")
    assert ag.session_input_tokens == 0
    assert ag.session_output_tokens == 0

    # 模拟 GLM API 返回（标准 OpenAI 兼容格式）
    raw = {
        "choices": [{"message": {"content": "hi"}}],
        "usage": {"prompt_tokens": 20000, "completion_tokens": 800,
                  "total_tokens": 20800},
    }
    ag._record_token_usage(raw)

    assert ag.session_input_tokens == 20000
    assert ag.session_output_tokens == 800
    # budget_log 也记了一笔
    used = get_token_tracker().usage_last_hour("test-agent")
    assert used == 20800


def test_agent_skips_missing_usage(isolated_runtime):
    """raw 没有 usage 字段时静默跳过，不破坏 LLM 流程。"""
    from infrastructure.ai.agent import AIAgent
    from infrastructure.config import set_current_instance_id
    set_current_instance_id("test-missing")
    ag = AIAgent(model="x", base_url="http://x")

    # 没 usage 字段
    ag._record_token_usage({"choices": [{}]})
    assert ag.session_input_tokens == 0
    assert ag.session_output_tokens == 0

    # usage 是 None
    ag._record_token_usage({"usage": None})
    assert ag.session_input_tokens == 0

    # usage 全 0
    ag._record_token_usage({"usage": {"prompt_tokens": 0, "completion_tokens": 0}})
    assert ag.session_input_tokens == 0
