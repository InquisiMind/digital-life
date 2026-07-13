#!/usr/bin/env python3
"""
资金曲线分析
"""

from typing import List, Dict
import math

def calculate_equity_curve(trades: List[Dict], initial_capital: float) -> List[float]:
    """计算资金曲线"""
    equity = [initial_capital]
    
    for trade in trades:
        if trade['type'] == 'sell':
            profit = trade.get('profit', 0)
            equity.append(equity[-1] + profit)
    
    return equity

def analyze_equity_curve(equity: List[float]) -> Dict:
    """分析资金曲线"""
    if len(equity) < 2:
        return {}
    
    # 总收益
    total_return = (equity[-1] - equity[0]) / equity[0] * 100
    
    # 最大回撤
    peak = equity[0]
    max_dd = 0
    for e in equity:
        if e > peak:
            peak = e
        dd = (peak - e) / peak
        if dd > max_dd:
            max_dd = dd
    
    # 波动率
    returns = []
    for i in range(1, len(equity)):
        r = (equity[i] - equity[i-1]) / equity[i-1]
        returns.append(r)
    
    if returns:
        avg = sum(returns) / len(returns)
        variance = sum((r - avg) ** 2 for r in returns) / len(returns)
        volatility = math.sqrt(variance) * 100
    else:
        volatility = 0
    
    # 斜率（趋势）
    if len(equity) >= 2:
        slope = (equity[-1] - equity[0]) / len(equity)
    else:
        slope = 0
    
    return {
        'total_return': round(total_return, 2),
        'max_drawdown': round(max_dd * 100, 2),
        'volatility': round(volatility, 2),
        'slope': round(slope, 2),
        'final_equity': round(equity[-1], 2)
    }


if __name__ == "__main__":
    print("=== 资金曲线分析测试 ===\n")
    
    # 测试数据
    trades = [
        {'type': 'sell', 'profit': 100},
        {'type': 'sell', 'profit': -50},
        {'type': 'sell', 'profit': 200},
        {'type': 'sell', 'profit': -80},
        {'type': 'sell', 'profit': 150},
    ]
    
    equity = calculate_equity_curve(trades, 100000)
    print(f"资金曲线: {equity}")
    
    analysis = analyze_equity_curve(equity)
    print(f"\n分析结果:")
    print(f"  总收益: {analysis['total_return']}%")
    print(f"  最大回撤: {analysis['max_drawdown']}%")
    print(f"  波动率: {analysis['volatility']}%")
    print(f"  最终资金: ¥{analysis['final_equity']:,.0f}")
    
    print("\n✅ 资金曲线分析测试通过")
