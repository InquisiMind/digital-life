#!/usr/bin/env python3
"""
回测日志记录
"""

import os
import json
from datetime import datetime

LOG_DIR = os.path.expanduser("~/.backtest_logs")

def ensure_log_dir():
    if not os.path.exists(LOG_DIR):
        os.makedirs(LOG_DIR)

def log_backtest_run(config: dict, result: dict):
    """记录回测运行"""
    ensure_log_dir()
    
    log_file = os.path.join(LOG_DIR, f"backtest_{datetime.now().strftime('%Y%m%d')}.json")
    
    entry = {
        'timestamp': datetime.now().isoformat(),
        'config': config,
        'result': result
    }
    
    logs = []
    if os.path.exists(log_file):
        with open(log_file, 'r') as f:
            try:
                logs = json.load(f)
            except:
                logs = []
    
    logs.append(entry)
    
    with open(log_file, 'w') as f:
        json.dump(logs, f, indent=2)
    
    return log_file

def get_recent_logs(days: int = 7):
    """获取最近的日志"""
    ensure_log_dir()
    
    logs = []
    for filename in os.listdir(LOG_DIR):
        if filename.endswith('.json'):
            filepath = os.path.join(LOG_DIR, filename)
            with open(filepath, 'r') as f:
                try:
                    logs.extend(json.load(f))
                except:
                    pass
    
    return logs[-days*10:]  # 最近N天，每天约10条

if __name__ == "__main__":
    # 测试
    log_backtest_run(
        {'stop_loss': 0.08, 'take_profit': 0.20},
        {'return': 8.8, 'win_rate': 60}
    )
    print("✅ 日志记录测试通过")
