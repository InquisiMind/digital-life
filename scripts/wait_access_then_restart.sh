#!/bin/bash
# 等待辅助功能权限生效 → 重启 perception daemon → 通知
cd /Users/zhanghaopu/Documents/探索项目/digital-life
for i in $(seq 1 90); do  # 最多 90*2=180s
  GRANTED=$(./.venv/bin/python3 -c "
import ctypes
libc = ctypes.CDLL('/System/Library/Frameworks/IOKit.framework/IOKit')
r = libc.IOHIDCheckAccess(1)  # kIOHIDAccessTypeHIDEventTap
print('YES' if r == 0 else 'NO')
" 2>/dev/null)
  if [ "$GRANTED" = "YES" ]; then
    echo "$(date '+%H:%M:%S') access granted, restarting daemon"
    # 杀旧 daemon（经 DigitalLife.app 起的 python 进程跑 perception_daemon.py）
    pkill -f perception_daemon.py 2>/dev/null
    sleep 1
    # 经 DigitalLife.app 拉起（TCC 归属干净）
    open scripts/DigitalLife.app --args daemon
    sleep 3
    # 验证起来没
    if pgrep -f perception_daemon.py > /dev/null; then
      echo "$(date '+%H:%M:%S') daemon restarted OK"
    else
      echo "$(date '+%H:%M:%S') daemon FAILED to start"
    fi
    exit 0
  fi
  sleep 2
done
echo "$(date '+%H:%M:%S') timeout: access never granted in 180s"
exit 1
