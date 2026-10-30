"""科大讯飞 实时语音转写大模型版（RTASR LLM）流式 WebSocket 客户端。

API 文档: https://www.xfyun.cn/doc/spark/asr_llm/rtasr_llm.html

协议要点（2026-10-28 真实凭据实测）：
  - 端点 ``wss://office-api-ast-dx.iflyaisol.com/ast/communicate/v1``
  - 鉴权：除 signature 外全部参数按 key 升序、键值 URL 编码后拼 ``k=v&...``，
    以 accessKeySecret 做 HmacSHA1 → base64 → signature（query 里也要 URL 编码）。
    **注意**：accessKeyId = 控制台 APIKey，accessKeySecret = APISecret
    （2026-10-28 实测两个值与直觉相反，配置时看注释别按记忆填）。
  - 必须等 ``started`` 消息后才能推音频，否则 37011 Text message not json format。
  - 音频：16k/16bit/mono raw PCM，二进制帧，建议 1280B/40ms；
    15s 不发音频断连（37005）→ 空闲时补静音帧保活。
  - 结果 ``msg_type=result``：
      ``data.cn.st.type="1"`` 中间结果（当前句累积全量，适合实时字幕）
      ``data.cn.st.type="0"`` 确认句（bg/ed 区间不重叠 → **天然断句点**，
      逐句回调，客户端无需自己做 VAD 断句）
  - 结束：发 ``{"end": true, "sessionId": ...}``，服务端补发尾句后 ``action=end``。
  - 单会话 ≤8h；发送过快可能 100001。

线程模型（对齐 iflytek_asr.IflytekStreamingSession 的使用姿势）：
  调用方线程   start() / feed(pcm) / finish()
  发送线程     攒 1280B 帧 + 实时转发 + 空闲静音帧保活
  接收线程     解析 result/started/end/error → 回调（调用方线程外的线程里执行）
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import queue
import threading
import time
import uuid as uuid_mod
import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import websocket  # websocket-client

logger = logging.getLogger(__name__)

RTASR_LLM_URL = "wss://office-api-ast-dx.iflyaisol.com/ast/communicate/v1"

# 16k × 16bit × mono = 32000 B/s；1280B = 40ms（讯飞推荐帧大小）
FRAME_SIZE = 1280
# 空闲保活间隔（秒）。服务端 15s 无音频断连，8s 补一帧静音留足余量。
KEEPALIVE_INTERVAL = 8.0
# 断线自动重连间隔
RECONNECT_INTERVAL = 2.0

# type=0 句首冗余标点（分段边界常把上一句末的逗号放到下一句开头）
_LEADING_PUNCT = "，。！？、,.!?；;：: "


def build_auth_url(app_id: str, access_key_id: str, access_key_secret: str) -> str:
    """构造带签名的 WebSocket URL（每连接一个 uuid 防签名重放）。"""
    utc = datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=8))
    ).strftime("%Y-%m-%dT%H:%M:%S%z")
    params = {
        "appId": app_id,
        "accessKeyId": access_key_id,
        "uuid": uuid_mod.uuid4().hex,
        "utc": utc,
        "audio_encode": "pcm_s16le",
        "lang": "autodialect",
        "samplerate": "16000",
    }
    base_string = "&".join(
        f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in sorted(params.items())
    )
    sign = base64.b64encode(
        hmac.new(access_key_secret.encode(), base_string.encode(), hashlib.sha1).digest()
    ).decode()
    return f"{RTASR_LLM_URL}?{base_string}&signature={quote(sign, safe='')}"


def extract_message_text(data: dict[str, Any]) -> tuple[str, str]:
    """从一条 result 消息提取 ``(文本, st.type)``。

    - ``type="1"``：中间结果，文本 = 当前正在说的这句话的累积全量
    - ``type="0"``：确认句，文本 = 该 bg/ed 区间（一句话）的最终转写
    """
    st = ((data.get("data") or {}).get("cn") or {}).get("st") or {}
    words: list[str] = []
    for rt in st.get("rt") or []:
        for ws in rt.get("ws") or []:
            for cw in ws.get("cw") or []:
                w = str(cw.get("w") or "")
                if w:
                    words.append(w)
    return "".join(words), str(st.get("type") or "")


def clean_sentence(text: str) -> str:
    """确认句清理：去句首冗余标点/空白（分段边界的逗号漂移）。"""
    return (text or "").lstrip(_LEADING_PUNCT).strip()


class RtasrLlmStream:
    """一次网页语音会话 = 一个 RTASR LLM 流（断线自动重连续传）。

    用法::

        stream = RtasrLlmStream(
            app_id=..., access_key_id=..., access_key_secret=...,
            on_partial=lambda t: ...,   # 实时字幕（调用方保证线程安全）
            on_sentence=lambda t: ...,  # 成句回调（断句点）
        )
        if stream.start():
            for chunk in pcm_chunks:
                stream.feed(chunk)
            stream.finish()
        # start 失败 / 运行中出错 → stream.ok 为 False，调用方降级或提示
    """

    def __init__(
        self,
        *,
        app_id: str,
        access_key_id: str,
        access_key_secret: str,
        on_partial: Callable[[str], None] | None = None,
        on_sentence: Callable[[str], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self._creds = (app_id, access_key_id, access_key_secret)
        self._on_partial = on_partial
        self._on_sentence = on_sentence
        self._on_error = on_error

        self._send_queue: "queue.Queue[bytes | None]" = queue.Queue()
        self._ws: websocket.WebSocket | None = None
        self._ws_lock = threading.Lock()
        self._session_id: str | None = None
        self._started = threading.Event()
        self._closed = threading.Event()
        self._finished = False
        self._error: str | None = None
        self._send_thread: threading.Thread | None = None
        self._recv_thread: threading.Thread | None = None
        self._last_sent_at = 0.0

    # ── 生命周期 ────────────────────────────────────────────────────────────

    @property
    def ok(self) -> bool:
        return self._error is None

    def start(self) -> bool:
        """建立连接并等待 ``started``。失败返回 False（调用方降级）。"""
        if not self._open():
            return False
        if not self._started.wait(timeout=10):
            self._error = "wait started timeout"
            self._close_ws()
            return False
        self._send_thread = threading.Thread(
            target=self._send_loop, name="rtasr-send", daemon=True
        )
        self._send_thread.start()
        return True

    def feed(self, pcm: bytes) -> None:
        """推一块 raw PCM（16k/16bit/mono，任意长度；内部攒 1280B 帧）。"""
        if self._finished or self._error or not pcm:
            return
        self._send_queue.put(pcm)

    def finish(self, timeout: float = 4.0) -> None:
        """结束流：发 end 标记 → 等尾句 → 关闭。幂等。"""
        if self._finished:
            return
        self._finished = True
        self._send_queue.put(None)  # 发送线程哨兵：先 flush 尾帧再发 end
        self._closed.wait(timeout=timeout)
        self._close_ws()

    # ── 内部：连接管理 ──────────────────────────────────────────────────────

    def _open(self) -> bool:
        try:
            ws = websocket.create_connection(
                build_auth_url(*self._creds), timeout=10, enable_multithread=True
            )
            ws.settimeout(5.0)
        except Exception as exc:
            logger.warning("rtasr connect failed: %s", exc)
            self._error = f"connect: {exc}"
            self._fire_error(self._error)
            return False
        with self._ws_lock:
            self._ws = ws
            self._started.clear()
        self._recv_thread = threading.Thread(
            target=self._recv_loop, args=(ws,), name="rtasr-recv", daemon=True
        )
        self._recv_thread.start()
        return True

    def _reconnect(self) -> bool:
        """断线重连（新会话，云端丢上下文；转写流本身是独立的，可接受）。"""
        self._close_ws()
        if self._finished or self._error:
            return False
        logger.info("rtasr reconnecting...")
        if not self._open():
            return False
        return self._started.wait(timeout=10)

    def _close_ws(self) -> None:
        with self._ws_lock:
            ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        self._closed.set()

    def _fire_error(self, msg: str) -> None:
        if self._on_error:
            try:
                self._on_error(msg)
            except Exception:
                logger.exception("rtasr on_error callback failed")

    # ── 内部：发送线程（攒帧 + 实时转发 + 静音保活 + 断线重连）──────────────

    def _send_loop(self) -> None:
        buf = b""
        while True:
            try:
                item = self._send_queue.get(timeout=0.5)
            except queue.Empty:
                item = None  # 超时 → 检查保活
            if item is None and self._finished:
                # 结束：flush 尾帧（补零到整帧）+ 发 end 标记
                if buf:
                    self._send_binary(buf + b"\x00" * (FRAME_SIZE - len(buf)))
                    buf = b""
                self._send_end()
                return
            if item is not None:
                buf += item
            while len(buf) >= FRAME_SIZE:
                if not self._send_binary(buf[:FRAME_SIZE]):
                    buf = b""
                    return
                buf = buf[FRAME_SIZE:]
            # 空闲保活：静音环境下服务端 15s 断连，补一帧 0
            now = time.time()
            if (not buf) and now - self._last_sent_at >= KEEPALIVE_INTERVAL:
                self._send_binary(b"\x00" * FRAME_SIZE)

    def _send_binary(self, frame: bytes) -> bool:
        with self._ws_lock:
            ws = self._ws
        if ws is None or not self._started.is_set():
            return True  # 未就绪：丢帧（只发生在重连窗口，语音流短缺口可接受）
        try:
            ws.send_binary(frame)
            self._last_sent_at = time.time()
            return True
        except Exception as exc:
            logger.warning("rtasr send failed: %s → reconnect", exc)
            time.sleep(RECONNECT_INTERVAL)
            if not self._reconnect():
                self._error = f"send: {exc}"
                self._fire_error(self._error)
                self._closed.set()
                return False
            try:
                ws2 = self._ws
                if ws2 is not None:
                    ws2.send_binary(frame)
                    self._last_sent_at = time.time()
                return True
            except Exception as exc2:
                self._error = f"send-after-reconnect: {exc2}"
                self._fire_error(self._error)
                self._closed.set()
                return False

    def _send_end(self) -> None:
        with self._ws_lock:
            ws = self._ws
        if ws is None:
            return
        try:
            ws.send(json.dumps({"end": True, "sessionId": self._session_id or ""}))
        except Exception as exc:
            logger.debug("rtasr send end failed (non-fatal): %s", exc)

    # ── 内部：接收线程 ──────────────────────────────────────────────────────

    def _recv_loop(self, ws) -> None:
        while True:
            with self._ws_lock:
                current = self._ws
            if current is not ws or ws is None:
                return  # 已被重连/关闭替换
            try:
                msg = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except Exception:
                # 对端关闭/网络断：接收线程退出即可——发送线程的 _send_binary
                # 会发现并统一重连（旧版 recv 也调 _reconnect 造成双线程竞争
                # 互踩：send 刚建好的新连被 recv 再 close 再建）
                return
            if not msg:
                continue
            if isinstance(msg, bytes):
                continue
            try:
                data = json.loads(msg)
            except Exception:
                continue

            msg_type = data.get("msg_type")
            payload = data.get("data") or {}
            if msg_type == "action":
                action = payload.get("action")
                if action == "started":
                    self._session_id = payload.get("sessionId")
                    self._started.set()
                elif action == "end":
                    logger.info("rtasr session ended by server (sid=%s)", self._session_id)
                    self._closed.set()
                    return
                elif str(payload.get("code") or ""):
                    # action=end 带 code = 错误结束（如 37011/37005）
                    err = f"rtasr code={payload.get('code')}: {payload.get('message', '')}"
                    logger.warning("%s", err)
                    self._fire_error(err)
                    self._closed.set()
                    return
            elif msg_type == "error":
                err = f"rtasr error: {json.dumps(payload, ensure_ascii=False)[:200]}"
                self._error = err
                self._fire_error(err)
                self._closed.set()
                return
            elif msg_type == "result":
                text, st_type = extract_message_text(data)
                if not text:
                    continue
                if st_type == "1" and self._on_partial:
                    try:
                        self._on_partial(text)
                    except Exception:
                        logger.exception("rtasr on_partial callback failed")
                elif st_type == "0":
                    sentence = clean_sentence(text)
                    if sentence and self._on_sentence:
                        # st 不在当前作用域——从 data 重新提取 bg/ed 时间戳
                        _st = (((data.get("data") or {}).get("cn") or {}).get("st") or {})
                        bg = int(_st.get("bg") or 0)
                        ed = int(_st.get("ed") or 0)
                        try:
                            self._on_sentence(sentence, bg, ed)
                        except TypeError:
                            # 兼容旧签名 on_sentence(text)
                            try:
                                self._on_sentence(sentence)
                            except Exception:
                                logger.exception("rtasr on_sentence callback failed")
                        except Exception:
                            logger.exception("rtasr on_sentence callback failed")


def load_pcm(wav_path: str | Path) -> bytes:
    """读 16k/16bit/mono wav 的 raw PCM（冒烟/测试用）。"""
    import wave

    with wave.open(str(wav_path), "rb") as wf:
        return wf.readframes(wf.getnframes())
