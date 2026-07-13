
"""
ETF价格查询模块
使用新浪接口获取实时价格
"""

import urllib.request
import re
from datetime import datetime

HEADERS = {
    'Referer': 'http://finance.sina.com.cn',
    'User-Agent': 'Mozilla/5.0'
}

def get_etf_price(code):
    """
    获取单个ETF价格
    
    Args:
        code: ETF代码（带交易所前缀），如 'sh513100', 'sz159941'
    
    Returns:
        dict: {'name', 'price', 'open', 'pre_close', 'change_pct', 'time'}
    """
    url = f"http://hq.sinajs.cn/list={code}"
    
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=5) as resp:
            content = resp.read().decode('gbk')
            match = re.search(r'="([^"]+)"', content)
            if match:
                parts = match.group(1).split(',')
                if len(parts) >= 4:
                    price = float(parts[3])
                    pre_close = float(parts[2])
                    return {
                        'name': parts[0],
                        'price': price,
                        'open': float(parts[1]),
                        'pre_close': pre_close,
                        'change_pct': round((price - pre_close) / pre_close * 100, 2),
                        'time': datetime.now().isoformat()
                    }
    except Exception as e:
        return {'error': str(e)}
    
    return {'error': 'no data'}

def get_etf_prices(codes):
    """批量获取多个ETF价格"""
    return {code: get_etf_price(code) for code in codes}

if __name__ == "__main__":
    # 测试
    codes = ['sh513100', 'sz159941']
    for code, data in get_etf_prices(codes).items():
        print(f"{code}: {data}")
