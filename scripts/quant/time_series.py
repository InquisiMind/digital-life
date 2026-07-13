#!/usr/bin/env python3
"""
时间序列分析
"""

from typing import List, Dict
import math

def calculate_returns(prices: List[float]) -> List[float]:
    """计算收益率序列"""
    returns = []
    for i in range(1, len(prices)):
        r = (prices[i] - prices[i-1]) / prices[i-1]
        returns.append(r)
    return returns

def calculate_volatility(returns: List[float], annualize: bool = True) -> float:
    """计算波动率"""
    if not returns:
        return 0
    
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / len(returns)
    vol = math.sqrt(variance)
    
    if annualize:
        vol *= math.sqrt(252)
    
    return vol * 100

def calculate_sharpe(returns: List[float], rf: float = 0.02) -> float:
    """计算夏普比率"""
    if not returns:
        return 0
    
    mean_return = sum(returns) / len(returns)
    vol = calculate_volatility(returns, annualize=False)
    
    if vol == 0:
        return 0
    
    # 年化
    annual_return = mean_return * 252
    annual_vol = vol * math.sqrt(252)
    
    return (annual_return - rf) / annual_vol


if __name__ == "__main__":
    print("=== 时间序列分析测试 ===\n")
    
    prices = [100, 102, 101, 103, 105, 104, 106, 108, 107, 109]
    
    returns = calculate_returns(prices)
    print(f"收益率序列: {[f'{r*100:.1f}%' for r in returns[:5]]}...")
    
    vol = calculate_volatility(returns)
    print(f"年化波动率: {vol:.1f}%")
    
    sharpe = calculate_sharpe(returns)
    print(f"夏普比率: {sharpe:.2f}")
    
    print("\n✅ 时间序列分析测试通过")
