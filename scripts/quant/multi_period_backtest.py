#!/usr/bin/env python3
"""
多周期回测分析
对比不同时间段的策略表现
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from run_backtest import run_backtest_v2

def multi_period_analysis():
    """多周期回测对比"""
    from datetime import datetime
    
    print("=" * 60)
    print("多周期回测分析")
    print("=" * 60)
    
    # 测试不同周期
    periods = [
        ("20260603", "20260606", "短期(3天)"),
    ]
    
    results = []
    for start, end, label in periods:
        print(f"\n测试周期: {label}")
        print("-" * 40)
        try:
            result = run_backtest_v2(start, end, max_positions=3)
            result['label'] = label
            results.append(result)
        except Exception as e:
            print(f"错误: {e}")
    
    # 汇总结果
    print("\n" + "=" * 60)
    print("汇总对比")
    print("=" * 60)
    
    for r in results:
        print(f"{r.get('label', 'N/A')}: 收益率 {r.get('return_pct', 0):.2f}%")
    
    return results


if __name__ == "__main__":
    multi_period_analysis()
