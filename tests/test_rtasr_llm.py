"""讯飞 RTASR 大模型版流式客户端（rtasr_llm）测试。

解析逻辑全部用 2026-10-28 真实凭据实测抓回的报文做样本——协议格式
（msg_type/data.cn.st/rt/ws/cw、type=0|1、wp 标点）以实测为准，不凭文档想象。
"""
from __future__ import annotations

from infrastructure.perception.rtasr_llm import (
    build_auth_url,
    clean_sentence,
    extract_message_text,
)

# 实测样本（节选自真实会话，词表已截短但结构保真）
PARTIAL_MSG = {
    "data": {
        "seg_id": 0,
        "cn": {"st": {"rt": [{"ws": [{"cw": [{"w": "你", "wp": "n"}], "wb": 0}]}],
                       "type": "1", "bg": 0, "ed": 260}},
        "ls": False,
    },
    "msg_type": "result",
    "res_type": "asr",
}

# 确认句样本：句首带分段边界漂移的逗号（实测 seg 12 现象）
FINAL_MSG = {
    "data": {
        "seg_id": 12,
        "cn": {"st": {"rt": [{"ws": [
            {"cw": [{"w": "，", "wp": "p"}]},
            {"cw": [{"w": "帮我", "wp": "n"}]},
            {"cw": [{"w": "查", "wp": "n"}]},
            {"cw": [{"w": "。", "wp": "p"}]},
        ]}], "type": "0", "bg": 1150, "ed": 6820}},
        "ls": True,
    },
    "msg_type": "result",
    "res_type": "asr",
}


def test_extract_partial():
    text, st_type = extract_message_text(PARTIAL_MSG)
    assert text == "你"
    assert st_type == "1"


def test_extract_final():
    text, st_type = extract_message_text(FINAL_MSG)
    assert text == "，帮我查。"
    assert st_type == "0"


def test_clean_sentence_strips_leading_punct():
    """确认句句首的分段边界逗号要剥掉（拼接时上一句已带自己的标点）。"""
    assert clean_sentence("，帮我查。") == "帮我查。"
    assert clean_sentence("  。好的") == "好的"
    assert clean_sentence("正常句子。") == "正常句子。"


def test_extract_malformed_is_safe():
    assert extract_message_text({}) == ("", "")
    assert extract_message_text({"data": {"cn": {}}}) == ("", "")
    assert extract_message_text({"data": None}) == ("", "")


def test_build_auth_url_shape():
    """签名 URL 结构：端点 + 全参数 + signature 在最后；可重复构建（uuid 不同）。"""
    url = build_auth_url("app", "key-id", "secret")
    assert url.startswith(
        "wss://office-api-ast-dx.iflyaisol.com/ast/communicate/v1?"
    )
    query = url.split("?", 1)[1]
    params = dict(p.split("=", 1) for p in query.split("&"))
    assert params["appId"] == "app"
    assert params["accessKeyId"] == "key-id"
    assert params["samplerate"] == "16000"
    assert params["audio_encode"] == "pcm_s16le"
    assert "signature" in params and params["signature"]
    # utc 为北京时间 ISO 带时区（冒号被编码）
    assert "%3A" in params["utc"] and "%2B0800" in params["utc"]
    # 每次连接 uuid 独立（防签名重放）
    assert build_auth_url("app", "key-id", "secret") != url
