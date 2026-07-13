#!/usr/bin/env python3
"""
回测框架使用示例
演示如何使用各个模块
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest_engine import BacktestConfig, TradingSimulator, DataFetcher

def example_basic_backtest():
    """基础回测示例"""
    print("=== 基础回测示例 ===\n")
    
    # 1. 创建配置
    config = BacktestConfig()
    print(f"配置: 止损{config.STOP_LOSS*100}%, 止盈{config.TAKE_PROFIT*100}%")
    
    # 2. 获取数据
    zt_pool = DataFetcher.get_zt_pool("20260605")
    print(f"涨停板数据: {len(zt_pool)}只")
    
    # 3. 创建交易模拟器
    sim = TradingSimulator(config)
    
    # 4. 模拟买入
    if zt_pool:
        stock = zt_pool[0]
        sim.execute_buy(stock['symbol'], stock['price'], 100)
        print(f"买入: {stock['name']} x100@{stock['price']}")
    
    print()


def example_signal_generation():
    """信号生成示例"""
    from signal_generator import generate_dragon_head_signals
    
    print("=== 信号生成示例 ===\n")
    
    zt_pool = DataFetcher.get_zt_pool("20260605")
    signals = generate_dragon_head_signals(zt_pool)
    
    print(f"龙头股信号: {len(signals)}个")
    for s in signals[:3]:
        print(f"  {s['name']}({s['symbol']}): {s['reason']}")
    
    print()


def example_market_monitor():
    """市场监控示例"""
    from market_monitor import get_market_status
    
    print("=== 市场监控示例 ===\n")
    
    status = get_market_status()
    print(f"市场强度: {status.get('market_strength', 'N/A')}")
    print(f"风险等级: {status.get('risk_level', 'N/A')}")
    
    print()


if __name__ == "__main__":
    example_basic_backtest()
    example_signal_generation()
    example_market_monitor()
    
    print("✅ 示例运行完成")
