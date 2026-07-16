"""P1 — 嵌入客户端 partial-success / no-retry / timeout 测试。

Spec feature 002 / User Story 1 / FR-103 / SC-003.
驱动问题(T010 修复前的当前行为):
  - 当前 `_embed_texts` 是 all-or-nothing，任一条 null → 返 None、丢整批
  - 任何异常包括 429 都 `logger.debug` 静默吞
  - 单次 HTTP timeout=30s 不符合"召回不阻断"的 5s 总预算

要求:
  - 部分成功时保留成功项(None 占位对应失败项)
  - 失败用 `logger.warning` 暴露
  - 失败时 call count == 1(不重试)
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest


def test_embed_texts_partial_success_keeps_non_null() -> None:
    """3 文本 batch，API 只回 2/3 → 应返回 3 元素 list，
    第 1 项 None、其余为非空向量(留成功项)。"""
    import json as _json
    from domain.memory.memory.recall.vector import _embed_texts

    # 构造真实合法 JSON（用列表构造,避免手写 2048 项）
    fake_payload = _json.dumps({
        "data": [
            {"index": 0, "embedding": [0.1] * 2048},
            {"index": 2, "embedding": [0.3] * 2048},
            # 故意缺失 index=1 → 留 None 占位
        ]
    }).encode("utf-8")

    fake_resp = MagicMock()
    fake_resp.read.return_value = fake_payload
    fake_resp.__enter__ = MagicMock(return_value=fake_resp)
    fake_resp.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=fake_resp), \
         patch("domain.memory.memory.recall.vector._get_api_key", return_value="fake-key"):
        result = _embed_texts(["a", "b", "c"])

    # 期望:列表长度 3;索引 1 为 None;其它非 None
    assert result is not None
    assert len(result) == 3
    assert result[1] is None
    assert result[0] is not None and len(result[0]) == 2048
    assert result[2] is not None and len(result[2]) == 2048


def test_embed_texts_429_no_retry_logger_warning(caplog: pytest.LogCaptureFixture) -> None:
    """API 429 → 必须:
       (1) 不重试(call count == 1)
       (2) warning 级别日志(非 debug)"""
    from domain.memory.memory.recall.vector import _embed_texts

    with patch("urllib.request.urlopen", side_effect=Exception("HTTP Error 429: Too Many Requests")) as mocked, \
         patch("domain.memory.memory.recall.vector._get_api_key", return_value="fake-key"):
        with caplog.at_level(logging.WARNING, logger="domain.memory.recall.vector"):
            result = _embed_texts(["hello"])

    assert mocked.call_count == 1, "MUST NOT retry; should fail-fast per FR-001 / FR-103"
    # 结果应是 None(全失败无回收)，或不阻塞消费(留 None 占位);
    # spec FR-103 不要求具体 None vs [];但 result 必须概念上"不可达"
    assert result is None or all(x is None for x in (result or []))

    # 必须在 WARNING 或更高级别记录(不能只是 debug 静默)
    assert any(
        record.levelno >= logging.WARNING
        for record in caplog.records
        if record.name == "domain.memory.recall.vector"
    ), "429 failure MUST surface as warning+, not silent debug"


def test_embed_texts_timeout_in_seconds_window(caplog: pytest.LogCaptureFixture) -> None:
    """单次 urlopen timeout 必须从 30s 缩到 8s。
       通过检查 urlopen 调用的 timeout 关键字参数。"""
    from domain.memory.memory.recall.vector import _embed_texts

    fake_exc = TimeoutError("urlopen timed out")
    with patch("urllib.request.urlopen", side_effect=fake_exc) as mocked, \
         patch("domain.memory.memory.recall.vector._get_api_key", return_value="fake-key"):
        with caplog.at_level(logging.WARNING, logger="domain.memory.recall.vector"):
            _embed_texts(["x"])

    # 第二个位置参数 or 关键字 timeout 必须为 8
    call_kwargs = mocked.call_args.kwargs if mocked.call_args.kwargs else {}
    call_args = mocked.call_args.args if mocked.call_args.args else ()
    timeout_value = call_kwargs.get("timeout")
    if timeout_value is None and len(call_args) >= 2:
        timeout_value = call_args[1]  # urlopen(url, timeout) 位置形式
    assert timeout_value == 8, (
        f"单次 HTTP timeout 必须为 8s(为整体召回 5s 上限让出余地);实际 {timeout_value}"
    )
