#!/bin/bash
# tcc_fix.sh v2 — alpha 9/15 19:46 重写：按 zhp 实测 schema（flags列/TEXT boot_uuid）修正
# 铁证：库里 DocumentsFolder|python路径|type1 行证明 TCC 追踪 venv python 绝对路径，照抄该行格式写麦克风+屏幕录制
set -u
DB="$HOME/Library/Application Support/com.apple.TCC/TCC.db"
BK="/Users/zhanghaopu/Documents/探索项目/digital-life/var/backups/TCC.db.$(date +%H%M%S)"
PYREAL=$(readlink -f "/Users/zhanghaopu/Documents/探索项目/digital-life/.venv/bin/python" 2>/dev/null)

echo "=== 0. 前置自检 ==="
if ! sqlite3 "$DB" "SELECT 1 FROM access LIMIT 1;" >/dev/null 2>&1; then
  echo "FATAL: 读不了 TCC.db —— Terminal 的完全磁盘访问没生效。停下。"; exit 1
fi
echo "读库 OK"
echo "python 真身: $PYREAL"
[ -x "$PYREAL" ] || { echo "FATAL: python 路径解析失败"; exit 1; }
mkdir -p "$(dirname "$BK")" && cp "$DB" "$BK" && echo "已备份: $BK"

echo ""
echo "=== 1. 读模板（DocumentsFolder python 行，实测存在的格式） ==="
TPL=$(sqlite3 "$DB" "SELECT auth_value, auth_reason, auth_version, flags FROM access WHERE service='kTCCServiceSystemPolicyDocumentsFolder' AND client='$PYREAL' LIMIT 1;" 2>/dev/null)
if [ -n "$TPL" ]; then
  echo "模板(python Documents行): $TPL"
  AV=$(echo "$TPL" | cut -d'|' -f1); AR=$(echo "$TPL" | cut -d'|' -f2); AVV=$(echo "$TPL" | cut -d'|' -f3); BF=$(echo "$TPL" | cut -d'|' -f4)
  AV=${AV:-2}; AR=${AR:-2}; AVV=${AVV:-1}; BF=${BF:-0}
else
  echo "模板行没找到，用标准值 2|2|1|0"
  AV=2; AR=2; AVV=1; BF=0
fi
NOW=$(date +%s)

echo ""
echo "=== 2. 写入（麦克风 + 屏幕录制，python 路径型） ==="
for SVC in kTCCServiceMicrophone kTCCServiceScreenCapture; do
  sqlite3 "$DB" "INSERT OR REPLACE INTO access(service, client, client_type, auth_value, auth_reason, auth_version, flags, last_modified) VALUES('$SVC','$PYREAL',1,$AV,$AR,$AVV,$BF,$NOW);"
done
echo "写入完成（2行）"

echo ""
echo "=== 3. 刷新权限服务 ==="
killall tccd 2>/dev/null && echo "tccd 已重启" || echo "tccd 不在跑（无妨，下次请求现读）"

echo ""
echo "=== 4. 验证读回 ==="
sqlite3 "$DB" "SELECT service, client_type, auth_value, auth_reason FROM access WHERE client='$PYREAL';"
echo ""
echo "期望 3 行：DocumentsFolder + Microphone + ScreenCapture 全部 auth_value=2"
echo "回滚: sudo cp $BK $DB && killall tccd"
