"""语音会话段路由（voice_router）测试。

验证：
  - match_instance：关键词子串匹配（命中/未命中/空文本/大小写）
  - build_instance_keyword_map：遍历活跃实例读 keywords（mock discover）
  - emit_segment_to_instance：设 ContextVar + emit_event（mock emit）
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from infrastructure.perception.voice_router import match_instance


# ── match_instance：纯函数 ──────────────────────────────────────────────────


@pytest.fixture
def keyword_map():
    return {
        "iid-zero": ["zero", "Zero", "ZERO"],
        "iid-alpha": ["alpha", "Alpha", "ALPHA", "小α"],
    }


def test_match_zero(keyword_map):
    assert match_instance("zero 帮我查个东西", keyword_map) == "iid-zero"


def test_match_alpha(keyword_map):
    assert match_instance("alpha 那个文件在哪", keyword_map) == "iid-alpha"


def test_match_case_insensitive(keyword_map):
    """大小写不敏感：'ZERO' 和 'zero' 都命中。"""
    assert match_instance("ZERO 你好", keyword_map) == "iid-zero"
    assert match_instance("zero 你好", keyword_map) == "iid-zero"


def test_no_match_returns_none(keyword_map):
    assert match_instance("今天天气不错", keyword_map) is None


def test_empty_transcript_returns_none(keyword_map):
    assert match_instance("", keyword_map) is None
    assert match_instance("   ", keyword_map) is None


def test_none_transcript_returns_none(keyword_map):
    assert match_instance(None, keyword_map) is None  # type: ignore[arg-type]


def test_keyword_as_substring(keyword_map):
    """子串匹配：'zero' 在句中任意位置都命中。"""
    assert match_instance("嘿 zero 看看这个", keyword_map) == "iid-zero"


def test_empty_keyword_map():
    """空 map → None。"""
    assert match_instance("zero", {}) is None


def test_chinese_keyword(keyword_map):
    """中文关键词也能匹配。"""
    assert match_instance("小α 帮个忙", keyword_map) == "iid-alpha"


# ── build_instance_keyword_map：mock discover_active_instances ──────────────


def test_build_keyword_map_reads_app_yaml(tmp_path):
    """mock discover_active_instances + app.yaml 读取，验证 map 构建。

    build_instance_keyword_map 内部是延迟 import（from infrastructure.config import ...），
    所以必须 patch 源模块 infrastructure.config，而非 voice_router 的属性。
    """
    zero_yaml = tmp_path / "apps" / "iid-zero" / "config" / "app.yaml"
    zero_yaml.parent.mkdir(parents=True)
    zero_yaml.write_text(
        "display_name: zero\n"
        "group_chat:\n"
        "  attention_keywords:\n"
        "  - zero\n  - Zero\n", encoding="utf-8")
    alpha_yaml = tmp_path / "apps" / "iid-alpha" / "config" / "app.yaml"
    alpha_yaml.parent.mkdir(parents=True)
    alpha_yaml.write_text(
        "display_name: alpha\n"
        "group_chat:\n"
        "  attention_keywords:\n"
        "  - alpha\n", encoding="utf-8")

    import infrastructure.config as cfg_mod
    from infrastructure.perception.voice_router import build_instance_keyword_map

    with patch.object(cfg_mod, "discover_active_instances", return_value=["iid-zero", "iid-alpha"]), \
         patch.object(cfg_mod, "get_instance_app_config_path",
                      side_effect=lambda iid: tmp_path / "apps" / iid / "config" / "app.yaml"):
        m = build_instance_keyword_map()

    assert "iid-zero" in m and "iid-alpha" in m
    assert "zero" in m["iid-zero"]
    assert "alpha" in m["iid-alpha"]


def test_build_keyword_map_skips_no_keywords(tmp_path):
    """没有 attention_keywords 的实例不进 map。"""
    yaml_path = tmp_path / "apps" / "iid-x" / "config" / "app.yaml"
    yaml_path.parent.mkdir(parents=True)
    yaml_path.write_text("display_name: x\n", encoding="utf-8")  # 无 group_chat

    import infrastructure.config as cfg_mod
    from infrastructure.perception.voice_router import build_instance_keyword_map

    with patch.object(cfg_mod, "discover_active_instances", return_value=["iid-x"]), \
         patch.object(cfg_mod, "get_instance_app_config_path",
                      side_effect=lambda iid: tmp_path / "apps" / iid / "config" / "app.yaml"):
        m = build_instance_keyword_map()

    assert "iid-x" not in m  # 无关键词 → 不进 map


# ── emit_segment_to_instance：mock emit_event + ContextVar ─────────────────


def test_emit_segment_sets_context_and_emits():
    """emit_segment_to_instance 应设目标实例 ContextVar + 调 emit_event。"""
    emitted = []

    def fake_emit(kind, payload, **kw):
        emitted.append((kind, payload))
        return 42

    from infrastructure.perception.config import PerceptionConfig

    cfg = PerceptionConfig(api_key="test-key")

    with patch("domain.lifecycle.events.emit_event", side_effect=fake_emit), \
         patch("infrastructure.config.set_current_instance_id") as mock_set_id, \
         patch("domain.lifecycle.events.set_instance_context") as mock_set_ctx:
        from infrastructure.perception.voice_router import emit_segment_to_instance
        eid = emit_segment_to_instance("target-iid", "zero 你好", "/tmp/seg.wav", cfg)

    assert eid == 42
    assert len(emitted) == 1
    kind, payload = emitted[0]
    assert kind == "perception_signal"
    assert payload["transcript"] == "zero 你好"
    assert payload["reply_channel"] == "voice"
    assert payload["source"] == "voice_session"
    # 两个 ContextVar 都设到了目标实例
    mock_set_id.assert_called_once_with("target-iid")
    mock_set_ctx.assert_called_once_with("target-iid")


def test_emit_segment_failure_returns_none():
    """emit 抛异常时返回 None，不崩。"""
    from infrastructure.perception.config import PerceptionConfig
    cfg = PerceptionConfig(api_key="test")

    with patch("domain.lifecycle.events.emit_event", side_effect=RuntimeError("boom")), \
         patch("infrastructure.config.set_current_instance_id"), \
         patch("domain.lifecycle.events.set_instance_context"):
        from infrastructure.perception.voice_router import emit_segment_to_instance
        eid = emit_segment_to_instance("iid", "text", "/tmp/x.wav", cfg)

    assert eid is None
