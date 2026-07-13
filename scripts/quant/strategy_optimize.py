#!/usr/bin/env python3
"""
策略参数优化
网格搜索寻找最优参数
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest_engine import BacktestConfig

def grid_search_params():
    """
    网格搜索最优参数
    测试不同的止损止盈组合
    """
    print("=" * 60)
    print("策略参数优化")
    print("=" * 60)
    
    # 参数范围
    stop_loss_range = [0.05, 0.08, 0.10]  # 5%, 8%, 10%
    take_profit_range = [0.15, 0.20, 0.30]  # 15%, 20%, 30%
    
    print(f"止损范围: {[f'{s*100:.0f}%' for s in stop_loss_range]}")
    print(f"止盈范围: {[f'{t*100:.0f}%' for t in take_profit_range]}")
    print("-" * 60)
    
    results = []
    
    for sl in stop_loss_range:
        for tp in take_profit_range:
            # 计算盈亏平衡胜率
            breakeven = sl / (sl + tp)
            
            # 期望收益（假设胜率60%）
            winrate = 0.60
            expected = winrate * tp - (1 - winrate) * sl
            
            results.append({
                'stop_loss': sl,
                'take_profit': tp,
                'breakeven': breakeven,
                'expected': expected
            })
            
            print(f"止损{sl*100:.0f}% + 止盈{tp*100:.0f}%: 盈亏平衡{breakeven*100:.1f}%, 期望{expected*100:.1f}%")
    
    # 找最优
    best = max(results, key=lambda x: x['expected'])
    print("-" * 60)
    print(f"最优参数: 止损{best['stop_loss']*100:.0f}%, 止盈{best['take_profit']*100:.0f}%")
    print(f"期望收益: {best['expected']*100:.1f}%")
    
    return results


if __name__ == "__main__":
    grid_search_params()
