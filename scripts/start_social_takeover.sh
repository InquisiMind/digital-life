#!/bin/bash
# Social Takeover Daemon wrapper for launchd
# Auto-starts the Feishu social message takeover daemon
# Created by Zero on 2026-08-25

DIGITAL_LIFE_ROOT="/Users/zhanghaopu/Documents/项目材料/探索项目/数字生命"
PYTHON="/Library/Frameworks/Python.framework/Versions/3.12/Resources/Python.app/Contents/MacOS/Python"
INSTANCE_ID="c2a5c8e8-e4f5-4c69-be3e-aac49903081d"

cd "$DIGITAL_LIFE_ROOT"
export DIGITAL_LIFE_ROOT

exec "$PYTHON" -u -c "
import sys, os, time
sys.path.insert(0, '.')
os.environ['DIGITAL_LIFE_ROOT'] = os.getcwd()
from interfaces.social.feishu_takeover import start_takeover_daemon
start_takeover_daemon('${INSTANCE_ID}')
print('daemon alive', flush=True)
while True:
    time.sleep(60)
    print(f'heartbeat {int(time.time())}', flush=True)
"
