"""
ETF价格查询模块 v2
使用腾讯接口获取实时价格，支持缓存
"""

import urllib.request
import json
import os
import time
from datetime import datetime

# 缓存配置
CACHE_DIR = os.path.expanduser("~/.etf_cache")
CACHE_TTL = 60  # 缓存有效期60秒

# 腾讯行情接口
TENCENT_URL = "https://qt.gtimg.cn/q="

# ETF代码映射（简化代码 -> 腾讯格式）
CODE_MAP = {
    "513100": "sh513100",  # 纳指ETF
    "159941": "sz159941",  # 纳指ETF（深）
    "510300": "sh510300",  # 沪深300ETF
    "510500": "sh510500",  # 中证500ETF
}

def ensure_cache_dir():
    """确保缓存目录存在"""
    if not os.path.exists(CACHE_DIR):
        os.makedirs(CACHE_DIR)

def get_cache_path(code):
    """获取缓存文件路径"""
    return os.path.join(CACHE_DIR, f"{code}.json")

def read_cache(code):
    """读取缓存"""
    cache_file = get_cache_path(code)
    if os.path.exists(cache_file):
        try:
            with open(cache_file, 'r') as f:
                data = json.load(f)
                if time.time() - data.get('timestamp', 0) < CACHE_TTL:
                    return data
        except:
            pass
    return None

def write_cache(code, data):
    """写入缓存"""
    ensure_cache_dir()
    cache_file = get_cache_path(code)
    data['timestamp'] = time.time()
    with open(cache_file, 'w') as f:
        json.dump(data, f)

def parse_tencent_response(content, code):
    """解析腾讯接口响应"""
    # 腾讯返回格式：v_sh513100="1~纳指ETF~513100~~2.205~~..."
    lines = content.strip().split('\n')
    for line in lines:
        if f'_{code}="' in line or f'_{CODE_MAP.get(code, code)}="' in line:
            # 提取引号内内容
            start = line.index('"') + 1
            end = line.rindex('"')
            parts = line[start:end].split('~')
            
            if len(parts) >= 35:
                return {
                    'name': parts[1],
                    'code': code,
                    'price': float(parts[3]),
                    'pre_close': float(parts[4]),
                    'open': float(parts[5]),
                    'high': float(parts[33]),
                    'low': float(parts[34]),
                    'change_pct': round((float(parts[3]) - float(parts[4])) / float(parts[4]) * 100, 2),
                    'time': datetime.now().isoformat(),
                    'source': 'tencent'
                }
    return None

def get_etf_price(code, use_cache=True):
    """
    获取单个ETF价格
    
    Args:
        code: ETF代码（6位数字或带前缀）
        use_cache: 是否使用缓存
    
    Returns:
        dict: {'name', 'price', 'open', 'high', 'low', 'pre_close', 'change_pct', 'time'}
    """
    # 标准化代码
    if len(code) == 6 and code.isdigit():
        tencent_code = CODE_MAP.get(code, f"sh{code}")
    else:
        tencent_code = code
    
    # 检查缓存
    if use_cache:
        cached = read_cache(code)
        if cached:
            cached['from_cache'] = True
            return cached
    
    # 请求腾讯接口
    url = f"{TENCENT_URL}{tencent_code}"
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0'
        })
        with urllib.request.urlopen(req, timeout=5) as resp:
            content = resp.read().decode('gbk')
            result = parse_tencent_response(content, code)
            if result:
                if use_cache:
                    write_cache(code, result)
                result['from_cache'] = False
                return result
    except Exception as e:
        return {'error': str(e), 'code': code}
    
    return {'error': 'no data', 'code': code}

def get_etf_prices(codes, use_cache=True):
    """批量获取多个ETF价格"""
    return {code: get_etf_price(code, use_cache) for code in codes}

if __name__ == "__main__":
    # 测试
    codes = ['513100', '159941']
    print("ETF价格查询测试（腾讯接口 + 缓存）")
    print("=" * 50)
    for code, data in get_etf_prices(codes).items():
        if 'error' in data:
            print(f"{code}: 错误 - {data['error']}")
        else:
            print(f"{data['name']} ({code}): ¥{data['price']} ({data['change_pct']:+.2f}%)")
            print(f"  开盘: {data['open']}  最高: {data['high']}  最低: {data['low']}")
            print(f"  昨收: {data['pre_close']}  缓存: {data.get('from_cache', False)}")
            print()
