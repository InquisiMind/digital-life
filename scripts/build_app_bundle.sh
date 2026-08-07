#!/bin/bash
# 构建 DigitalLife.app bundle（用于触发 macOS 麦克风/屏幕录制权限弹窗）。
#
# macOS 的 TCC 权限基于 app bundle identifier。裸 python 命令行进程无
# bundle identifier，请求麦克风权限会被静默拒绝。本脚本构建一个最小
# .app bundle，bundle identifier = com.digital-life.agent，运行它即可
# 触发系统权限弹窗。授权后该 bundle 下启动的进程都能用麦克风。
#
# 用法：bash scripts/build_app_bundle.sh
# 之后：open scripts/DigitalLife.app --args --request-permissions

set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="$REPO_ROOT/scripts/DigitalLife.app"
CONTENTS="$APP_DIR/Contents"
MACOS_DIR="$CONTENTS/MacOS"

# 找 python（和 digital-life 用同一个）
PYTHON="$(command -v python3 || echo /usr/bin/python3)"

echo "构建 DigitalLife.app bundle..."
echo "  python: $PYTHON"
echo "  目标: $APP_DIR"

rm -rf "$APP_DIR"
mkdir -p "$MACOS_DIR"

# Info.plist —— 声明 bundle identifier + usage descriptions
cat > "$CONTENTS/Info.plist" << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>DigitalLife</string>
    <key>CFBundleDisplayName</key>
    <string>Digital Life</string>
    <key>CFBundleIdentifier</key>
    <string>com.digital-life.agent</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleExecutable</key>
    <string>DigitalLife</string>
    <key>LSMinimumSystemVersion</key>
    <string>10.14</string>
    <key>NSMicrophoneUsageDescription</key>
    <string>Digital Life 感知系统需要麦克风权限来录制音频，用于语音转写后注入给数字生命。</string>
    <key>NSScreenCaptureUsageDescription</key>
    <string>Digital Life 感知系统需要屏幕录制权限来截取屏幕画面，供视觉模型理解。</string>
    <key>NSAppleEventsUsageDescription</key>
    <string>Digital Life 需要发送 Apple 事件来触发系统功能。</string>
</dict>
</plist>
PLIST

# launcher 脚本 —— 它是被 macOS 当作 bundle 可执行文件运行的入口。
# 作用：把 args 传给 python 跑对应脚本。默认跑 request_mic_permission.py。
cat > "$MACOS_DIR/DigitalLife" << LAUNCHER
#!/bin/bash
# DigitalLife.app launcher —— 在 bundle 上下文里跑 python 脚本
export PYTHONPATH="$REPO_ROOT:\$PYTHONPATH"
PYTHON="$PYTHON"
# 默认行为：请求麦克风权限
SCRIPT="\${1:-request_mic_permission}"
shift 2>/dev/null || true
case "\$SCRIPT" in
    --request-permissions|-r)
        exec "\$PYTHON" "$REPO_ROOT/scripts/request_mic_permission.py" "\$@"
        ;;
    *)
        exec "\$PYTHON" "$REPO_ROOT/scripts/\${SCRIPT}.py" "\$@"
        ;;
esac
LAUNCHER

chmod +x "$MACOS_DIR/DigitalLife"

echo ""
echo "✓ 构建完成: $APP_DIR"
echo ""
echo "下一步：触发麦克风权限弹窗"
echo "  open $APP_DIR --args --request-permissions"
echo ""
echo "授权后重启 digital-life 生效："
echo "  digital-life restart"
