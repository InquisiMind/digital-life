#!/usr/bin/env python3
"""
回测执行脚本 v2
完整的策略回测，包含买入、持有、止损止盈检查
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest_engine import (
    BacktestEngine, BacktestConfig, DataFetcher,
    TradingSimulator, PerformanceCalculator
)

def run_backtest_v2(start_date: str, end_date: str, max_positions: int = 3):
    """
    完整回测v2
    
    包含：
    1. 信号生成
    2. 买入执行
    3. 持仓检查（止损止盈）
    4. 卖出执行
    5. 绩效统计
    """
    from datetime import datetime, timedelta
    
    print("=" * 60)
    print("回测框架 v2.0 - 完整策略执行")
    print("=" * 60)
    
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
    
    print(f"回测期间: {start_date} ~ {end_date}")
    print(f"交易日数: {len(trade_days)}")
    print(f"初始资金: ¥{config.INITIAL_CAPITAL:,.2f}")
    print(f"止损: {config.STOP_LOSS*100}% | 止盈: {config.TAKE_PROFIT*100}%")
    print("=" * 60)
    
    all_signals = []
    portfolio_values = [config.INITIAL_CAPITAL]
    
    for day in trade_days:
        # 1. 检查现有持仓的止损止盈
        for pos in simulator.positions[:]:
            # 获取当日价格（简化：用涨停板数据中的价格）
            zt_pool = DataFetcher.get_zt_pool(day)
            price = None
            for stock in zt_pool:
                if stock['symbol'] == pos['symbol']:
                    price = stock['price']
                    break
            
            if not price:
                # 如果不在涨停板，保持原成本价
                price = pos['cost_price']
            
            # 检查止损
            loss_pct = (price - pos['cost_price']) / pos['cost_price']
            if loss_pct <= -config.STOP_LOSS:
                simulator.execute_sell(pos['symbol'], price, pos['shares'], f'止损({loss_pct*100:.1f}%)')
                print(f"{day}: 止损卖出 {pos['symbol']} @ {price:.2f}")
            # 检查止盈
            elif loss_pct >= config.TAKE_PROFIT:
                simulator.execute_sell(pos['symbol'], price, pos['shares'], f'止盈({loss_pct*100:.1f}%)')
                print(f"{day}: 止盈卖出 {pos['symbol']} @ {price:.2f}")
        
        # 2. 生成买入信号
        zt_pool = DataFetcher.get_zt_pool(day)
        if not zt_pool:
            continue
        
        signals = []
        for stock in zt_pool:
            # 龙头股筛选
            limit_time = str(stock.get('last_limit_time', ''))
            if limit_time and limit_time.isdigit():
                hour = int(limit_time[:2]) if len(limit_time) >= 2 else 99
                if hour >= 10:
                    continue
            if stock.get('break_count', 0) > 0:
                continue
            if stock.get('continuous', 0) < 1:
                continue
            
            signals.append({
                'symbol': stock['symbol'],
                'name': stock['name'],
                'price': stock['price']
            })
        
        all_signals.extend(signals)
        
        # 3. 执行买入
        if len(simulator.positions) < max_positions and signals:
            for sig in signals:
                if len(simulator.positions) >= max_positions:
                    break
                
                # 检查是否已持有
                if any(p['symbol'] == sig['symbol'] for p in simulator.positions):
                    continue
                
                # 计算买入数量
                position_value = config.INITIAL_CAPITAL * config.MAX_POSITION
                shares = int(position_value / sig['price'] / 100) * 100
                
                if shares > 0:
                    ok = simulator.execute_buy(sig['symbol'], sig['price'], shares)
                    if ok:
                        print(f"{day}: 买入 {sig['name']}({sig['symbol']}) x{shares}@{sig['price']:.2f}")
        
        # 记录当日资产
        portfolio_values.append(simulator.get_portfolio_value({}))
    
    # 最终统计
    final_value = simulator.get_portfolio_value({})
    
    print("=" * 60)
    print("回测结果")
    print("=" * 60)
    print(f"总信号: {len(all_signals)}个")
    print(f"买入次数: {sum(1 for t in simulator.trades if t['type']=='buy')}笔")
    print(f"卖出次数: {sum(1 for t in simulator.trades if t['type']=='sell')}笔")
    print(f"最终资产: ¥{final_value:,.2f}")
    print(f"收益率: {(final_value/config.INITIAL_CAPITAL - 1)*100:.2f}%")
    
    # 绩效计算
    perf = PerformanceCalculator.calculate(simulator.trades, portfolio_values)
    print(f"\n绩效指标:")
    print(f"  胜率: {perf.get('win_rate', 0):.1f}%")
    print(f"  盈亏比: {perf.get('profit_ratio', 0):.2f}")
    print(f"  最大回撤: {perf.get('max_drawdown', 0):.1f}%")
    
    return {
        'signals': len(all_signals),
        'trades': len(simulator.trades),
        'final_value': final_value,
        'return_pct': (final_value/config.INITIAL_CAPITAL - 1)*100,
        'performance': perf
    }


if __name__ == "__main__":
    result = run_backtest_v2("20260603", "20260606")
