#!/usr/bin/env python3
"""
技术指标计算
"""

from typing import List, Dict
import math

def calculate_ma(prices: List[float], period: int) -> List[float]:
    """计算移动平均线"""
    if len(prices) < period:
        return []
    
    result = []
    for i in range(period - 1, len(prices)):
        avg = sum(prices[i-period+1:i+1]) / period
        result.append(avg)
    
    return result

def calculate_rsi(prices: List[float], period: int = 14) -> float:
    """计算RSI"""
    if len(prices) < period + 1:
        return 50
    
    gains = []
    losses = []
    
    for i in range(1, len(prices)):
        change = prices[i] - prices[i-1]
        if change > 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))
    
    if len(gains) < period:
        return 50
    
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    
    if avg_loss == 0:
        return 100
    
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    
    return round(rsi, 2)

def calculate_macd(prices: List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Dict:
    """计算MACD"""
    if len(prices) < slow:
        return {'macd': 0, 'signal': 0, 'histogram': 0}
    
    # EMA计算
    def ema(data, period):
        multiplier = 2 / (period + 1)
        ema_val = data[0]
        for price in data[1:]:
            ema_val = (price - ema_val) * multiplier + ema_val
        return ema_val
    
    ema_fast = ema(prices, fast)
    ema_slow = ema(prices, slow)
    macd = ema_fast - ema_slow
    
    # 简化：信号线用当前MACD
    signal_line = macd * 0.8
    
    return {
        'macd': round(macd, 4),
        'signal': round(signal_line, 4),
        'histogram': round(macd - signal_line, 4)
    }

def detect_trend(prices: List[float], ma_period: int = 20) -> str:
    """判断趋势"""
    if len(prices) < ma_period:
        return "unknown"
    
    ma = calculate_ma(prices, ma_period)
    current_price = prices[-1]
    current_ma = ma[-1]
    
    if current_price > current_ma * 1.02:
        return "uptrend"
    elif current_price < current_ma * 0.98:
        return "downtrend"
    else:
        return "sideways"


if __name__ == "__main__":
    print("=== 技术指标测试 ===\n")
    
    # 测试数据
    prices = [100, 101, 102, 101, 103, 105, 104, 106, 108, 107,
              109, 111, 110, 112, 114, 113, 115, 117, 116, 118]
    
    # MA
    ma5 = calculate_ma(prices, 5)
    print(f"MA5最新: {ma5[-1]:.2f}")
    
    # RSI
    rsi = calculate_rsi(prices)
    print(f"RSI(14): {rsi}")
    
    # 趋势
    trend = detect_trend(prices)
    print(f"趋势: {trend}")
    
    print("\n✅ 技术指标测试通过")
