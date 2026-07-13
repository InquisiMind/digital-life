#!/usr/bin/env python3
"""
回测结果深度分析
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest_engine import BacktestConfig, DataFetcher
from performance_analyzer import analyze_performance, get_optimization_suggestions

def analyze_single_day(date: str):
    """分析单日涨停板数据"""
    print(f"=== {date} 涨停板分析 ===\n")
    
    zt_pool = DataFetcher.get_zt_pool(date)
    
    if not zt_pool:
        print("无数据")
        return
    
    # 按板块统计
    sectors = {}
    for stock in zt_pool:
        sector = stock.get('sector', '未知')
        sectors[sector] = sectors.get(sector, 0) + 1
    
    # 按连板数统计
    continuous = {}
    for stock in zt_pool:
        c = stock.get('continuous', 1)
        continuous[c] = continuous.get(c, 0) + 1
    
    # 输出
    print(f"总涨停: {len(zt_pool)}只")
    print(f"\n板块分布 (TOP 5):")
    for s, count in sorted(sectors.items(), key=lambda x: x[1], reverse=True)[:5]:
        print(f"  {s}: {count}只")
    
    print(f"\n连板分布:")
    for c in sorted(continuous.keys()):
        print(f"  {c}连板: {continuous[c]}只")


if __name__ == "__main__":
    analyze_single_day("20260605")
