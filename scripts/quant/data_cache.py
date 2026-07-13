#!/usr/bin/env python3
"""
历史数据缓存管理
避免重复请求API
"""

import os
import json
import time
from datetime import datetime

CACHE_DIR = os.path.expanduser("~/.backtest_cache")
CACHE_TTL = 3600  # 1小时

def ensure_cache_dir():
    """确保缓存目录存在"""
    if not os.path.exists(CACHE_DIR):
        os.makedirs(CACHE_DIR)

def get_cache_key(data_type: str, params: dict) -> str:
    """生成缓存键"""
    import hashlib
    key_str = f"{data_type}_{str(sorted(params.items()))}"
    return hashlib.md5(key_str.encode()).hexdigest()

def read_cache(data_type: str, params: dict) -> dict:
    """读取缓存"""
    ensure_cache_dir()
    key = get_cache_key(data_type, params)
    cache_file = os.path.join(CACHE_DIR, f"{key}.json")
    
    if os.path.exists(cache_file):
        try:
            with open(cache_file, 'r') as f:
                data = json.load(f)
                if time.time() - data.get('timestamp', 0) < CACHE_TTL:
                    return data.get('value')
        except:
            pass
    return None

def write_cache(data_type: str, params: dict, value: dict):
    """写入缓存"""
    ensure_cache_dir()
    key = get_cache_key(data_type, params)
    cache_file = os.path.join(CACHE_DIR, f"{key}.json")
    
    data = {
        'timestamp': time.time(),
        'data_type': data_type,
        'params': params,
        'value': value
    }
    
    with open(cache_file, 'w') as f:
        json.dump(data, f)

def clear_old_cache(max_age_hours: int = 24):
    """清理过期缓存"""
    ensure_cache_dir()
    cleared = 0
    
    for filename in os.listdir(CACHE_DIR):
        if filename.endswith('.json'):
            filepath = os.path.join(CACHE_DIR, filename)
            try:
                with open(filepath, 'r') as f:
                    data = json.load(f)
                    age_hours = (time.time() - data.get('timestamp', 0)) / 3600
                    if age_hours > max_age_hours:
                        os.remove(filepath)
                        cleared += 1
            except:
                pass
    
    return cleared


# 测试
if __name__ == "__main__":
    # 测试缓存
    test_params = {'date': '20260605', 'type': 'zt_pool'}
    
    # 写入
    write_cache('zt_pool', test_params, {'count': 73, 'data': []})
    print("✅ 缓存已写入")
    
    # 读取
    cached = read_cache('zt_pool', test_params)
    print(f"缓存读取: {cached}")
    
    # 清理
    cleared = clear_old_cache(0)  # 清理所有
    print(f"清理缓存: {cleared}个文件")
