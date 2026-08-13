"""circuit_breaker 429 错误码分类测试。

验证：429 状态码本身无法区分限流 vs 欠费，靠 response body 的 error.code
决定熔断时长（hard → MAX，soft → Retry-After）。
"""
from __future__ import annotations

import json

from infrastructure.budget import circuit_breaker as cb


# ── extract_glm_error_code ──────────────────────────────────────────────────


def test_extract_code_from_dict():
    body = {"error": {"code": "1113", "message": "余额不足"}}
    assert cb.extract_glm_error_code(body) == "1113"


def test_extract_code_from_json_bytes():
    body = json.dumps({"error": {"code": "1302"}}).encode()
    assert cb.extract_glm_error_code(body) == "1302"


def test_extract_code_from_json_string():
    body = '{"error": {"code": "1305"}}'
    assert cb.extract_glm_error_code(body) == "1305"


def test_extract_code_missing_returns_none():
    assert cb.extract_glm_error_code(None) is None
    assert cb.extract_glm_error_code("") is None
    assert cb.extract_glm_error_code({}) is None
    assert cb.extract_glm_error_code({"error": "string"}) is None
    assert cb.extract_glm_error_code("not json") is None


# ── classify_429 ────────────────────────────────────────────────────────────


def test_classify_hard_codes():
    """硬故障：充值前重试必败。"""
    for code in ("1113", "1314", "1311"):
        assert cb.classify_429(code) == "hard", f"{code} 应为 hard"


def test_classify_soft_codes():
    """软限流：窗口后自愈。"""
    for code in ("1302", "1305", "1308", "1310", "1313"):
        assert cb.classify_429(code) == "soft", f"{code} 应为 soft"


def test_classify_unknown():
    assert cb.classify_429(None) == "unknown"
    assert cb.classify_429("9999") == "unknown"
    assert cb.classify_429("1316") == "unknown"  # 复合码，未穷举 → 保守 soft/unknown


# ── resolve_retry_after_for_429 ─────────────────────────────────────────────


def test_hard_failure_uses_max_regardless_of_header():
    """余额不足：即使有短 Retry-After 头也用 MAX（等也没用）。"""
    secs, reason = cb.resolve_retry_after_for_429(
        retry_after_header="10",
        response_body={"error": {"code": "1113"}},
    )
    assert secs == cb.MAX_RETRY_AFTER_SEC
    assert reason == "429:hard:1113"


def test_hard_failure_no_header():
    secs, reason = cb.resolve_retry_after_for_429(
        retry_after_header=None,
        response_body={"error": {"code": "1113", "message": "欠费"}},
    )
    assert secs == cb.MAX_RETRY_AFTER_SEC
    assert "hard" in reason and "1113" in reason


def test_soft_limit_respects_retry_after_header():
    """真限流：沿用 Retry-After 头解析。"""
    secs, reason = cb.resolve_retry_after_for_429(
        retry_after_header="120",
        response_body={"error": {"code": "1302"}},
    )
    assert secs == 120
    assert reason == "429:soft:1302"


def test_soft_limit_no_header_uses_default():
    secs, reason = cb.resolve_retry_after_for_429(
        retry_after_header=None,
        response_body={"error": {"code": "1302"}},
    )
    assert secs == cb.DEFAULT_RETRY_AFTER_SEC
    assert reason == "429:soft:1302"


def test_unknown_code_no_body_uses_default():
    """未知 code + 无 body → 默认窗口（保守，不改变原行为）。"""
    secs, reason = cb.resolve_retry_after_for_429(
        retry_after_header=None,
        response_body=None,
    )
    assert secs == cb.DEFAULT_RETRY_AFTER_SEC
    assert reason == "429:unknown"


def test_hard_failure_dominates_short_header():
    """关键场景：余额不足 + Retry-After=300 → 用 MAX(3600) 而非 300。

    这是修复的核心：之前 1113 用 300s，到期重试必败又刷新；现在用 MAX
    避免无效重试刷屏。"""
    secs, _ = cb.resolve_retry_after_for_429(
        retry_after_header="300",
        response_body={"error": {"code": "1113"}},
    )
    assert secs == cb.MAX_RETRY_AFTER_SEC
    assert secs > 300  # MAX(3600) > 默认 300


# ── trip / is_tripped 集成：reason 留痕 ─────────────────────────────────────


def test_trip_records_hard_reason(tmp_path, monkeypatch):
    """trip 写入的 reason 含分类信息，可被 is_tripped 读回（运维诊断）。"""
    monkeypatch.setattr(cb, "_repo_root", lambda: tmp_path)

    cb.trip(
        "test-hard-key",
        retry_after_sec=cb.MAX_RETRY_AFTER_SEC,
        instance_id="inst-x",
        reason="429:hard:1113",
    )
    tripped, info = cb.is_tripped("test-hard-key")
    assert tripped is True
    assert info["reason"] == "429:hard:1113"
    cb.clear("test-hard-key")
