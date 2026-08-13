#!/bin/bash
# 感知 + 语音系统一键安装（feature 003-perception）
#
# 在任意 Mac 上跑一次，完成所有配置：
#   1. 编译 Carbon 快捷键 helper + AudioCaptureHelper.app
#   2. 安装 Python 依赖（sounddevice / silero-vad / sherpa-onnx / edge-tts 等）
#   3. 下载 KWS 唤醒词模型（18MB）
#   4. 引导 macOS 权限（辅助功能 / 麦克风 / 屏幕录制）
#   5. 验证一切就绪
#
# 用法：bash scripts/setup_perception.sh

set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

BOLD="\033[1m"
GREEN="\033[32m"
YELLOW="\033[33m"
RED="\033[31m"
RESET="\033[0m"

echo -e "${BOLD}=== 感知 + 语音系统安装 ===${RESET}"
echo ""

# ── 1. 检查 Swift 编译器 ──
echo -e "${BOLD}[1/6] 检查 Swift 编译器...${RESET}"
if ! command -v swiftc &>/dev/null; then
    echo -e "${RED}✗ swiftc 未找到。请安装 Xcode Command Line Tools：xcode-select --install${RESET}"
    exit 1
fi
echo -e "${GREEN}✓ Swift $(swift --version 2>&1 | head -1)${RESET}"

# ── 2. 编译 Carbon helper + AudioCaptureHelper.app ──
echo ""
echo -e "${BOLD}[2/6] 编译快捷键 helper + 录音 app...${RESET}"

# Carbon 快捷键 helper
swiftc -O scripts/hotkey_helper.swift -o scripts/hotkey_helper 2>&1 || {
    echo -e "${RED}✗ hotkey_helper 编译失败${RESET}"
    exit 1
}
echo -e "${GREEN}✓ hotkey_helper 编译完成${RESET}"

# AudioCaptureHelper.app（持续录音 + FIFO 传 PCM）
APP_DIR="scripts/AudioCaptureHelper.app"
mkdir -p "$APP_DIR/Contents/MacOS"
# Info.plist
cat > "$APP_DIR/Contents/Info.plist" << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>AudioCaptureHelper</string>
    <key>CFBundleIdentifier</key><string>com.digital-life.audio-capture-helper</string>
    <key>CFBundleVersion</key><string>1.0</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleExecutable</key><string>audio_capture_helper</string>
    <key>LSMinimumSystemVersion</key><string>14.0</string>
    <key>LSUIElement</key><true/>
    <key>NSMicrophoneUsageDescription</key><string>Digital Life 语音感知需要麦克风来持续监听。</string>
</dict>
</plist>
PLIST
# Swift 包装器（读环境变量 AUDIO_SENSE_FIFO，调 /usr/bin/python3 录音写 FIFO）
cat > /tmp/_ach.swift << 'SWIFT'
import Foundation
let fifo = ProcessInfo.processInfo.environment["AUDIO_SENSE_FIFO"] ?? "/tmp/_audio_sense_fifo"
let task = Process()
task.launchPath = "/usr/bin/python3"
task.arguments = ["-c", """
import os, sys, time, signal
import sounddevice as sd
import numpy as np
FIFO = '\(fifo)'
SR = 16000
BATCH_S = 2.0
recording = [True]
def stop(sig, frame):
    recording[0] = False
signal.signal(signal.SIGTERM, stop)
try:
    fifo_fd = os.open(FIFO, os.O_WRONLY)
except Exception as e:
    sys.exit(1)
try:
    while recording[0]:
        data = sd.rec(int(BATCH_S * SR), samplerate=SR, channels=1, dtype='int16')
        sd.wait()
        if not recording[0]:
            break
        os.write(fifo_fd, data.tobytes())
except Exception:
    pass
finally:
    os.close(fifo_fd)
"""]
task.launch()
task.waitUntilExit()
SWIFT
swiftc -O /tmp/_ach.swift -o "$APP_DIR/Contents/MacOS/audio_capture_helper" 2>&1 || {
    echo -e "${YELLOW}⚠ AudioCaptureHelper 编译失败（持续对话模式不可用，快捷键仍可用）${RESET}"
}
rm -f /tmp/_ach.swift
codesign --force --deep --sign - "$APP_DIR" 2>/dev/null || true
echo -e "${GREEN}✓ AudioCaptureHelper.app 编译完成${RESET}"

# ── 3. 安装 Python 依赖 ──
echo ""
echo -e "${BOLD}[3/6] 安装 Python 依赖...${RESET}"
PYTHON=$(command -v python3 || echo /usr/bin/python3)
PIP_OK=true

# 基础感知依赖
for pkg in sounddevice httpx numpy; do
    if ! $PYTHON -c "import ${pkg//-/_}" 2>/dev/null; then
        echo "  安装 $pkg..."
        $PYTHON -m pip install "$pkg" -q 2>&1 | tail -1 || PIP_OK=false
    fi
done

