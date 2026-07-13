#!/usr/bin/env python3
"""
回测框架主入口
"""

import sys
import os

# 添加路径
sys.path.insert(0, os.path.dirname(__file__))

from run_backtest import run_backtest_v2
from test_backtest import *
from strategy_optimize import grid_search_params
from strategy_compare import compare_strategies
from market_monitor import get_market_status

def main():
    """主入口"""
    print("=" * 60)
    print("回测框架 v2.0")
    print("=" * 60)
    print()
    print("可用命令:")
    print("  1. 运行回测")
    print("  2. 参数优化")
    print("  3. 策略对比")
    print("  4. 市场状态")
    print("  5. 运行测试")
    print()
    
    choice = input("请选择 (1-5): ").strip()
    
    if choice == '1':
        run_backtest_v2("20260603", "20260606")
    elif choice == '2':
        grid_search_params()
    elif choice == '3':
        compare_strategies()
    elif choice == '4':
        status = get_market_status()
        print(f"市场状态: {status}")
    elif choice == '5':
        test_config()
        test_trading_simulator()
        test_performance_calculator()
        print("所有测试通过")
    else:
        print("无效选择")


if __name__ == "__main__":
    main()
