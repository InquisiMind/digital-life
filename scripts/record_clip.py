#!/usr/bin/env python3
"""录 N 秒音频写 wav，打印 maxval。经 DigitalLife.app bundle 上下文运行时获得麦克风 TCC 归属。
用法: record_clip.py <out.wav> [secs]
退出码: 0=有声(maxval>阈值) 2=静默(疑似TCC拒绝) 1=异常
"""
import sys, time, signal, wave
import numpy as np
import sounddevice as sd

out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/record_clip.wav"
secs = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0
sr = 16000
recording = [True]
mx = [0]

def on_stop(sig, frame):
    recording[0] = False

signal.signal(signal.SIGTERM, on_stop)

with wave.open(out, "wb") as wf:
    wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(sr)
    def callback(indata, frames, t, status):
        if not recording[0]:
            raise sd.CallbackStop
        wf.writeframes(indata.tobytes())
        m = int(np.abs(indata).max())
        if m > mx[0]:
            mx[0] = m
    with sd.InputStream(samplerate=sr, channels=1, dtype="int16",
                        blocksize=1024, callback=callback):
        t0 = time.time()
        while recording[0] and time.time() - t0 < secs:
            time.sleep(0.05)

print(f"maxval={mx[0]} path={out}")
sys.exit(0 if mx[0] > 300 else 2)
