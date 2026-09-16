#!/bin/bash
# DigitalLife.app launcher —— 在 bundle 上下文里跑 python 脚本（stub → 本脚本）
# stub 已把 REPO 根改为动态：从本脚本路径反推。此处保持双源：优先环境变量，退回固定路径
REPO="/Users/zhanghaopu/Documents/探索项目/digital-life"
export PYTHONPATH="$REPO:$PYTHONPATH"
PYTHON="$REPO/.venv/bin/python3"
LOG="$REPO/var/log/digitallife_last.log"
mkdir -p "$(dirname "$LOG")"
exec > >(tee -a "$LOG") 2>&1
echo "=== $(date '+%F %T') DigitalLife.app wrapper 启动, argv=$* ==="
SCRIPT="${1:-request_mic_permission}"
shift 2>/dev/null || true
case "$SCRIPT" in
    --request-permissions|-r)
        exec "$PYTHON" "$REPO/scripts/request_mic_permission.py" "$@"
        ;;
    gateway)
        cd "$REPO"
        exec "$PYTHON" "$REPO/gateway/main.py" "$@"
        ;;
    *)
        exec "$PYTHON" "$REPO/scripts/${SCRIPT}.py" "$@"
        ;;
esac
