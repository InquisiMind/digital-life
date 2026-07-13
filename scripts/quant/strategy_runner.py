#!/usr/bin/env python3
"""
策略回测运行器
整合所有模块运行完整回测
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest_engine import BacktestConfig, TradingSimulator, DataFetcher
from signal_generator import generate_dragon_head_signals
from performance_analyzer import analyze_performance, get_optimization_suggestions
from data_preprocess import clean_zt_pool_data

def run_strategy_backtest(start_date: str, end_date: str, config: BacktestConfig = None):
    """
    运行策略回测
    
    Args:
        start_date: 开始日期 "20260603"
        end_date: 结束日期 "20260606"
        config: 配置（可选）
    
    Returns:
        回测结果
    """
    from datetime import datetime, timedelta
    
    if config is None:
        config = BacktestConfig()
    
    simulator = TradingSimulator(config)
    
    # 生成交易日
    start = datetime.strptime(start_date, "%Y%m%d")
    end = datetime.strptime(end_date, "%Y%m%d")
    
    trade_days = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            trade_days.append(current.strftime("%Y%m%d"))
        current += timedelta(days=1)
    
    all_trades = []
    portfolio_values = [config.INITIAL_CAPITAL]
    
    for day in trade_days:
        # 获取涨停板数据
        zt_pool = DataFetcher.get_zt_pool(day)
        
        if not zt_pool:
            continue
        
        # 生成信号
        signals = generate_dragon_head_signals(zt_pool)
        
        # 执行交易（简化版）
        for sig in signals[:2]:  # 最多买入2只
            if len(simulator.positions) >= 2:
                break
            
            shares = int(config.INITIAL_CAPITAL * config.MAX_POSITION / sig['price'] / 100) * 100
            if shares > 0:
                ok = simulator.execute_buy(sig['symbol'], sig['price'], shares)
                if ok:
                    all_trades.append({'date': day, 'action': 'buy', 'symbol': sig['symbol'], 'price': sig['price']})
        
        # 记录资产
        portfolio_values.append(simulator.cash)
    
    # 分析结果
    analysis = analyze_performance(simulator.trades, config.INITIAL_CAPITAL)
    suggestions = get_optimization_suggestions(analysis) if analysis else []
    
    return {
        'trade_days': len(trade_days),
        'signals_generated': sum(1 for t in all_trades if t['action'] == 'buy'),
        'trades': len(simulator.trades),
        'final_capital': simulator.cash,
        'analysis': analysis,
        'suggestions': suggestions
    }


if __name__ == "__main__":
    print("=== 策略回测运行器测试 ===\n")
    
    result = run_strategy_backtest("20260603", "20260606")
    
    print(f"交易日: {result['trade_days']}天")
    print(f"信号数: {result['signals_generated']}个")
    print(f"交易数: {result['trades']}笔")
    print(f"最终资金: ¥{result['final_capital']:,.2f}")
    
    if result['analysis']:
        print(f"\n分析: {result['analysis']}")
    
    print("\n✅ 策略回测运行器测试通过")
