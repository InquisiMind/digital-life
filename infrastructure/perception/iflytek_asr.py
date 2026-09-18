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
import threading
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


# ── 流式会话（边录边推） ──────────────────────────────────────────────────────
# zhp 2026-09-16 拍板：录音开始即开 WS，音频实时推流，松手时服务端已识别完，
# 端到端延迟 <1s。替代"录完→整段重放推帧"（10s 音频要再花 10s 发送）。
#
# 60s 上限：IAT 单会话音频 ≤60s（#160），_MAX_SESSION_SECONDS 到点自动滚动
# 新会话，文本拼接，滚动期间音频块暂存不丢。

_MAX_SESSION_SECONDS = 55.0  # 留 5s 余量给尾帧


class IflytekStreamingSession:
    """一次录音 = 一个（或滚动多个）讯飞 IAT WS 会话。

    用法（音频采集线程）：
        sess = IflytekStreamingSession(app_id=..., api_key=..., api_secret=...)
        if sess.start():
            for chunk in mic_chunks:
                sess.feed(chunk)          # raw PCM 16k/16bit/mono
            text = sess.finish()          # 松手即得，<1s
        # start 失败或中途出错 → sess.ok False → 调用方降级走文件整段转写
    """

    def __init__(
        self,
        *,
        app_id: str,
        api_key: str,
        api_secret: str,
        language: str = "zh_cn",
        accent: str = "mandarin",
        hotwords: tuple[str, ...] = (),
    ) -> None:
        self._creds = (app_id, api_key, api_secret)
        self._business = {
            "language": language,
            "domain": "iat",
            "accent": accent,
            "vad_eos": 5000,
            # 不开 dwa/wpgs：wpgs 的 rpl 是跨句显示缓冲替换，与滚动拼接语义冲突；
            # 我们只要松手时的最终文本，纯 append 结果更稳（教训 9/16 实测）
        }
        if hotwords:
            self._business["pd"] = "tech"

        self._ws = None
        self._recv_thread = None
        self._lock = threading.Lock()
        self._text = ""                 # 当前会话累积（rpl 整段替换）
        self._head = ""                 # 已滚动定稿的前段文本
        self._error: str | None = None
        self._first_sent = False        # 当前会话是否已发首帧
        self._buf = b""                 # 攒帧 buffer（<1280B 尾块）
        self._pending = b""             # 滚动换会话期间暂存的音频
        self._fed_seconds = 0.0         # 当前会话已推秒数
        self._session_done = threading.Event()  # 当前会话收到最终结果/出错
        self._finished = False

    # ── 生命周期 ────────────────────────────────────────────────────────────

    @property
    def ok(self) -> bool:
        return self._error is None

    def start(self) -> bool:
        """建立 WS + 启动接收线程。失败返回 False（调用方降级）。"""
        if self._error:
            return False
        return self._open_session()

    def _open_session(self) -> bool:
        try:
            auth_url = _build_auth_url(self._creds[1], self._creds[2])
            ws = websocket.create_connection(auth_url, timeout=10)
            ws.settimeout(5.0)
        except Exception as exc:
            logger.warning("streaming session connect failed: %s", exc)
            self._error = f"connect: {exc}"
            return False

        with self._lock:
            self._ws = ws
            self._first_sent = False
            self._fed_seconds = 0.0
            self._session_done.clear()
            self._recv_thread = threading.Thread(
                target=self._recv_loop, args=(ws,), daemon=True
            )
            self._recv_thread.start()
        return True

    def feed(self, pcm: bytes) -> None:
        """推一块 raw PCM。任何出错后静默 no-op（调用方 finish 时查 ok）。"""
        if self._error or self._finished:
            return

        # 60s 滚动：到点先结束当前会话再开新会话，期间音频进 _pending
        if self._fed_seconds >= _MAX_SESSION_SECONDS:
            self._rollover()

        if self._error:
            return
        if self._pending:
            pcm, self._pending = self._pending + pcm, b""

        data = self._buf + pcm
        self._buf = b""
        offset = 0
        n = len(data)
        while offset + FRAME_SIZE <= n:
            self._recover_and_retry(data[offset:offset + FRAME_SIZE], STATUS_CONTINUE_FRAME)
            offset += FRAME_SIZE
        self._buf = data[offset:]

    def _snapshot(self) -> None:
        """定稿快照：当前会话文本并入 _head，新会话从零累积。"""
        with self._lock:
            if self._text:
                self._head += self._text
                self._text = ""

    def _hard_close_ws(self) -> None:
        with self._lock:
            ws, self._ws = self._ws, None
        if ws:
            try:
                ws.close()
            except Exception:
                pass

    def _recover_and_retry(self, pcm: bytes, status: int) -> None:
        """韧性发送：断链则快照已识别文本 → 重连 → 重发本帧；再败才置错。"""
        try:
            self._send_frame(pcm, status)
            return
        except ConnectionError as exc:
            logger.info("ws dropped (%s), snapshot + reconnect + resend", exc)
            if self._finished:
                self._error = f"send: {exc}"
                return
            self._hard_close_ws()
            self._snapshot()
            if not self._open_session():
                return  # connect 失败已置错
            try:
                self._send_frame(pcm, status)
            except ConnectionError as exc2:
                self._error = f"send-after-reconnect: {exc2}"

    def _rollover(self) -> None:
        """60s 到点：发结束帧 → 等 ≤1.5s 最终结果 → 快照 → 换新会话。

        结束帧发送失败不视为错误（音频已实时推完），直接快照重连续推。
        """
        try:
            self._send_frame(b"", STATUS_LAST_FRAME)
            self._session_done.wait(timeout=1.5)
        except Exception as exc:
            logger.info("rollover end-frame: %s (continue anyway)", exc)
        self._hard_close_ws()
        self._snapshot()
        if not self._open_session():
            return
        # 新会话重置成功，_pending 由下一次 feed flush
        logger.info("streaming session rolled over at %.0fs", self._fed_seconds)

    def _send_frame(self, pcm: bytes, status: int) -> None:
        with self._lock:
            ws = self._ws
        if ws is None:
            raise ConnectionError("ws closed")
        frame: dict[str, Any] = {"data": {
            "status": status,
            "format": "audio/L16;rate=16000",
            "audio": base64.b64encode(pcm).decode("utf-8") if pcm else "",
            "encoding": "raw",
        }}
        if not self._first_sent and status == STATUS_CONTINUE_FRAME:
            status = STATUS_FIRST_FRAME
            frame["data"]["status"] = status
            frame["common"] = {"app_id": self._creds[0]}
            frame["business"] = self._business
        try:
            ws.send(json.dumps(frame))
        except Exception as exc:
            logger.warning("send frame failed: %s", exc)
            raise ConnectionError(str(exc)) from exc
        if frame["data"]["status"] == STATUS_FIRST_FRAME:
            self._first_sent = True
        self._fed_seconds += len(pcm) / 32000.0  # 16k*16bit*mono = 32000 B/s

    def finish(self, timeout: float = 5.0) -> str:
        """松手调用：发结束帧 + 等最终文本。返回累积文本（可能为空）。"""
        if self._finished:
            return self._text.strip()
        self._finished = True
        if self._error:
            self._close()
            return (self._head + self._text).strip()
        # 尾块不足一帧也推出去
        if self._buf:
            tail, self._buf = self._buf, b""
            self._recover_and_retry(tail, STATUS_CONTINUE_FRAME)
        self._recover_and_retry(b"", STATUS_LAST_FRAME)
        self._session_done.wait(timeout=timeout)
        self._close()
        return (self._head + self._text).strip()

    def _close(self) -> None:
        with self._lock:
            if self._ws:
                try:
                    self._ws.close()
                except Exception:
                    pass
                self._ws = None

    # ── 接收线程（每会话一个） ──────────────────────────────────────────────

    def _recv_loop(self, ws) -> None:
        while True:
            try:
                resp = ws.recv()
                if not resp:
                    break
                resp_json = json.loads(resp)
                code = resp_json.get("code", -1)
                if code != 0:
                    with self._lock:
                        self._error = f"xfyun code={code}: {resp_json.get('message', '')}"
                    break
                text, pgs = _extract_text(resp_json)
                if text:
                    with self._lock:
                        if pgs == "rpl":
                            self._text = text
                        else:
                            self._text += text
                data = resp_json.get("data") or {}
                if data.get("status") == 2:
                    break
            except websocket.WebSocketTimeoutException:
                continue  # 静默间隙，会话仍活着
            except Exception:
                break  # 会话关闭/网络断
        self._session_done.set()
