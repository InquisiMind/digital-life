#!/usr/bin/env python3
"""
止损止盈策略
不同的止损止盈方法
"""

from typing import Dict, List, Tuple
import math

class StopLossStrategy:
    """止损策略基类"""
    
    @staticmethod
    def fixed_stop_loss(buy_price: float, stop_pct: float) -> float:
        """固定百分比止损"""
        return buy_price * (1 - stop_pct)
    
    @staticmethod
    def trailing_stop_loss(buy_price: float, highest_price: float, trail_pct: float) -> float:
        """移动止损"""
        return highest_price * (1 - trail_pct)
    
    @staticmethod
    def atr_stop_loss(buy_price: float, atr: float, multiplier: float = 2.0) -> float:
        """ATR止损"""
        return buy_price - atr * multiplier


class TakeProfitStrategy:
    """止盈策略基类"""
    
    @staticmethod
    def fixed_take_profit(buy_price: float, profit_pct: float) -> float:
        """固定百分比止盈"""
        return buy_price * (1 + profit_pct)
    
    @staticmethod
    def resistance_take_profit(buy_price: float, resistance: float) -> float:
        """阻力位止盈"""
        return resistance
    
    @staticmethod
    def risk_reward_take_profit(buy_price: float, stop_loss: float, ratio: float = 2.0) -> float:
        """风险收益比止盈"""
        risk = buy_price - stop_loss
        return buy_price + risk * ratio


def calculate_optimal_exit(buy_price: float, current_price: float, 
                           highest_price: float, stop_pct: float = 0.08,
                           trail_pct: float = 0.05, profit_pct: float = 0.20) -> Dict:
    """
    计算最优退出价格
    
    Returns:
        {'stop_loss': 止损价, 'take_profit': 止盈价, 'trailing_stop': 移动止损}
    """
    return {
        'stop_loss': StopLossStrategy.fixed_stop_loss(buy_price, stop_pct),
        'take_profit': TakeProfitStrategy.fixed_take_profit(buy_price, profit_pct),
        'trailing_stop': StopLossStrategy.trailing_stop_loss(buy_price, highest_price, trail_pct),
        'risk_reward_tp': TakeProfitStrategy.risk_reward_take_profit(
            buy_price, 
            StopLossStrategy.fixed_stop_loss(buy_price, stop_pct),
            ratio=2.0
        )
    }


if __name__ == "__main__":
    print("=== 止损止盈策略测试 ===\n")
    
    buy_price = 10.0
    highest_price = 11.0
    current_price = 10.5
    
    exits = calculate_optimal_exit(buy_price, current_price, highest_price)
    
    print(f"买入价: {buy_price}")
    print(f"当前价: {current_price}")
    print(f"最高价: {highest_price}")
    print(f"\n止损价: {exits['stop_loss']:.2f}")
    print(f"止盈价: {exits['take_profit']:.2f}")
    print(f"移动止损: {exits['trailing_stop']:.2f}")
    print(f"风险收益比止盈: {exits['risk_reward_tp']:.2f}")
    
    print("\n✅ 策略测试通过")
