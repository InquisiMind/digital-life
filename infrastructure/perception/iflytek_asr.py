"""科大讯飞 语音听写（IAT）流式版 Websocket 客户端。

API 文档：https://www.xfyun.cn/doc/asr/voicedictation/API.html
协议：wss://iat-api.xfyun.cn/v2/iat

音频要求：
  - 格式：raw PCM (L16)
  - 采样率：16000 Hz
  - 声道：mono
  - 位深：16-bit

本模块只负责单段音频转写，分段调度仍由 asr.py 的 transcribe_file 负责。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import struct
import time
import wave
from pathlib import Path
from typing import Any

import websocket  # websocket-client

logger = logging.getLogger(__name__)

# ── 常量 ────────────────────────────────────────────────────────────────────
IAT_HOST = "iat-api.xfyun.cn"
IAT_PATH = "/v2/iat"
IAT_URL = f"wss://{IAT_HOST}{IAT_PATH}"

# 帧状态
STATUS_FIRST_FRAME = 0
STATUS_CONTINUE_FRAME = 1
STATUS_LAST_FRAME = 2

# 每帧音频大小（字节）。16kHz × 16bit × 1ch = 32000 B/s
# 1280 bytes = 40ms，讯飞推荐
FRAME_SIZE = 1280
# 发送间隔（秒），避免过快
FRAME_INTERVAL = 0.04

# 响应超时
RECV_TIMEOUT = 30


def _build_auth_url(api_key: str, api_secret: str) -> str:
    """生成讯飞 Websocket 鉴权 URL。

    讯飞要求 date 为 RFC 1123 格式（如 "Mon, 17 Aug 2026 09:22:04 GMT"）。
    """
    from email.utils import formatdate
    # RFC 1123 format date, use UTC
    date_str = formatdate(timeval=time.time(), usegmt=True)
    signature_origin = (
        f"host: {IAT_HOST}\n"
        f"date: {date_str}\n"
        f"GET {IAT_PATH} HTTP/1.1"
    )
    signature_sha = hmac.new(
        api_secret.encode("utf-8"),
        signature_origin.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    signature = base64.b64encode(signature_sha).decode("utf-8")
    authorization_origin = (
        f'api_key="{api_key}", '
        f'algorithm="hmac-sha256", '
        f'headers="host date request-line", '
        f'signature="{signature}"'
    )
    authorization = base64.b64encode(
        authorization_origin.encode("utf-8")
    ).decode("utf-8")
    # URL-encode the date and authorization for the query string
    from urllib.parse import quote
    return (
        f"{IAT_URL}"
        f"?authorization={quote(authorization)}"
        f"&date={quote(date_str)}"
        f"&host={IAT_HOST}"
    )


def _wav_to_pcm(wav_bytes: bytes) -> bytes:
    """从 WAV 字节中提取 raw PCM 数据。"""
    import io
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate()
        n_frames = wf.getnframes()
        pcm = wf.readframes(n_frames)

    # 讯飞要求 16kHz/16-bit/mono
    # 如果已经是兼容格式，直接返回
    if n_channels == 1 and sampwidth == 2 and framerate == 16000:
        return pcm

    # 需要重采样 —— 用 audioop 降混 + 线性插值
    import audioop

    # 多声道 → mono
    if n_channels > 1:
        pcm = audioop.tomono(pcm, sampwidth, 1.0, 1.0)

    # 8-bit → 16-bit
    if sampwidth == 1:
        pcm = audioop.lin2lin(pcm, 1, 2)
    elif sampwidth == 4:
        pcm = audioop.lin2lin(pcm, 4, 2)
    elif sampwidth != 2:
        raise ValueError(f"不支持的采样位宽: {sampwidth}")

    # 重采样到 16kHz
    if framerate != 16000:
        pcm, _ = audioop.ratecv(pcm, 2, 1, framerate, 16000, None)

    return pcm


def _extract_text(resp_data: dict[str, Any]) -> tuple[str, str]:
    """从讯飞响应 JSON 提取识别文本和 pgs 类型。

    返回 (text, pgs)：
    - pgs="rpl": text 是累积替换文本（从此条往前到 rg[0] 的所有结果被替换）
    - pgs="apd": text 是新增文本（追加到之前结果之后）
    """
    data = resp_data.get("data") or {}
    result = data.get("result") or {}
    pgs = result.get("pgs") or "apd"
    ws_list = result.get("ws") or []
    parts: list[str] = []
    for ws in ws_list:
        cw_list = ws.get("cw") or []
        for cw in cw_list:
            w = cw.get("w") or ""
            if w:
                parts.append(w)
    return "".join(parts), pgs


def transcribe_iflytek(
    wav_bytes: bytes,
    *,
    app_id: str,
    api_key: str,
    api_secret: str,
    language: str = "zh_cn",
    accent: str = "mandarin",
    hotwords: tuple[str, ...] = (),
) -> str:
    """调讯飞 IAT 流式 ASR，返回完整文本。

    Args:
        wav_bytes: WAV 格式音频字节（会自动转 raw PCM 16kHz/16-bit/mono）
        app_id: 讯飞应用 ID
        api_key: 讯飞 API Key
        api_secret: 讯飞 API Secret
        language: 语言（默认 zh_cn）
        accent: 口音（默认 mandarin）
        hotwords: 热词列表

    Returns:
        识别文本。失败抛异常。
    """
    pcm = _wav_to_pcm(wav_bytes)
    auth_url = _build_auth_url(api_key, api_secret)

    # 热词参数
    pd_param = {}
    if hotwords:
        pd_param["pd"] = "tech"
        # 讯飞热词需要在控制台配置，这里只传 personal_param
    common = {"app_id": app_id}
    business = {
        "language": language,
        "domain": "iat",
        "accent": accent,
        "vad_eos": 5000,
        "dwa": "wpgs",  # 动态修正
        **pd_param,
    }

    # 讯飞 IAT：发送全部音频帧，然后读取响应直到 status==2
    latest_text: str = ""
    ws = websocket.create_connection(auth_url, timeout=10)

    try:
        # Phase 1: 快速发送全部音频帧（不读取响应）
        offset = 0
        is_first = True
        frame_count = 0
        while offset < len(pcm):
            chunk = pcm[offset:offset + FRAME_SIZE]
            status = STATUS_FIRST_FRAME if is_first else STATUS_CONTINUE_FRAME

            frame_data: dict[str, Any] = {"data": {
                "status": status,
                "format": "audio/L16;rate=16000",
                "audio": base64.b64encode(chunk).decode("utf-8"),
                "encoding": "raw",
            }}
            if is_first:
                frame_data["common"] = common
                frame_data["business"] = business

            ws.send(json.dumps(frame_data))
            is_first = False
            offset += FRAME_SIZE
            frame_count += 1
            time.sleep(FRAME_INTERVAL)  # 40ms per frame, rate-limited

        # Phase 2: 发送结束帧
        end_frame = {"data": {
            "status": STATUS_LAST_FRAME,
            "format": "audio/L16;rate=16000",
            "audio": "",
            "encoding": "raw",
        }}
        ws.send(json.dumps(end_frame))

        # Phase 3: 读取所有响应直到 status==2
        ws.settimeout(10.0)
        while True:
            try:
                resp = ws.recv()
                if not resp:
                    break
                resp_json = json.loads(resp)
                code = resp_json.get("code", -1)
                if code != 0:
                    raise RuntimeError(
                        f"讯飞 ASR 错误: code={code}, "
                        f"msg={resp_json.get('message', '')}"
                    )
                text, pgs = _extract_text(resp_json)
                if text:
                    if pgs == "rpl":
                        latest_text = text
                    else:  # apd
                        latest_text += text
                data = resp_json.get("data") or {}
                if data.get("status") == 2:
                    break
            except websocket.WebSocketTimeoutException:
                break

    finally:
        ws.close()

    return latest_text.strip()
