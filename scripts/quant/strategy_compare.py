#!/usr/bin/env python3
"""
策略对比分析
比较不同策略参数的表现
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest_engine import BacktestConfig, DataFetcher
from datetime import datetime

def compare_strategies():
    """对比不同策略参数"""
    print("=" * 60)
    print("策略对比分析")
    print("=" * 60)
    
    # 策略配置
    strategies = [
        {"name": "保守型", "stop_loss": 0.05, "take_profit": 0.15, "max_position": 0.30},
        {"name": "平衡型", "stop_loss": 0.08, "take_profit": 0.20, "max_position": 0.40},
        {"name": "激进型", "stop_loss": 0.05, "take_profit": 0.30, "max_position": 0.50},
    ]
    
    # 假设胜率60%
    winrate = 0.60
    
    results = []
    for s in strategies:
        # 计算期望收益
        expected = winrate * s['take_profit'] - (1 - winrate) * s['stop_loss']
        breakeven = s['stop_loss'] / (s['stop_loss'] + s['take_profit'])
        
        results.append({
            'name': s['name'],
            'stop_loss': s['stop_loss'] * 100,
            'take_profit': s['take_profit'] * 100,
            'max_position': s['max_position'] * 100,
            'expected': expected * 100,
            'breakeven': breakeven * 100
        })
    
    # 输出对比表
    print(f"{'策略':<10} {'止损':>6} {'止盈':>6} {'仓位':>6} {'期望收益':>10} {'盈亏平衡':>10}")
    print("-" * 60)
    
    for r in results:
        print(f"{r['name']:<10} {r['stop_loss']:>5.0f}% {r['take_profit']:>5.0f}% {r['max_position']:>5.0f}% {r['expected']:>9.1f}% {r['breakeven']:>9.1f}%")
    
    # 推荐
    best = max(results, key=lambda x: x['expected'])
    print("-" * 60)
    print(f"推荐: {best['name']}（期望收益 {best['expected']:.1f}%）")
    
    return results


if __name__ == "__main__":
    compare_strategies()
