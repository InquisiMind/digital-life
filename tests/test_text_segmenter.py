"""split_text_for_send 切分逻辑单测。

覆盖：短文本 / 换行切 / 句末标点切 / 硬切 / 空串 / 边界长度 /
拼接无损 / max_length 兜底。
"""
from __future__ import annotations

import pytest

from interfaces.ingress.text_segmenter import split_text_for_send


def test_short_text_single_segment():
    assert split_text_for_send("hi", 100) == ["hi"]


def test_empty_string_returns_one_empty_segment():
    # 空串仍返回非空 list，调用方决定是否发
    assert split_text_for_send("", 100) == [""]


def test_exact_max_length_single_segment():
    text = "a" * 50
    assert split_text_for_send(text, 50) == [text]


def test_newline_preferred_split_point():
    # 换行位置在窗口内 → 在换行处切，换行留在前段
    text = "first line\nsecond line"
    segs = split_text_for_send(text, 20)  # 整段 22 > 20
    assert len(segs) == 2
    assert segs[0] == "first line\n"
    assert segs[1] == "second line"
    # 拼接无损
    assert "".join(segs) == text


def test_paragraph_double_newline_split():
    text = "para one\n\npara two"
    segs = split_text_for_send(text, 15)
    assert len(segs) == 2
    assert segs[0] == "para one\n\n"
    assert "".join(segs) == text


def test_sentence_end_split_when_no_newline():
    # 没有换行 → 在句末标点处切（标点留在前段）
    text = "你好。世界。"
    # 上限 4：足够触发两次句号切分
    segs = split_text_for_send(text, 4)
    assert "".join(segs) == text
    assert all(len(s) <= 4 for s in segs)
    assert segs[0] == "你好。"


def test_hard_cut_when_no_break_in_window():
    # 窗口内无任何可断点 → 硬切在 max_length
    text = "abcdefghij" * 10  # 100 字符，无标点无换行
    segs = split_text_for_send(text, 30)
    assert "".join(segs) == text
    assert all(len(s) <= 30 for s in segs)


@pytest.mark.parametrize("max_len", [1, 2, 5, 10, 33, 100])
def test_concat_lossless_and_length_bound(max_len):
    text = (
        "第一段较长的内容用于测试切分。\n"
        "第二段也很长，需要多次切。这里加一些标点。"
        "然后是没有换号的连续中文串测试硬切兜底路径abcdefghij"
        "klmnopqrstuvwxyz0123456789继续拼接凑长一点。\n\n"
        "末尾段。"
    )
    segs = split_text_for_send(text, max_len)
    # 拼接无损
    assert "".join(segs) == text
    # 每段不超限
    assert all(len(s) <= max_len for s in segs)
    # 至少一段
    assert len(segs) >= 1


def test_max_length_below_one_clamped():
    text = "abc"
    # max_length=0 按 1 兜底，每个字符一段
    segs = split_text_for_send(text, 0)
    assert segs == ["a", "b", "c"]


def test_long_realistic_feishu_text():
    # 模拟一条会被飞书 4000 上限切的回复
    para = "这是一段模拟回复。包含若干句子。还有换行。\n下一行继续。"
    text = para * 200  # 远超 4000
    segs = split_text_for_send(text, 4000)
    assert "".join(segs) == text
    assert all(len(s) <= 4000 for s in segs)
    assert len(segs) > 1
