"""工具调用参数的流式增量解析（语音提前播报用）。

背景（2026-10-28 zhp 提议）：工具调用通常要等模型输出完整 JSON 才能解析执行；
语音对话里 express_to_human 的 ``text`` 参数边生成边可用——在流式 SSE 上增量
解码出"已完成的前缀"，按句子边界提前喂 TTS，首句发声时间 ≈ 模型写出首句的
时刻，而不是整个响应结束。

只做一件事：从**累积的** arguments JSON 片段里，安全地解码 ``text`` 字段的最长
前缀。安全 = 不会停在转义序列中间（``\\"`` / ``\\uXXXX`` 被切断时回退到上一个
边界），因此前缀随输入增长单调不减，可作"TTS 已播前缀长度"的记账依据。
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# 句末标点：完整句（立即出）。换行也当句界（模型偶尔分行）。
_SENTENCE_END = "。！？!?；;"
# 缓冲内软句界（逗号/顿号）：攒够 min_soft_len 字才切，避免碎句连播
_SOFT_BREAK = "，、,"
_TEXT_KEY_RE = re.compile(r'"text"\s*:\s*"')

# \u 转义按 4 个 hex 解码
_SIMPLE_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


def safe_text_prefix(raw_args: str) -> str | None:
    """从累积的 arguments JSON 文本里解码 ``text`` 字段的最长安全前缀。

    返回 None 表示还没流到 text 值（找不到完整的 ``"text":`` 开引号）。
    字符串已闭合（遇到未转义 ``"``）时返回到此为止的完整值。
    """
    m = _TEXT_KEY_RE.search(raw_args)
    if m is None:
        return None
    i = m.end()                     # 开引号后第一个字符
    out: list[str] = []
    n = len(raw_args)
    while i < n:
        ch = raw_args[i]
        if ch == '"':
            break                   # 字符串闭合
        if ch == "\\":
            if i + 1 >= n:
                break               # 转义被切断：回退到上一个安全边界
            esc = raw_args[i + 1]
            if esc == "u":
                hex_part = raw_args[i + 2:i + 6]
                if len(hex_part) < 4 or any(c not in "0123456789abcdefABCDEF" for c in hex_part):
                    break           # \u 不完整：回退
                out.append(chr(int(hex_part, 16)))
                i += 6
                continue
            out.append(_SIMPLE_ESCAPES.get(esc, esc))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


class TextFieldSentenceStream:
    """喂 arguments 片段 → 按句吐出已完成的 text 前缀句子。

    用法::

        stream = TextFieldSentenceStream()
        for frag in sse_argument_fragments:
            stream.feed(frag)
            for sentence in stream.pop_sentences():
                tts_speak(sentence)          # 提前播报
        # 流结束后剩余未成句的尾巴不吐——由工具正式执行时播（见
        # voice prespeak offset：_send_voice_local 剥离已播前缀）
    """

    def __init__(self, *, min_soft_len: int = 18) -> None:
        self._args = ""
        self._min_soft_len = min_soft_len
        self._pending = ""          # 已解码、未成句的缓冲
        self._emitted_chars = 0     # 已吐出总字符数（= prespeak offset）

    @property
    def emitted_chars(self) -> int:
        return self._emitted_chars

    @property
    def raw_args(self) -> str:
        """已积累的原始 arguments 文本（目标甄别用：channel/chat_id 甄别提前播报）。"""
        return self._args

    def feed(self, fragment: str) -> None:
        self._args += fragment

    def pop_sentences(self) -> list[str]:
        prefix = safe_text_prefix(self._args)
        if prefix is None:
            return []
        # 已见过的解码文本 = 已吐出 + 待成句缓冲；只处理新增量
        seen = self._emitted_chars + len(self._pending)
        if len(prefix) <= seen:
            return []
        self._pending += prefix[seen:]

        sentences: list[str] = []
        while self._pending:
            cut = -1
            for idx, ch in enumerate(self._pending):
                if ch in _SENTENCE_END or ch == "\n":
                    cut = idx + 1
                    break
                if ch in _SOFT_BREAK and idx + 1 >= self._min_soft_len:
                    cut = idx + 1
                    break
            if cut <= 0:
                break
            sentence = self._pending[:cut].strip()
            self._pending = self._pending[cut:]
            # offset 记账按原始前缀长度推进（含 strip 掉的空白），
            # 与 _send_voice_local 的 text[offset:] 切片严格对齐
            self._emitted_chars += cut
            if sentence:
                sentences.append(sentence)
        return sentences