# 语音感知依赖
for pkg in silero-vad onnxruntime edge-tts sherpa-onnx; do
    if ! $PYTHON -c "import ${pkg//-/_}" 2>/dev/null; then
        echo "  安装 $pkg..."
        $PYTHON -m pip install "$pkg" -q 2>&1 | tail -1 || PIP_OK=false
    fi
done

if [ "$PIP_OK" = true ]; then
    echo -e "${GREEN}✓ Python 依赖就绪${RESET}"
else
    echo -e "${YELLOW}⚠ 部分依赖安装失败（可能影响部分功能）${RESET}"
fi

# ── 4. 下载 KWS 唤醒词模型 ──
echo ""
echo -e "${BOLD}[4/6] 下载 KWS 唤醒词模型（18MB）...${RESET}"
MODEL_DIR="models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
if [ -f "$MODEL_DIR/encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx" ]; then
    echo -e "${GREEN}✓ 模型已存在${RESET}"
else
    $PYTHON -c "
from modelscope import snapshot_download
snapshot_download(
    'pkufool/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01',
    local_dir='$MODEL_DIR',
)
print('✓ 模型下载完成')
" 2>&1 || echo -e "${YELLOW}⚠ 模型下载失败（持续对话模式需要，快捷键不受影响）${RESET}"
fi

# ── 5. 检查 ffmpeg ──
echo ""
echo -e "${BOLD}[5/6] 检查 ffmpeg...${RESET}"
if command -v ffmpeg &>/dev/null; then
    echo -e "${GREEN}✓ ffmpeg 已安装${RESET}"
else
    echo -e "${YELLOW}⚠ ffmpeg 未安装（音频分段会降级，建议 brew install ffmpeg）${RESET}"
fi

# ── 6. 授权引导 ──
echo ""
echo -e "${BOLD}[6/6] macOS 权限授权${RESET}"
echo ""
echo -e "感知系统需要以下 macOS 权限："
echo ""
echo -e "${BOLD}┌─ 辅助功能 ─┐${RESET}"
echo -e "  系统设置 → 隐私与安全性 → 辅助功能"
echo -e "  添加运行 digital-life 的终端（Terminal / iTerm / ZCode）"
echo -e "  ${GREEN}快捷键（cmd+shift+r）需要这个权限${RESET}"
echo ""
echo -e "${BOLD}┌─ 麦克风 ─┐${RESET}"
echo -e "  系统设置 → 隐私与安全性 → 麦克风"
echo -e "  添加 ${BOLD}AudioCaptureHelper.app${RESET}（scripts/ 目录下）"
echo -e "  以及运行 digital-life 的终端"
echo -e "  ${GREEN}语音录入 + 持续监听需要这个权限${RESET}"
echo ""

# ── 验证 ──
echo -e "${BOLD}=== 验证 ===${RESET}"
echo ""
echo -e "${BOLD}快捷键 helper：${RESET}"
echo "  ./scripts/hotkey_helper r cmd+shift &"
echo "  （输出 READY 表示注册成功，按 cmd+shift+r 输出 TRIGGERED）"
echo ""
echo -e "${BOLD}麦克风：${RESET}"
$PYTHON -c "
import sounddevice as sd, numpy as np
try:
    data = sd.rec(int(1*16000), samplerate=16000, channels=1, dtype='int16')
    sd.wait()
    mx = int(np.abs(data).max())
    if mx > 100:
        print(f'  ✓ 录音正常（最大值 {mx}）')
    else:
        print(f'  ✗ 录音静音（麦克风未授权，最大值 {mx}）')
except Exception as e:
    print(f'  ✗ 录音失败: {e}')
" 2>&1

echo ""
echo -e "${BOLD}VAD（Silero）：${RESET}"
$PYTHON -c "
try:
    from infrastructure.perception.voice_session import _find_silero_onnx
    p = _find_silero_onnx()
    print(f'  ✓ VAD 模型: {p.name}（{p.stat().st_size // 1024}KB）')
except Exception as e:
    print(f'  ✗ VAD 加载失败: {e}')
" 2>&1

echo ""
echo -e "${BOLD}KWS（sherpa-onnx）：${RESET}"
$PYTHON -c "
try:
    import sherpa_onnx
    print(f'  ✓ sherpa-onnx {sherpa_onnx.__version__}')
except Exception as e:
    print(f'  ✗ sherpa-onnx 未安装（持续对话模式不可用）')
" 2>&1

echo ""
echo -e "${BOLD}TTS（edge-tts）：${RESET}"
if command -v edge-tts &>/dev/null; then
    echo -e "${GREEN}  ✓ edge-tts 已安装${RESET}"
else
    echo -e "${YELLOW}  ⚠ edge-tts 未安装（pip install edge-tts）${RESET}"
fi

echo ""
echo -e "${BOLD}=== 安装完成 ===${RESET}"
echo ""
echo "接下来："
echo "  1. 实例 app.yaml 设 perception.enabled: true（快捷键单次问答）"
echo "  2. config/voice_sense.yaml 设 enabled: true（持续对话模式，可选）"
echo "  3. digital-life restart"
echo "  4. 按 cmd+shift+r 触发感知录制"
echo ""
echo "详细文档：docs/operations/perception-setup.md"
