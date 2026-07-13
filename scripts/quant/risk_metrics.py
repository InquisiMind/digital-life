#!/usr/bin/env python3
"""
风险指标计算
夏普比率、最大回撤、波动率等
"""

import math
from typing import List, Dict

def calculate_sharpe_ratio(returns: List[float], risk_free_rate: float = 0.02) -> float:
    """
    计算夏普比率
    
    Args:
        returns: 收益率列表
        risk_free_rate: 无风险利率（年化）
    
    Returns:
        夏普比率
    """
    if not returns:
        return 0
    
    n = len(returns)
    avg_return = sum(returns) / n
    
    # 计算标准差
    variance = sum((r - avg_return) ** 2 for r in returns) / n
    std_dev = math.sqrt(variance)
    
    if std_dev == 0:
        return 0
    
    # 年化（假设每个return是日收益）
    annual_return = avg_return * 252
    annual_std = std_dev * math.sqrt(252)
    
    sharpe = (annual_return - risk_free_rate) / annual_std
    return round(sharpe, 2)


def calculate_max_drawdown(values: List[float]) -> Dict:
    """
    计算最大回撤
    
    Returns:
        {'max_drawdown': 百分比, 'start_idx': 起始位置, 'end_idx': 结束位置}
    """
    if not values or len(values) < 2:
        return {'max_drawdown': 0, 'start_idx': 0, 'end_idx': 0}
    
    peak = values[0]
    peak_idx = 0
    max_dd = 0
    start_idx = 0
    end_idx = 0
    
    for i, v in enumerate(values):
        if v > peak:
            peak = v
            peak_idx = i
        
        dd = (peak - v) / peak
        if dd > max_dd:
            max_dd = dd
            start_idx = peak_idx
            end_idx = i
    
    return {
        'max_drawdown': round(max_dd * 100, 2),
        'start_idx': start_idx,
        'end_idx': end_idx
    }


def calculate_volatility(returns: List[float]) -> float:
    """计算年化波动率"""
    if not returns:
        return 0
    
    n = len(returns)
    avg_return = sum(returns) / n
    variance = sum((r - avg_return) ** 2 for r in returns) / n
    daily_std = math.sqrt(variance)
    
    # 年化
    annual_std = daily_std * math.sqrt(252)
    return round(annual_std * 100, 2)


def calculate_all_metrics(portfolio_values: List[float], returns: List[float]) -> Dict:
    """计算所有风险指标"""
    max_dd = calculate_max_drawdown(portfolio_values)
    sharpe = calculate_sharpe_ratio(returns)
    volatility = calculate_volatility(returns)
    
    # 总收益
    total_return = (portfolio_values[-1] - portfolio_values[0]) / portfolio_values[0] * 100 if portfolio_values else 0
    
    return {
        'total_return': round(total_return, 2),
        'max_drawdown': max_dd['max_drawdown'],
        'sharpe_ratio': sharpe,
        'volatility': volatility
    }


# 测试
if __name__ == "__main__":
    # 模拟数据
    values = [100000, 102000, 101000, 103000, 99000, 105000, 104000, 106000]
    returns = [0.02, -0.01, 0.02, -0.04, 0.06, -0.01, 0.02]
    
    metrics = calculate_all_metrics(values, returns)
    
    print("=== 风险指标 ===")
    print(f"总收益: {metrics['total_return']}%")
    print(f"最大回撤: {metrics['max_drawdown']}%")
    print(f"夏普比率: {metrics['sharpe_ratio']}")
    print(f"年化波动率: {metrics['volatility']}%")
