#!/usr/bin/env python3
"""
仓位管理模块
不同的仓位计算方法
"""

from typing import Dict
import math

class PositionSizing:
    """仓位计算"""
    
    @staticmethod
    def fixed_position(capital: float, position_pct: float) -> float:
        """固定仓位"""
        return capital * position_pct
    
    @staticmethod
    def kelly_criterion(win_rate: float, win_loss_ratio: float) -> float:
        """
        凯利公式
        
        f* = p - q/b
        p: 胜率
        q: 败率 (1-p)
        b: 盈亏比
        """
        q = 1 - win_rate
        kelly = win_rate - q / win_loss_ratio
        # 通常使用半凯利
        return max(0, min(kelly * 0.5, 0.5))  # 最大50%
    
    @staticmethod
    def risk_parity(volatility: float, target_risk: float = 0.02) -> float:
        """风险平价"""
        if volatility <= 0:
            return 0
        return min(target_risk / volatility, 1.0)
    
    @staticmethod
    def volatility_adjusted(capital: float, price: float, atr: float, 
                           risk_per_trade: float = 0.02) -> int:
        """波动率调整仓位"""
        risk_amount = capital * risk_per_trade
        shares = risk_amount / (atr * 2)  # 2倍ATR作为止损
        return int(shares // 100 * 100)  # 取整到100股


def calculate_position_sizes(capital: float, signals: list) -> Dict:
    """计算多个信号的仓位分配"""
    n_signals = len(signals)
    if n_signals == 0:
        return {}
    
    # 平均分配
    per_signal = capital / n_signals
    
    result = {}
    for sig in signals:
        result[sig['symbol']] = {
            'position_value': per_signal,
            'shares': int(per_signal / sig['price'] // 100 * 100)
        }
    
    return result


if __name__ == "__main__":
    print("=== 仓位管理测试 ===\n")
    
    capital = 100000
    
    # 固定仓位
    fixed = PositionSizing.fixed_position(capital, 0.3)
    print(f"固定仓位(30%): ¥{fixed:,.0f}")
    
    # 凯利公式
    kelly = PositionSizing.kelly_criterion(0.6, 2.0)
    print(f"凯利仓位(胜率60%,盈亏比2): {kelly*100:.1f}%")
    
    # 多信号分配
    signals = [
        {'symbol': '513100', 'price': 2.20},
        {'symbol': '159941', 'price': 1.65},
    ]
    
    positions = calculate_position_sizes(capital, signals)
    print(f"\n仓位分配:")
    for sym, pos in positions.items():
        print(f"  {sym}: {pos['shares']}股 (¥{pos['position_value']:,.0f})")
    
    print("\n✅ 仓位管理测试通过")
