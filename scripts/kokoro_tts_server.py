#!/usr/bin/env python3
"""Kokoro TTS server (v1.1-zh) — 重建版 2026-09-15.
原部署随 ~/Downloads/models 丢失（迁移事故），本版按记忆档案 #164/#184/#185/#187 重建。
模型与代码均放仓库内 var/，根治"放 Downloads 迁移即丢"。

协议:
  GET  /health -> {"status":"ok","model":...,"voices":[...]}
  POST /api/tts  {text, voice?, rate?} -> audio/wav
  WS   /ws/tts   {action:"start", text, voice?, rate?} -> {type:"started"} -> binary wav -> {type:"finished"}
  voice 支持 blend 语法 "zf_017:0.83,zm_014:0.17"（权重和≈1，自动归一）
"""
import os
import espeakng_loader
os.environ["PHONEMIZER_ESPEAK_LIBRARY"] = espeakng_loader.get_library_path()
os.environ["ESPEAK_DATA_PATH"] = espeakng_loader.get_data_path()
espeakng_loader.make_library_available()
import io, wave, asyncio, logging
from pathlib import Path
import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from pydantic import BaseModel, Field
from kokoro_onnx import Kokoro

ROOT = Path(__file__).resolve().parent.parent         # 仓库根
MODELS = ROOT / "var" / "models"
MODEL = MODELS / "kokoro-v1.1-zh.fp16.onnx"
VOICES = MODELS / "voices-v1.1-zh.bin"
PORT = 8300
DEFAULT_VOICE = "zf_017:0.83,zm_014:0.17"             # 8/18 zhp 拍板
DEFAULT_RATE = 1.0
LOG = ROOT / "var" / "logs" / "kokoro-tts-server.log"
LOG.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(level=logging.INFO,
    handlers=[logging.FileHandler(LOG), logging.StreamHandler()],
    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kokoro-tts")

VOCAB = MODELS / "kokoro-v1.1-zh-config.json"
kokoro = Kokoro(str(MODEL), str(VOICES), vocab_config=str(VOCAB))
from misaki import zh as _zh
g2p = _zh.ZHG2P(version="1.1")   # v1.1-zh 模型必须走 misaki G2P
app = FastAPI(title="Kokoro TTS")

def resolve_voice(spec: str | None):
    """blend string -> weighted style vector; plain name -> str"""
    if not spec:
        spec = DEFAULT_VOICE
    if ":" not in spec:
        return spec
    total, acc = 0.0, None
    for part in spec.split(","):
        name, _, w = part.partition(":")
        w = float(w or 1.0)
        style = kokoro.get_voice_style(name.strip())
        acc = style * w if acc is None else acc + style * w
        total += w
    if acc is None or total <= 0:
        raise ValueError(f"bad voice spec: {spec}")
    return (acc / total).astype(np.float32)

def synth(text: str, voice: str | None, rate: float | None) -> bytes:
    phonemes, _ = g2p(text)          # 中文 G2P（misaki，espeak fallback）
    audio, sr = kokoro.create(
        phonemes, voice=resolve_voice(voice),
        speed=min(max(float(rate or DEFAULT_RATE), 0.5), 2.0),
        is_phonemes=True, trim=True)
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()

class TTSReq(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    voice: str | None = None
    rate: float | None = None

@app.get("/health")
@app.get("/api/health")
def health():
    return {"status": "ok", "model": MODEL.name,
            "default_voice": DEFAULT_VOICE, "sample_rate": 24000}

@app.post("/api/tts")
def api_tts(req: TTSReq):
    return Response(synth(req.text, req.voice, req.rate),
                    media_type="audio/wav")

@app.websocket("/ws/tts")
async def ws_tts(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            msg = await ws.receive_json()
            if msg.get("action") != "start":
                await ws.send_json({"type": "error", "detail": "expected action=start"})
                continue
            try:
                await ws.send_json({"type": "started",
                                    "voice": msg.get("voice") or DEFAULT_VOICE,
                                    "rate": msg.get("rate") or DEFAULT_RATE})
                wav = await asyncio.to_thread(
                    synth, msg.get("text", ""), msg.get("voice"), msg.get("rate"))
                await ws.send_bytes(wav)
                await ws.send_json({"type": "finished", "bytes": len(wav)})
            except Exception as e:
                log.exception("tts failed")
                await ws.send_json({"type": "error", "detail": str(e)})
    except WebSocketDisconnect:
        pass

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
