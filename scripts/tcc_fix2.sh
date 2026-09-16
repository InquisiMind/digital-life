#!/bin/bash
# tcc_fix2.sh — alpha 9/15 19:49：补 csreq 签名指纹（v1 写的行 csreq=NULL 被 tccd 无视）+ 补辅助功能两服务 + bundle id 双归属
set -u
DB="$HOME/Library/Application Support/com.apple.TCC/TCC.db"
PYREAL="/Users/zhanghaopu/.local/share/uv/python/cpython-3.12.14-macos-aarch64-none/bin/python3.12"
BID="com.digital-life.agent"
NOW=$(date +%s)

if ! sqlite3 "$DB" "SELECT 1 FROM access LIMIT 1;" >/dev/null 2>&1; then
  echo "FATAL: 读不了 TCC.db"; exit 1
fi
BK="/Users/zhanghaopu/Documents/探索项目/digital-life/var/backups/TCC.db.$(date +%H%M%S)"
mkdir -p "$(dirname "$BK")" && cp "$DB" "$BK" && echo "已备份: $BK"

echo "=== 1. 四个服务（麦克风/屏幕录制/辅助功能×2），python 路径型 + csreq 从已生效行复制 ==="
for SVC in kTCCServiceMicrophone kTCCServiceScreenCapture kTCCServicePostEvent kTCCServiceListenEvent; do
  sqlite3 "$DB" "UPDATE access SET csreq=(SELECT csreq FROM access WHERE service='kTCCServiceSystemPolicyDocumentsFolder' AND client='$PYREAL'), auth_value=2, auth_reason=2, auth_version=1, flags=0, last_modified=$NOW WHERE service='$SVC' AND client='$PYREAL';"
  CNT=$(sqlite3 "$DB" "SELECT count(*) FROM access WHERE service='$SVC' AND client='$PYREAL';")
  if [ "$CNT" = "0" ]; then
    sqlite3 "$DB" "INSERT INTO access(service, client, client_type, auth_value, auth_reason, auth_version, csreq, flags, last_modified) SELECT '$SVC', client, 1, 2, 2, 1, csreq, 0, $NOW FROM access WHERE service='kTCCServiceSystemPolicyDocumentsFolder' AND client='$PYREAL';"
  fi
done

echo "=== 2. bundle id 型双保险（无签名 bundle，csreq 留空） ==="
for SVC in kTCCServiceMicrophone kTCCServiceScreenCapture kTCCServicePostEvent kTCCServiceListenEvent; do
  sqlite3 "$DB" "INSERT OR REPLACE INTO access(service, client, client_type, auth_value, auth_reason, auth_version, flags, last_modified) VALUES('$SVC','$BID',0,2,2,1,0,$NOW);"
done

echo "=== 3. 刷新 ==="
killall tccd 2>/dev/null && echo "tccd 已重启"

echo "=== 4. 读回（service|归属|指纹长度|授权值） ==="
sqlite3 "$DB" "SELECT service, CASE client_type WHEN 1 THEN 'python路径' ELSE 'bundle' END, coalesce(length(csreq),0), auth_value FROM access WHERE client='$PYREAL' OR client='$BID' ORDER BY service, client_type;"
echo ""
echo "python 路径型各行指纹长度应 > 0（如 91 之类）。回滚: sudo cp $BK ~/Library/Application\\ Support/com.apple.TCC/TCC.db && killall tccd"
