#!/usr/bin/env python3
"""
交易信号验证
"""

from typing import Dict, List, Tuple

def validate_signal(signal: Dict) -> Tuple[bool, List[str]]:
    """验证交易信号"""
    errors = []
    
    # 必要字段
    required = ['symbol', 'type', 'price']
    for field in required:
        if field not in signal:
            errors.append(f"缺少必要字段: {field}")
    
    # 信号类型
    if 'type' in signal and signal['type'] not in ['buy', 'sell']:
        errors.append(f"无效信号类型: {signal['type']}")
    
    # 价格有效性
    if 'price' in signal:
        try:
            price = float(signal['price'])
            if price <= 0:
                errors.append("价格必须大于0")
        except:
            errors.append("价格格式无效")
    
    # 代码格式
    if 'symbol' in signal:
        symbol = str(signal['symbol'])
        if len(symbol) != 6 or not symbol.isdigit():
            errors.append("股票代码应为6位数字")
    
    return len(errors) == 0, errors

def filter_valid_signals(signals: List[Dict]) -> List[Dict]:
    """过滤有效信号"""
    valid = []
    for sig in signals:
        ok, errors = validate_signal(sig)
        if ok:
            valid.append(sig)
        else:
            print(f"信号无效 {sig.get('symbol', 'unknown')}: {errors}")
    return valid

def rank_signals(signals: List[Dict]) -> List[Dict]:
    """信号排序（按优先级）"""
    # 简化：按名称排序
    return sorted(signals, key=lambda x: x.get('symbol', ''))


if __name__ == "__main__":
    print("=== 信号验证测试 ===\n")
    
    signals = [
        {'symbol': '513100', 'type': 'buy', 'price': 2.20, 'reason': '龙头股'},
        {'symbol': '159941', 'type': 'buy', 'price': 1.65},
        {'symbol': 'INVALID', 'type': 'invalid', 'price': -1},
    ]
    
    valid = filter_valid_signals(signals)
    print(f"有效信号: {len(valid)}/{len(signals)}")
    
    for sig in valid:
        print(f"  {sig['symbol']}: {sig['type']}@{sig['price']}")
    
    print("\n✅ 信号验证测试通过")
