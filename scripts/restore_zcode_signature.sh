#!/bin/bash
# 恢复 ZCode 原签名（Developer ID）—— 修复 ad-hoc 重签导致的辅助功能/快捷键失效。
#
# 用法：
#   1. 先完全退出 ZCode（Cmd+Q，确保 Activity Monitor 里没有 ZCode 进程）
#   2. 在 Terminal（不是 ZCode）里跑：bash scripts/restore_zcode_signature.sh
#   3. 重新打开 ZCode
#
# 原理：从 ZCode 的更新缓存（pending update zip）解压原签名版本，替换当前的 ad-hoc 版。

set -e

ZIP_PATH="$HOME/Library/Caches/@zcodedesktop-updater/pending/ZCode-3.7.3-mac-arm64.zip"
APP_PATH="/Applications/ZCode.app"

echo "=== 恢复 ZCode 原签名 ==="

# 检查 ZCode 是否在运行
if pgrep -f "ZCode.app" > /dev/null 2>&1; then
    echo "✗ ZCode 还在运行！请先完全退出 ZCode（Cmd+Q），再跑此脚本。"
    echo "  残留进程："
    pgrep -fl "ZCode.app" | head -5
    exit 1
fi

# 检查源文件
if [ ! -f "$ZIP_PATH" ]; then
    echo "✗ 更新缓存不存在: $ZIP_PATH"
    echo "  请从 zcode.z.ai 重新下载 ZCode 3.7.3 安装包"
    exit 1
fi

echo "1. 解压原签名版本..."
rm -rf /tmp/zcode_restore
mkdir -p /tmp/zcode_restore
unzip -q "$ZIP_PATH" -d /tmp/zcode_restore

echo "2. 验证原签名..."
TEAM=$(codesign -dv /tmp/zcode_restore/ZCode.app 2>&1 | grep "TeamIdentifier" || true)
if echo "$TEAM" | grep -q "8A5X4JJ39T"; then
    echo "   ✓ Developer ID 签名确认"
else
    echo "   ✗ 签名异常: $TEAM"
    exit 1
fi

echo "3. 替换 $APP_PATH ..."
rm -rf "$APP_PATH"
ditto /tmp/zcode_restore/ZCode.app "$APP_PATH"

echo "4. 验证替换结果..."
NEW_FLAGS=$(codesign -dv "$APP_PATH" 2>&1 | grep "flags" || true)
echo "   $NEW_FLAGS"
if echo "$NEW_FLAGS" | grep -q "runtime"; then
    echo "   ✓✓✓ 原签名恢复成功！"
else
    echo "   ✗ 替换后仍非原签名"
    exit 1
fi

echo ""
echo "✓ 完成。现在可以重新打开 ZCode 了。"
echo "  快捷键（Cmd+Shift+Z）应该恢复正常。"
echo "  注意：麦克风可能需要重新授权（如果之前用 entitlement 方案解决的）。"
