"""E2E 验证：真实 38s 录音 → 流式回放（模拟边录边转）→ 测 stop 后延迟。"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

IID = "c2a5c8e8-e4f5-4c69-be3e-aac49903081d"
# 默认用 zero 的最近一段真实录音；可传参指定其他 wav
_DEFAULT = ROOT / f"apps/{IID}/data/perception/audio_1786694750.wav"
SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else _DEFAULT

from infrastructure.perception.config import load_config
from infrastructure.perception.live_transcribe import LiveTranscriber

growing = Path("/tmp/live_e2e_test/audio_grow.wav")
if growing.parent.exists():
    import shutil
    shutil.rmtree(growing.parent)
growing.parent.mkdir(parents=True)

data = SRC.read_bytes()
hdr, body = data[:44], data[44:]
print(f"source: {SRC.name}, {len(data)} bytes, {len(body)/2/16000:.1f}s audio")

cfg = load_config(IID)
lt = LiveTranscriber(growing, config=cfg, min_segment_seconds=3.0, silence_frames=25)

growing.write_bytes(hdr)
lt.start()

CHUNK = 6400  # 0.2s
t_start = time.time()
for i in range(0, len(body), CHUNK):
    with open(growing, "ab") as f:
        f.write(body[i:i + CHUNK])
    time.sleep(0.2)  # 真实速度回放

t_stop = time.time()  # ← 模拟"用户按下停止键"
text = lt.stop_and_finalize()
t_done = time.time()

print(f"\n=== 模拟录音 {t_stop - t_start:.1f}s ===")
print(f"=== 停止 → 全文就绪: {t_done - t_stop:.2f}s ===\n")
print(text)
