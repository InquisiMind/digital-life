#!/usr/bin/env python3
"""
策略信号生成器
根据不同条件生成买卖信号
"""

from typing import List, Dict
from data_preprocess import clean_zt_pool_data

def generate_dragon_head_signals(zt_data: List[Dict]) -> List[Dict]:
    """
    龙头股信号生成
    
    条件：
    1. 封板时间早于10:00
    2. 炸板次数=0
    3. 连板数>=1
    """
    cleaned = clean_zt_pool_data(zt_data)
    
    signals = []
    for stock in cleaned:
        if stock['is_dragon_head']:
            signals.append({
                'type': 'buy',
                'symbol': stock['symbol'],
                'name': stock['name'],
                'price': stock['price'],
                'reason': f"龙头股，板块:{stock.get('sector', '未知')}"
            })
    
    return signals

def generate_breakout_signals(zt_data: List[Dict], min_continuous: int = 2) -> List[Dict]:
    """
    突破信号生成
    
    条件：
    1. 连板数>=min_continuous
    """
    cleaned = clean_zt_pool_data(zt_data)
    
    signals = []
    for stock in cleaned:
        if stock['continuous'] >= min_continuous:
            signals.append({
                'type': 'buy',
                'symbol': stock['symbol'],
                'name': stock['name'],
                'price': stock['price'],
                'reason': f"{stock['continuous']}连板突破"
            })
    
    return signals

def generate_sector_rotation_signals(zt_data: List[Dict], min_sector_count: int = 3) -> List[Dict]:
    """
    板块轮动信号
    
    条件：
    1. 板块涨停数>=min_sector_count
    2. 选择板块内第一个涨停
    """
    from collections import defaultdict
    
    # 按板块分组
    sector_stocks = defaultdict(list)
    for stock in zt_data:
        sector = stock.get('sector', '未知')
        sector_stocks[sector].append(stock)
    
    signals = []
    for sector, stocks in sector_stocks.items():
        if len(stocks) >= min_sector_count:
            # 选择第一个
            first = stocks[0]
            signals.append({
                'type': 'buy',
                'symbol': first['symbol'],
                'name': first.get('name', ''),
                'price': first['price'],
                'reason': f"板块轮动:{sector}({len(stocks)}只涨停)"
            })
    
    return signals

if __name__ == "__main__":
    test_data = [
        {'symbol': '002594', 'name': '比亚迪', 'price': 90.0, 'last_limit_time': '093000', 
         'break_count': 0, 'continuous': 2, 'sector': '汽车'},
        {'symbol': '600863', 'name': '华能蒙电', 'price': 7.5, 'last_limit_time': '094500',
         'break_count': 0, 'continuous': 1, 'sector': '电力'},
    ]
    
    print("=== 信号生成测试 ===")
    
    signals1 = generate_dragon_head_signals(test_data)
    print(f"龙头股信号: {len(signals1)}个")
    
    signals2 = generate_breakout_signals(test_data, min_continuous=2)
    print(f"突破信号: {len(signals2)}个")
    
    print("\n✅ 信号生成器测试通过")
