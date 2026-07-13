#!/usr/bin/env python3
"""
回测框架单元测试
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest_engine import (
    BacktestConfig, TradingSimulator, 
    PerformanceCalculator, DataFetcher
)
from data_preprocess import clean_zt_pool_data

def test_config():
    """测试配置"""
    config = BacktestConfig()
    assert config.STOP_LOSS == 0.08
    assert config.TAKE_PROFIT == 0.20
    assert config.MAX_POSITION == 0.50
    print("✅ 配置测试通过")

def test_trading_simulator():
    """测试交易模拟器"""
    config = BacktestConfig()
    sim = TradingSimulator(config)
    
    # 测试买入
    ok = sim.execute_buy('513100', 2.20, 1000)
    assert ok == True
    assert len(sim.positions) == 1
    assert sim.cash < config.INITIAL_CAPITAL
    
    # 测试卖出
    ok = sim.execute_sell('513100', 2.40, 1000, '止盈')
    assert ok == True
    assert len(sim.positions) == 0
    assert len(sim.trades) == 2
    
    print("✅ 交易模拟器测试通过")

def test_performance_calculator():
    """测试绩效计算"""
    trades = [
        {'type': 'sell', 'profit': 100},
        {'type': 'sell', 'profit': -50},
    ]
    values = [100000, 100050]
    
    perf = PerformanceCalculator.calculate(trades, values)
    assert perf['win_rate'] == 50.0
    
    print("✅ 绩效计算测试通过")

def test_data_preprocess():
    """测试数据预处理"""
    raw = [
        {'symbol': '002594', 'name': '比亚迪', 'price': 90.0, 
         'last_limit_time': '093000', 'break_count': 0, 'continuous': 2}
    ]
    
    cleaned = clean_zt_pool_data(raw)
    assert len(cleaned) == 1
    assert cleaned[0]['is_dragon_head'] == True
    
    print("✅ 数据预处理测试通过")


if __name__ == "__main__":
    print("=" * 50)
    print("回测框架单元测试")
    print("=" * 50)
    
    test_config()
    test_trading_simulator()
    test_performance_calculator()
    test_data_preprocess()
    
    print("=" * 50)
    print("所有测试通过")
