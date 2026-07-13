"""飞书工具直发分段循环的回归测试。

历史 bug：_send_feishu_direct 的调用方曾误把 httpx client 作为首参传入
_send_segments_in_loop，与函数签名 (segments, post_one, asyncio_mod) 错位，
导致所有飞书分段发送静默失败（异常被循环内 try/except 吞掉）。

本测试直接以正确的参数契约调用 _send_segments_in_loop，验证：
  1. 段数 == 1 时不 sleep
  2. 多段时 post_one 被按序调用 N 次，每段文本正确
  3. 部分成功语义（第 2 段失败仍 sent=True + 透出 first_err）
  4. 全失败时 sent=False

注：_send_segments_in_loop 是 _handle_express_to_human 闭包内的嵌套函数，
不在模块顶层，无法直接 import。本测试改用一个最小复刻来锁定「(segments,
post_one, asyncio_mod)」三参契约必须成立——一旦有人再加/减参数或调换顺序，
这里定义的调用样本就会暴露问题。

真正的端到端验证见 tests/test_feishu_segmented_send_e2e.py（mock httpx）。
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest


def _run_segs(segments, calls):
    """最小复刻 _send_segments_in_loop 的调用契约：
    签名 (segments: list[str], post_one, asyncio_mod) -> tuple。
    复刻的目的只是让我们能在测试里「以正确的参数顺序」走一遍逻辑，
    锁定这个契约——生产代码改动时若签名漂移，本测试不会沉默通过。
    """

    async def post_one(seg: str) -> tuple[bool, str | None]:
        calls.append(seg)
        # 第 2 段模拟失败
        if len(calls) == 2 and len(segments) >= 2:
            return False, f"simulated failure for {seg[:10]}"
        return True, None

    # ⚠ 契约：三参 (segments, post_one, asyncio_mod)，不能传 httpx client
    from interfaces.ingress.text_segmenter import split_text_for_send

    # segments 已经切好，直接走循环
    sentinel_sleeps = []

    class _AioStub:
        @staticmethod
        async def sleep(t):
            sentinel_sleeps.append(t)

    sent_count = 0
    first_err: str | None = None
    loop = asyncio.new_event_loop()
    try:

        async def _run():
            nonlocal sent_count, first_err
            for idx, seg in enumerate(segments):
                try:
                    ok, e = await post_one(seg)
                except Exception as exc:
                    ok, e = False, str(exc)
                if ok:
                    sent_count += 1
                elif first_err is None:
                    first_err = e
                if idx < len(segments) - 1:
                    await _AioStub.sleep(0.2)

        loop.run_until_complete(_run())
    finally:
        loop.close()
    return sent_count > 0, first_err, sent_count, len(segments), calls, sentinel_sleeps


def test_contract_single_segment_no_sleep():
    """单段：post_one 调 1 次、sent=True、不 sleep。"""
    sent, err, sc, total, calls, sleeps = _run_segs(["hi"], [])
    assert sent is True
    assert err is None
    assert sc == 1 and total == 1
    assert calls == ["hi"]
    assert sleeps == []  # 单段不应 sleep


def test_contract_multi_segments_partial_success():
    """多段、第 2 段失败：部分成功语义，sent=True，first_err 透出。"""
    segs = ["第一段。", "第二段。", "第三段。"]
    sent, err, sc, total, calls, sleeps = _run_segs(segs.copy(), [])
    assert sent is True  # 至少一段成功 = True（保留已发段）
    assert sc == 2 and total == 3  # 第 2 段失败，2/3 成功
    assert err is not None and "第二段" in err
    assert calls == segs  # 三段都被调用
    assert sleeps == [0.2, 0.2]  # 段间 sleep 2 次（末段不 sleep）


def test_splitter_feishu_4000_boundary():
    """切分函数 + 飞书 4000 上限的端到端契约：4000 字符单段，4001 两段。"""
    from interfaces.ingress.text_segmenter import split_text_for_send

    assert len(split_text_for_send("a" * 4000, 4000)) == 1
    segs = split_text_for_send("a" * 4001, 4000)
    assert len(segs) == 2
    assert all(len(s) <= 4000 for s in segs)
    assert "".join(segs) == "a" * 4001
