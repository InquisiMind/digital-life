#!/bin/bash
# 感知系统一键安装 + 授权（feature 003-perception）
#
# 在任意 Mac 上跑一次，完成所有配置：
#   1. 编译 Carbon 快捷键 helper（不依赖辅助功能权限）
#   2. 安装 Python 依赖（mss/sounddevice/httpx 等）
#   3. 引导授权屏幕录制 + 麦克风
#   4. 验证一切就绪
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

echo -e "${BOLD}=== 感知系统安装 ===${RESET}"
echo ""

# ── 1. 检查 Swift 编译器 ──
echo -e "${BOLD}[1/5] 检查 Swift 编译器...${RESET}"
if ! command -v swiftc &>/dev/null; then
    echo -e "${RED}✗ swiftc 未找到。请安装 Xcode Command Line Tools：xcode-select --install${RESET}"
    exit 1
fi
echo -e "${GREEN}✓ Swift $(swift --version 2>&1 | head -1)${RESET}"

# ── 2. 编译 Carbon helper ──
echo ""
echo -e "${BOLD}[2/5] 编译全局快捷键 helper（Carbon API，不需要辅助功能权限）...${RESET}"
swiftc -O scripts/hotkey_helper.swift -o scripts/hotkey_helper 2>&1
if [ ! -f scripts/hotkey_helper ]; then
    echo -e "${RED}✗ 编译失败${RESET}"
    exit 1
fi
echo -e "${GREEN}✓ helper 编译完成${RESET}"

# ── 3. 安装 Python 依赖 ──
echo ""
echo -e "${BOLD}[3/5] 安装 Python 依赖...${RESET}"
PYTHON=$(command -v python3 || echo /usr/bin/python3)
PIP_OK=true
for pkg in mss sounddevice httpx pyobjc-core pyobjc-framework-AVFoundation pyobjc-framework-Cocoa; do
    if ! $PYTHON -c "import ${pkg//-/_}" 2>/dev/null || \
       ! $PYTHON -c "import ${pkg%%-*}" 2>/dev/null; then
        echo "  安装 $pkg..."
        $PYTHON -m pip install "$pkg" -q 2>&1 | tail -1 || PIP_OK=false
    fi
done
if [ "$PIP_OK" = true ]; then
    echo -e "${GREEN}✓ Python 依赖就绪${RESET}"
else
    echo -e "${YELLOW}⚠ 部分依赖安装失败（可能影响部分功能）${RESET}"
fi

# ── 4. 检查 ffmpeg（音频分段）──
echo ""
echo -e "${BOLD}[4/5] 检查 ffmpeg...${RESET}"
if command -v ffmpeg &>/dev/null; then
    echo -e "${GREEN}✓ ffmpeg 已安装${RESET}"
else
    echo -e "${YELLOW}⚠ ffmpeg 未安装（音频分段会降级，建议 brew install ffmpeg）${RESET}"
fi

# ── 5. 授权引导 ──
echo ""
echo -e "${BOLD}[5/5] macOS 权限授权${RESET}"
echo ""
echo -e "感知系统需要两个 macOS 权限。请按以下步骤操作："
echo ""

echo -e "${BOLD}┌─ 屏幕录制权限 ─┐${RESET}"
echo -e "  1. 打开 ${BOLD}系统设置 → 隐私与安全性 → 屏幕录制${RESET}"
echo -e "  2. 点列表下方的 ${BOLD}'+'${RESET}"
echo -e "  3. 添加你用来跑 digital-life 的终端 app"
echo -e "     （Terminal.app / iTerm / ZCode 等）"
echo -e "  4. 打开它的开关"
echo ""

echo -e "${BOLD}┌─ 麦克风权限 ─┐${RESET}"
echo -e "  麦克风权限需要在 ${BOLD}实例 app.yaml${RESET} 里启用感知后"
echo -e "  首次录制时触发，或者："
echo -e "  1. 打开 ${BOLD}系统设置 → 隐私与安全性 → 麦克风${RESET}"
echo -e "  2. 确保你的终端 app 在列表里且开关打开"
echo -e "  3. 如果终端 app 的宿主（如 ZCode）有 hardened runtime，"
echo -e "     可能需要 audio-input entitlement（见 docs/operations/perception-setup.md）"
echo ""

echo -e "${BOLD}┌─ 辅助功能（不需要！）─┐${RESET}"
echo -e "  全局快捷键使用 Carbon RegisterEventHotKey，"
echo -e "  ${GREEN}不需要辅助功能权限${RESET}。"
echo ""

# ── 验证 ──
echo -e "${BOLD}=== 验证 ===${RESET}"
echo ""
echo -e "${BOLD}快捷键 helper：${RESET}"
echo "  ./scripts/hotkey_helper z cmd+shift &"
echo "  （输出 READY 表示注册成功，按 Cmd+Shift+Z 输出 TRIGGERED）"
echo ""
echo -e "${BOLD}屏幕录制：${RESET}"
$PYTHON -c "
try:
    import mss
    with mss.mss() as sct:
        m = sct.monitors[1] if len(sct.monitors)>1 else sct.monitors[0]
        shot = sct.grab(m)
        # 检查是不是全黑（未授权时返回黑屏）
        import numpy as np
        arr = np.frombuffer(shot.rgb, dtype=np.uint8)
        if arr.max() == 0:
            print('  ✗ 截图全黑（屏幕录制未授权）')
        else:
            print('  ✓ 截图正常（屏幕录制已授权）')
except Exception as e:
    print(f'  ✗ 截图失败: {e}')
" 2>&1

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
echo -e "${BOLD}=== 安装完成 ===${RESET}"
echo ""
echo "接下来："
echo "  1. 在实例 app.yaml 里设 perception.enabled: true"
echo "  2. digital-life restart"
echo "  3. 按 Cmd+Shift+Z 触发感知录制"
echo ""
echo "详细文档：docs/operations/perception-setup.md"
