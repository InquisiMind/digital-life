#!/usr/bin/env python3
"""
性能分析模块
分析策略表现和优化建议
"""

from typing import Dict, List
import math

def analyze_performance(trades: List[Dict], initial_capital: float) -> Dict:
    """分析交易表现"""
    if not trades:
        return {'error': '无交易数据'}
    
    sells = [t for t in trades if t['type'] == 'sell']
    
    if not sells:
        return {'error': '无卖出记录'}
    
    # 基本统计
    profits = [t.get('profit', 0) for t in sells]
    wins = [p for p in profits if p > 0]
    losses = [p for p in profits if p <= 0]
    
    total_profit = sum(profits)
    avg_profit = total_profit / len(sells)
    
    win_rate = len(wins) / len(sells) * 100 if sells else 0
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    
    profit_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else 0
    
    # 综合评分
    score = calculate_strategy_score(win_rate, profit_ratio)
    
    return {
        'total_trades': len(sells),
        'win_count': len(wins),
        'loss_count': len(losses),
        'win_rate': round(win_rate, 1),
        'profit_ratio': round(profit_ratio, 2),
        'total_profit': round(total_profit, 2),
        'avg_profit': round(avg_profit, 2),
        'avg_win': round(avg_win, 2),
        'avg_loss': round(avg_loss, 2),
        'score': score
    }

def calculate_strategy_score(win_rate: float, profit_ratio: float) -> float:
    """计算策略评分（0-100）"""
    # 胜率得分（权重40%）
    win_score = min(win_rate / 60 * 100, 100) * 0.4
    
    # 盈亏比得分（权重60%）
    ratio_score = min(profit_ratio / 2 * 100, 100) * 0.6
    
    return round(win_score + ratio_score, 1)

def get_optimization_suggestions(analysis: Dict) -> List[str]:
    """获取优化建议"""
    suggestions = []
    
    if analysis.get('win_rate', 0) < 50:
        suggestions.append("胜率偏低，建议优化选股条件")
    
    if analysis.get('profit_ratio', 0) < 1.5:
        suggestions.append("盈亏比偏低，建议调整止损止盈")
    
    if analysis.get('avg_loss', 0) < -500:
        suggestions.append("平均亏损较大，建议收紧止损")
    
    if analysis.get('score', 0) < 60:
        suggestions.append("综合评分偏低，建议重新审视策略")
    
    if not suggestions:
        suggestions.append("策略表现良好，建议保持当前配置")
    
    return suggestions


if __name__ == "__main__":
    # 测试
    print("=== 性能分析测试 ===")
    
    test_trades = [
        {'type': 'buy', 'symbol': '513100', 'price': 2.20, 'shares': 1000},
        {'type': 'sell', 'symbol': '513100', 'price': 2.40, 'shares': 1000, 'profit': 195},
        {'type': 'buy', 'symbol': '159941', 'price': 1.65, 'shares': 2000},
        {'type': 'sell', 'symbol': '159941', 'price': 1.50, 'shares': 2000, 'profit': -306},
    ]
    
    analysis = analyze_performance(test_trades, 100000)
    print(f"胜率: {analysis['win_rate']}%")
    print(f"盈亏比: {analysis['profit_ratio']}")
    print(f"评分: {analysis['score']}")
    
    suggestions = get_optimization_suggestions(analysis)
    print(f"建议: {suggestions[0]}")
    
    print("\n✅ 性能分析模块测试通过")
