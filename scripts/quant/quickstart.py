#!/usr/bin/env python3
"""
快速启动脚本
一键运行常用功能
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

def quick_backtest():
    """快速回测"""
    from strategy_runner import run_strategy_backtest
    result = run_strategy_backtest("20260603", "20260606")
    print(f"回测完成: {result}")

def quick_market():
    """快速市场查看"""
    from market_monitor import get_market_status
    status = get_market_status()
    print(f"市场状态: {status}")

def quick_optimize():
    """快速参数优化"""
    from strategy_optimize import grid_search_params
    grid_search_params()

def quick_test():
    """快速测试"""
    from test_backtest import test_config, test_trading_simulator, test_performance_calculator
    test_config()
    test_trading_simulator()
    test_performance_calculator()
    print("\n所有测试通过")


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="回测框架快速启动")
    parser.add_argument('command', choices=['backtest', 'market', 'optimize', 'test'],
                       help='命令: backtest, market, optimize, test')
    
    args = parser.parse_args()
    
    if args.command == 'backtest':
        quick_backtest()
    elif args.command == 'market':
        quick_market()
    elif args.command == 'optimize':
        quick_optimize()
    elif args.command == 'test':
        quick_test()
