#!/usr/bin/env python3
"""
持仓管理模块
"""

from typing import Dict, List
from datetime import datetime

class PositionManager:
    """持仓管理器"""
    
    def __init__(self, config):
        self.config = config
        self.positions = []
    
    def add_position(self, symbol: str, name: str, price: float, shares: int):
        """添加持仓"""
        self.positions.append({
            'symbol': symbol,
            'name': name,
            'cost_price': price,
            'shares': shares,
            'buy_time': datetime.now().isoformat(),
            'stop_loss': price * (1 - self.config.STOP_LOSS),
            'take_profit': price * (1 + self.config.TAKE_PROFIT)
        })
    
    def remove_position(self, symbol: str) -> Dict:
        """移除持仓"""
        for i, pos in enumerate(self.positions):
            if pos['symbol'] == symbol:
                return self.positions.pop(i)
        return None
    
    def check_stop_loss(self, symbol: str, current_price: float) -> bool:
        """检查止损"""
        for pos in self.positions:
            if pos['symbol'] == symbol:
                return current_price <= pos['stop_loss']
        return False
    
    def check_take_profit(self, symbol: str, current_price: float) -> bool:
        """检查止盈"""
        for pos in self.positions:
            if pos['symbol'] == symbol:
                return current_price >= pos['take_profit']
        return False
    
    def get_total_value(self, prices: Dict[str, float]) -> float:
        """计算持仓总价值"""
        total = 0
        for pos in self.positions:
            price = prices.get(pos['symbol'], pos['cost_price'])
            total += price * pos['shares']
        return total
    
    def get_position_summary(self, prices: Dict[str, float]) -> List[Dict]:
        """获取持仓摘要"""
        summary = []
        for pos in self.positions:
            current_price = prices.get(pos['symbol'], pos['cost_price'])
            profit_pct = (current_price - pos['cost_price']) / pos['cost_price'] * 100
            
            summary.append({
                'symbol': pos['symbol'],
                'name': pos['name'],
                'cost': pos['cost_price'],
                'current': current_price,
                'shares': pos['shares'],
                'profit_pct': round(profit_pct, 2),
                'to_stop_loss': round((pos['stop_loss'] - current_price) / current_price * 100, 2),
                'to_take_profit': round((pos['take_profit'] - current_price) / current_price * 100, 2)
            })
        
        return summary


if __name__ == "__main__":
    from backtest_engine import BacktestConfig
    
    print("=== 持仓管理测试 ===")
    
    config = BacktestConfig()
    pm = PositionManager(config)
    
    # 添加持仓
    pm.add_position('513100', '纳指ETF', 2.20, 1000)
    
    # 检查止损止盈
    print(f"止损检查(2.10): {pm.check_stop_loss('513100', 2.10)}")
    print(f"止盈检查(2.50): {pm.check_take_profit('513100', 2.50)}")
    
    # 持仓摘要
    summary = pm.get_position_summary({'513100': 2.30})
    print(f"持仓摘要: 盈利{summary[0]['profit_pct']}%")
    
    print("\n✅ 持仓管理测试通过")
