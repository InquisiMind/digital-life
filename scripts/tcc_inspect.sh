#!/bin/bash
# TCC 体检脚本（alpha 9/15 写）：读 schema + digital/python 相关行 + 全部行摘要
DB="$HOME/Library/Application Support/com.apple.TCC/TCC.db"
echo "=== 1. access 表结构 ==="
sqlite3 "$DB" "PRAGMA table_info(access);"
echo ""
echo "=== 2. digital/python 相关现有行 ==="
sqlite3 "$DB" "SELECT * FROM access WHERE client LIKE '%digital%' OR client LIKE '%python%';"
echo ""
echo "=== 3. 全部行摘要(service|client|type|auth_value) ==="
sqlite3 "$DB" "SELECT service, client, client_type, auth_value FROM access;"
