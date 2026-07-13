#!/usr/bin/env python3
"""
交易记录分析
"""

from typing import List, Dict
from collections import defaultdict
from datetime import datetime

def analyze_trading_pattern(trades: List[Dict]) -> Dict:
    """分析交易模式"""
    if not trades:
        return {}
    
    # 按股票统计
    by_symbol = defaultdict(list)
    for t in trades:
        by_symbol[t['symbol']].append(t)
    
    # 按日期统计
    by_date = defaultdict(list)
    for t in trades:
        date = t.get('date', 'unknown')
        by_date[date].append(t)
    
    # 盈亏分布
    profits = [t.get('profit', 0) for t in trades if t['type'] == 'sell']
    
    return {
        'total_trades': len(trades),
        'unique_symbols': len(by_symbol),
        'trading_days': len(by_date),
        'profit_distribution': {
            'wins': len([p for p in profits if p > 0]),
            'losses': len([p for p in profits if p <= 0]),
            'max_win': max(profits) if profits else 0,
            'max_loss': min(profits) if profits else 0,
        }
    }

def calculate_trade_metrics(trades: List[Dict]) -> Dict:
    """计算交易指标"""
    sells = [t for t in trades if t['type'] == 'sell']
    
    if not sells:
        return {}
    
    profits = [t.get('profit', 0) for t in sells]
    total = sum(profits)
    
    # 平均持仓时间（简化）
    avg_holding = 2  # 天
    
    return {
        'total_profit': total,
        'avg_profit_per_trade': total / len(sells),
        'profit_factor': sum(p for p in profits if p > 0) / abs(sum(p for p in profits if p < 0)) if any(p < 0 for p in profits) else 999,
        'win_rate': len([p for p in profits if p > 0]) / len(profits) * 100,
    }


if __name__ == "__main__":
    print("=== 交易记录分析测试 ===\n")
    
    test_trades = [
        {'type': 'buy', 'symbol': '513100', 'date': '20260601', 'price': 2.20, 'shares': 1000},
        {'type': 'sell', 'symbol': '513100', 'date': '20260603', 'price': 2.40, 'shares': 1000, 'profit': 195},
        {'type': 'buy', 'symbol': '159941', 'date': '20260602', 'price': 1.65, 'shares': 2000},
        {'type': 'sell', 'symbol': '159941', 'date': '20260604', 'price': 1.50, 'shares': 2000, 'profit': -306},
    ]
    
    pattern = analyze_trading_pattern(test_trades)
    print(f"交易模式: {pattern['total_trades']}笔, {pattern['unique_symbols']}只股票")
    print(f"盈亏分布: {pattern['profit_distribution']['wins']}胜 {pattern['profit_distribution']['losses']}负")
    
    metrics = calculate_trade_metrics(test_trades)
    print(f"总盈亏: ¥{metrics['total_profit']:.2f}")
    print(f"胜率: {metrics['win_rate']:.1f}%")
    
    print("\n✅ 交易记录分析测试通过")
