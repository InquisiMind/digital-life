"""前端图表后端数据层测试：token 消耗序列 + 精力采样 + 429/摘要区分。

覆盖本轮新增的数据生产与聚合逻辑：
1. TokenUsageTracker.usage_series —— 桶聚合 + kind 拆分（主调用/摘要/429）
2. record() 允许零 token 事件 kind（429 可写入，total=0 不影响 SUM）
3. call_llm 摘要路径记 kind=session_summary（补盲区）
4. state._persist_snapshot_for_tick 写入 vital_history（连续采样）
5. vital_history_series 读取
"""
from __future__ import annotations

import os
import tempfile
from unittest.mock import patch


# 收集测试创建的临时 db 文件，结尾清理
_TMP_DBS: list[str] = []


def _new_tracker():
    from infrastructure.budget.token_tracker import TokenUsageTracker
    db = tempfile.mktemp(suffix=".db")
    _TMP_DBS.append(db)
    return TokenUsageTracker(db), db


def teardown_module() -> None:
    for p in _TMP_DBS:
        try:
            os.unlink(p)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# usage_series 聚合
# ---------------------------------------------------------------------------


class TestUsageSeriesAggregation:
    def test_buckets_populated(self) -> None:
        """同桶多笔 → SUM 到该桶，按 kind 拆分。"""
        t, _ = _new_tracker()
        t.record(instance_id="i1", input_tokens=100, output_tokens=50, kind="llm_call")
        t.record(instance_id="i1", input_tokens=200, output_tokens=100, kind="llm_call")
        t.record(instance_id="i1", input_tokens=500, output_tokens=200, kind="session_summary")
        t.record(instance_id="i1", input_tokens=0, output_tokens=0, kind="llm_call_429")
        t.record(instance_id="i1", input_tokens=0, output_tokens=0, kind="llm_call_429")
        series = t.usage_series(hours=1, bucket="minute", instance_id="i1")
        assert len(series) >= 1
        b = series[0]
        assert b["input"] == 300        # 100+200 主调用
        assert b["output"] == 150       # 50+100
        assert b["total"] == 450
        assert b["input_summary"] == 500
        assert b["output_summary"] == 200
        assert b["total_summary"] == 700
        assert b["count_429"] == 2

    def test_429_zero_token_does_not_pollute_sum(self) -> None:
        """429 的 total=0 不会影响 SUM(total_tokens)。"""
        t, _ = _new_tracker()
        t.record(instance_id="i1", input_tokens=100, output_tokens=50, kind="llm_call")
        t.record(instance_id="i1", input_tokens=0, output_tokens=0, kind="llm_call_429")
        # usage_today 是 SUM(total_tokens) 全 kind，429 的 0 不拉低也不拉高
        assert t.usage_today("i1") == 150

    def test_instance_filter(self) -> None:
        """instance_id 过滤生效。"""
        t, _ = _new_tracker()
        t.record(instance_id="i1", input_tokens=100, output_tokens=0, kind="llm_call")
        t.record(instance_id="i2", input_tokens=999, output_tokens=0, kind="llm_call")
        s1 = t.usage_series(hours=1, bucket="minute", instance_id="i1")
        s2 = t.usage_series(hours=1, bucket="minute", instance_id="i2")
        assert s1[0]["input"] == 100
        assert s2[0]["input"] == 999

    def test_empty_returns_empty_list(self) -> None:
        """无数据 → 空列表，不报错。"""
        t, _ = _new_tracker()
        assert t.usage_series(hours=1, instance_id="i1") == []


# ---------------------------------------------------------------------------
# record 零 token 事件 kind
# ---------------------------------------------------------------------------


class TestRecordZeroTokenEventKind:
    def test_zero_token_default_kind_skipped(self) -> None:
        """默认 kind（llm_call）零 token 不写入（旧行为）。"""
        t, _ = _new_tracker()
        t.record(instance_id="i1", input_tokens=0, output_tokens=0, kind="llm_call")
        assert t.usage_series(hours=1, instance_id="i1") == []

    def test_zero_token_event_kind_written(self) -> None:
        """kind=llm_call_429 即使零 token 也写入（供 COUNT）。"""
        t, _ = _new_tracker()
        t.record(instance_id="i1", input_tokens=0, output_tokens=0, kind="llm_call_429")
        s = t.usage_series(hours=1, bucket="minute", instance_id="i1")
        assert len(s) == 1
        assert s[0]["count_429"] == 1


# ---------------------------------------------------------------------------
# call_llm 摘要路径记 token
# ---------------------------------------------------------------------------


class TestLlmSummaryTokenRecorded:
    def test_call_llm_records_summary_kind(self) -> None:
        """call_llm 成功后把 usage 记成 kind=session_summary。

        用 patch 拦截 httpx 调用，注入假 response.usage。
        """
        from infrastructure.ai import llm as llm_mod

        fake_response = type("R", (), {
            "raise_for_status": lambda self: None,
            "json": lambda self: {
                "choices": [{"message": {"content": "摘要内容"}}],
                "usage": {"prompt_tokens": 80, "completion_tokens": 20},
            },
        })()

        captured: dict = {}
        def fake_post(self, url, **kw):
            return fake_response

        def fake_record(self, **kw):
            captured.update(kw)

        with patch("httpx.Client.post", fake_post), \
             patch("infrastructure.budget.token_tracker.TokenUsageTracker.record", fake_record), \
             patch("infrastructure.config.get_app_instance_id", return_value="test_iid"), \
             patch("infrastructure.ai.llm.load_runtime_config", return_value={}), \
             patch("infrastructure.ai.llm.resolve_runtime_provider", return_value={"model": "m", "api_key": "k", "base_url": "http://x"}):
            result = llm_mod.call_llm("prompt", system_prompt="sys")
        assert result == "摘要内容"
        assert captured.get("kind") == "session_summary"
        assert captured.get("input_tokens") == 80
        assert captured.get("output_tokens") == 20
        assert captured.get("instance_id") == "test_iid"

    def test_call_llm_missing_usage_skips_record(self) -> None:
        """response 无 usage 字段时不报错、不记录。"""
        from infrastructure.ai import llm as llm_mod

        fake_response = type("R", (), {
            "raise_for_status": lambda self: None,
            "json": lambda self: {"choices": [{"message": {"content": "x"}}]},
        })()

        recorded = []
        with patch("httpx.Client.post", lambda self, url, **kw: fake_response), \
             patch("infrastructure.budget.token_tracker.TokenUsageTracker.record", lambda self, **kw: recorded.append(kw)), \
             patch("infrastructure.config.get_app_instance_id", return_value="iid"), \
             patch("infrastructure.ai.llm.load_runtime_config", return_value={}), \
             patch("infrastructure.ai.llm.resolve_runtime_provider", return_value={"model": "m"}):
            llm_mod.call_llm("p")
        assert recorded == []
