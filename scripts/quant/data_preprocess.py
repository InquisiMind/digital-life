#!/usr/bin/env python3
"""
数据预处理模块
清洗和标准化数据
"""

from typing import List, Dict

def clean_zt_pool_data(raw_data: List[Dict]) -> List[Dict]:
    """清洗涨停板数据"""
    cleaned = []
    for stock in raw_data:
        try:
            if not stock.get('symbol') or not stock.get('price'):
                continue
            
            item = {
                'symbol': str(stock.get('symbol', '')).zfill(6),
                'name': stock.get('name', ''),
                'price': float(stock.get('price', 0)),
                'change_pct': float(stock.get('change_pct', 0)),
                'last_limit_time': str(stock.get('last_limit_time', '')),
                'break_count': int(stock.get('break_count', 0)),
                'continuous': int(stock.get('continuous', 1)),
                'sector': stock.get('sector', ''),
            }
            
            if item['last_limit_time'] and item['last_limit_time'].isdigit():
                hour = int(item['last_limit_time'][:2])
                item['early_limit'] = hour < 10
            else:
                item['early_limit'] = False
            
            item['is_dragon_head'] = item['early_limit'] and item['break_count'] == 0
            
            cleaned.append(item)
        except:
            continue
    
    return cleaned


if __name__ == "__main__":
    test = [{'symbol': '002594', 'name': '比亚迪', 'price': 90.0, 'change_pct': 10.0, 
             'last_limit_time': '093000', 'break_count': 0, 'continuous': 2}]
    cleaned = clean_zt_pool_data(test)
    print(f"清洗后: {len(cleaned)}条, 龙头股: {cleaned[0]['is_dragon_head']}")
