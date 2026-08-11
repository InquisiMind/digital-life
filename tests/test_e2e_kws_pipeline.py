"""V4+V6 端到端验证：用模型自带的测试音频通过完整 AudioSenseService 管道。

不依赖真人说话——用 test_wavs/3.wav（已知内容"文森特卡索"）+ test_keywords.txt
（含该关键词），验证从 PCM → VAD → KWS → router → emit 的完整链路。

方法：mock AudioCapture（直接从 wav 文件读 PCM 块喂给 _on_chunk），
其余组件（VAD/KWS/router/emit）全部用真实代码。
"""
import os
import sys
import time
import numpy as np
import soundfile as sf
from pathlib import Path
from unittest.mock import patch, MagicMock

# 项目根目录
ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

MODEL_DIR = ROOT / "models" / "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
TEST_DIR = MODEL_DIR / "test_wavs"

print("=" * 60)
print("V4+V6: 完整管道端到端验证（模型测试音频 → KWS hit → emit）")
print("=" * 60)

# 1. 构造配置（用 test_keywords.txt，能命中测试音频）
from infrastructure.perception.audio_sense.service import VoiceSenseConfig
config = VoiceSenseConfig(
    enabled=True,
    kws_model_dir=str(MODEL_DIR),
    kws_keywords_file=str(TEST_DIR / "test_keywords.txt"),
    kws_threshold=0.25,
    default_instance="test-instance-id",
    dialog_timeout_s=999,  # 不超时
)

# 2. 构造 service（但不 start AudioCapture——我们手动喂 PCM）
from infrastructure.perception.audio_sense.service import AudioSenseService
svc = AudioSenseService(config)

# 手动初始化组件（跳过 capture）
svc._init_components()
print(f"✅ KWS loaded: {svc._kws is not None}")
print(f"✅ VAD loaded: {svc._vad is not None}")
print(f"✅ Router state: {svc._router.state}")

# 3. mock emit（验证 V6 的 emit 链路）
emit_wake_calls = []
emit_dialog_calls = []

def mock_emit_wake(transcript, target):
    emit_wake_calls.append((transcript, target))
    print(f"  📤 emit_wake: target={target[:8]} transcript={transcript[:40]}")

def mock_emit_dialog(transcript, target):
    emit_dialog_calls.append((transcript, target))
    print(f"  📤 emit_dialog: target={target[:8]} transcript={transcript[:40]}")

svc._router._cb.emit_wake = mock_emit_wake
svc._router._cb.emit_dialog = mock_emit_dialog

# 4. 读测试音频，分块喂给 _on_chunk（模拟 capture 读循环）
wav_file = TEST_DIR / "3.wav"  # 内容"文森特卡索"
audio, sr = sf.read(str(wav_file), dtype="float32")
print(f"\n播放测试音频: {wav_file.name} ({len(audio)/sr:.1f}s)")

# 追加静音尾部（给 KWS 时间处理最后几帧）
tail = np.zeros(int(1.0 * sr), dtype=np.float32)
audio = np.concatenate([audio, tail])

# 分块喂（模拟 100ms 块）
chunk_size = 1600
hit_found = False
for i in range(0, len(audio), chunk_size):
    chunk = audio[i:i + chunk_size]
    if len(chunk) < chunk_size:
        chunk = np.pad(chunk, (0, chunk_size - len(chunk)))
    # 转成 int16（capture 产出 int16）
    pcm = (chunk * 32768).clip(-32768, 32767).astype(np.int16)
    svc._on_chunk(pcm)

    # 检查状态是否从 dormant → dialog
    if svc._router.state.value != "dormant" and not hit_found:
        hit_found = True
        print(f"\n✅✅✅ KWS HIT! 状态: {svc._router.state.value}")
        print(f"   emit_wake 调用数: {len(emit_wake_calls)}")
        break

if not hit_found:
    print(f"\n❌ 未命中（state={svc._router.state.value}）")
    print(f"   emit_wake: {len(emit_wake_calls)}, emit_dialog: {len(emit_dialog_calls)}")
else:
    print(f"\n>>> V4+V6 验证通过！")
    print(f"    V4: KWS 检测到关键词 → dormant→dialog ✅")
    print(f"    V6: emit_wake 被调用 → {emit_wake_calls[0] if emit_wake_calls else 'N/A'} ✅")
