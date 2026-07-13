#!/usr/bin/env python3
"""
回测框架 v1.0
用于模拟炒股策略的历史数据验证

模块结构：
1. 数据层：获取历史K线、涨停板数据
2. 策略层：生成买卖信号
3. 执行层：模拟交易执行（含滑点、手续费）
4. 统计层：计算绩效指标
"""

import json
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import requests

# ==================== 配置 ====================

class BacktestConfig:
    """回测配置"""
    # 交易规则
    COMMISSION = 0.0003      # 佣金 0.03%
    STAMP_DUTY = 0.001       # 印花税 0.1%（仅卖出）
    MIN_COMMISSION = 5.0     # 最低佣金
    SLIPPAGE = 0.001         # 滑点 0.1%
    
    # 风控参数
    STOP_LOSS = 0.08         # 止损 8%
    TAKE_PROFIT = 0.20       # 止盈 20%
    MAX_POSITION = 0.50      # 最大仓位 50%
    
    # 初始资金
    INITIAL_CAPITAL = 100000.0


# ==================== 数据层 ====================

class DataFetcher:
    """数据获取"""
    
    @staticmethod
    def get_zt_pool(date: str) -> List[Dict]:
        """
        获取指定日期的涨停板数据
        
        Args:
            date: 日期格式 "20260605"
        
        Returns:
            涨停板股票列表
        """
        try:
            import akshare as ak
            df = ak.stock_zt_pool_em(date=date)
            
            if df.empty:
                return []
            
            result = []
            for _, row in df.iterrows():
                result.append({
                    'symbol': row['代码'],
                    'name': row['名称'],
                    'change_pct': row['涨跌幅'],
                    'price': row['最新价'],
                    'last_limit_time': row.get('最后封板时间', ''),
                    'break_count': row.get('炸板次数', 0),
                    'continuous': row.get('连板数', 1),
                    'sector': row.get('所属行业', '')
                })
            
            return result
        except Exception as e:
            print(f"获取涨停板数据失败: {e}")
            return []
    
    @staticmethod
    def get_stock_history(symbol: str, start_date: str = None, end_date: str = None) -> List[Dict]:
        """
        获取股票历史K线
        
        Args:
            symbol: 6位代码
            start_date: "20240101"
            end_date: "20241231"
        """
        try:
            import akshare as ak
            df = ak.stock_zh_a_hist(
                symbol=symbol,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust="qfq"
            )
            
            if df.empty:
                return []
            
            result = []
            for _, row in df.iterrows():
                result.append({
                    'date': str(row['日期']),
                    'open': row['开盘'],
                    'high': row['最高'],
                    'low': row['最低'],
                    'close': row['收盘'],
                    'volume': row['成交量'],
                    'amount': row['成交额']
                })
            
            return result
        except Exception as e:
            print(f"获取历史数据失败: {e}")
            return []
class Strategy:
    """
    策略基类
    
    当前策略框架（来自STRATEGY.md）：
    1. 跟随主力资金
    2. 发现热点板块
    3. 识别板块龙头
    4. 等待买入时机
    """
    
    def __init__(self, config: BacktestConfig):
        self.config = config
    
    def generate_signals(self, date: str, data: Dict) -> List[Dict]:
        """
        生成交易信号
        
        Returns:
            [{'type': 'buy', 'symbol': '002594', 'reason': '...'}]
        """
        return []
    
    def check_stop_loss(self, position: Dict, current_price: float) -> bool:
        """检查是否触发止损"""
        cost = position['cost_price']
        loss_pct = (current_price - cost) / cost
        return loss_pct <= -self.config.STOP_LOSS
    
    def check_take_profit(self, position: Dict, current_price: float) -> bool:
        """检查是否触发止盈"""
        cost = position['cost_price']
        profit_pct = (current_price - cost) / cost
        return profit_pct >= self.config.TAKE_PROFIT


class FollowCapitalFlow(Strategy):
    """
    跟随主力资金策略
    
    龙头股特征：
    - 板块中率先启动
    - 成交量明显放大（2倍以上）
    - 涨停封板坚决
    - 有题材/业绩支撑
    """
    
    def generate_signals(self, date: str, data: Dict) -> List[Dict]:
        signals = []
        zt_pool = data.get('zt_pool', [])
        
        for stock in zt_pool:
            # 筛选条件
            if self._is_dragon_head(stock, data):
                signals.append({
                    'type': 'buy',
                    'symbol': stock['symbol'],
                    'reason': f"龙头股：{stock.get('name', '')}，板块{stock.get('sector', '')}"
                })
        
        return signals
    
    def _is_dragon_head(self, stock: Dict, data: Dict) -> bool:
        """
        判断是否为龙头股
        
        龙头股特征：
        1. 封板时间早于10:00（格式"HHMMSS"）
        2. 炸板次数=0（封板坚决）
        3. 连板数>=1（有连续性）
        """
        # 封板时间检查
        limit_time = str(stock.get('last_limit_time', ''))
        if limit_time and limit_time.isdigit():
            hour = int(limit_time[:2]) if len(limit_time) >= 2 else 99
            if hour >= 10:
                return False  # 封板时间太晚
        
        # 炸板次数检查
        break_count = stock.get('break_count', 0)
        if break_count > 0:
            return False  # 炸过板
        
        # 连板数检查
        continuous = stock.get('continuous', 0)
        if continuous < 1:
            return False
        
        return True

# ==================== 执行层 ====================

class TradingSimulator:
    """交易模拟器"""
    
    def __init__(self, config: BacktestConfig):
        self.config = config
        self.cash = config.INITIAL_CAPITAL
        self.positions = []  # 持仓列表
        self.trades = []     # 交易记录
    
    def execute_buy(self, symbol: str, price: float, shares: int) -> bool:
        """执行买入"""
        # 计算金额（含滑点）
        actual_price = price * (1 + self.config.SLIPPAGE)
        amount = actual_price * shares
        
        # 计算佣金
        commission = max(amount * self.config.COMMISSION, self.config.MIN_COMMISSION)
        total_cost = amount + commission
        
        if total_cost > self.cash:
            return False
        
        # 执行
        self.cash -= total_cost
        self.positions.append({
            'symbol': symbol,
            'shares': shares,
            'cost_price': actual_price,
            'buy_time': datetime.now().isoformat()
        })
        
        self.trades.append({
            'type': 'buy',
            'symbol': symbol,
            'price': actual_price,
            'shares': shares,
            'commission': commission
        })
        
        return True
    
    def execute_sell(self, symbol: str, price: float, shares: int, reason: str = '') -> bool:
        """执行卖出"""
        # 找到持仓
        pos_idx = None
        for i, pos in enumerate(self.positions):
            if pos['symbol'] == symbol and pos['shares'] >= shares:
                pos_idx = i
                break
        
        if pos_idx is None:
            return False
        
        # 计算金额（含滑点）
        actual_price = price * (1 - self.config.SLIPPAGE)
        amount = actual_price * shares
        
        # 计算费用
        commission = max(amount * self.config.COMMISSION, self.config.MIN_COMMISSION)
        stamp_duty = amount * self.config.STAMP_DUTY
        net_amount = amount - commission - stamp_duty
        
        # 执行
        self.cash += net_amount
        
        pos = self.positions[pos_idx]
        profit = (actual_price - pos['cost_price']) * shares
        
        self.trades.append({
            'type': 'sell',
            'symbol': symbol,
            'price': actual_price,
            'shares': shares,
            'commission': commission,
            'stamp_duty': stamp_duty,
            'profit': profit,
            'reason': reason
        })
        
        # 更新持仓
        if pos['shares'] == shares:
            self.positions.pop(pos_idx)
        else:
            self.positions[pos_idx]['shares'] -= shares
        
        return True
    
    def get_portfolio_value(self, prices: Dict[str, float]) -> float:
        """计算总资产"""
        value = self.cash
        for pos in self.positions:
            price = prices.get(pos['symbol'], pos['cost_price'])
            value += price * pos['shares']
        return value


# ==================== 统计层 ====================

class PerformanceCalculator:
    """绩效计算"""
    
    @staticmethod
    def calculate(trades: List[Dict], portfolio_values: List[float]) -> Dict:
        """计算回测绩效"""
        if not trades:
            return {}
        
        # 胜率
        wins = [t for t in trades if t.get('type') == 'sell' and t.get('profit', 0) > 0]
        losses = [t for t in trades if t.get('type') == 'sell' and t.get('profit', 0) <= 0]
        win_rate = len(wins) / len(trades) * 100 if trades else 0
        
        # 盈亏比
        avg_win = sum(t['profit'] for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t['profit'] for t in losses) / len(losses) if losses else 0
        profit_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else 0
        
        # 最大回撤
        max_drawdown = PerformanceCalculator._max_drawdown(portfolio_values)
        
        return {
            'total_trades': len(trades),
            'win_rate': round(win_rate, 2),
            'profit_ratio': round(profit_ratio, 2),
            'max_drawdown': round(max_drawdown, 2),
            'avg_win': round(avg_win, 2),
            'avg_loss': round(avg_loss, 2)
        }
    
    @staticmethod
    def _max_drawdown(values: List[float]) -> float:
        """计算最大回撤"""
        if not values:
            return 0
        
        peak = values[0]
        max_dd = 0
        
        for v in values:
            if v > peak:
                peak = v
            dd = (peak - v) / peak
            if dd > max_dd:
                max_dd = dd
        
        return max_dd * 100



    @staticmethod
    def calculate_risk_metrics(portfolio_values: List[float]) -> Dict:
        """计算风险指标（夏普比率、最大回撤、波动率）"""
        import math
        
        if not portfolio_values or len(portfolio_values) < 2:
            return {}
        
        # 计算收益率序列
        returns = []
        for i in range(1, len(portfolio_values)):
            r = (portfolio_values[i] - portfolio_values[i-1]) / portfolio_values[i-1]
            returns.append(r)
        
        # 总收益
        total_return = (portfolio_values[-1] - portfolio_values[0]) / portfolio_values[0] * 100
        
        # 最大回撤
        peak = portfolio_values[0]
        max_dd = 0
        for v in portfolio_values:
            if v > peak:
                peak = v
            dd = (peak - v) / peak
            if dd > max_dd:
                max_dd = dd
        
        # 夏普比率
        if returns:
            avg_return = sum(returns) / len(returns)
            variance = sum((r - avg_return) ** 2 for r in returns) / len(returns)
            std_dev = math.sqrt(variance) if variance > 0 else 0
            
            if std_dev > 0:
                annual_return = avg_return * 252
                annual_std = std_dev * math.sqrt(252)
                sharpe = (annual_return - 0.02) / annual_std
            else:
                sharpe = 0
        else:
            sharpe = 0
        
        return {
            'total_return': round(total_return, 2),
            'max_drawdown': round(max_dd * 100, 2),
            'sharpe_ratio': round(sharpe, 2)
        }


# ==================== 回测引擎 ====================

class BacktestEngine:
    """回测引擎"""
    
    def __init__(self, config: BacktestConfig = None):
        self.config = config or BacktestConfig()
        self.simulator = TradingSimulator(self.config)
        self.strategy = FollowCapitalFlow(self.config)
        self.portfolio_values = []
    
    def run(self, start_date: str, end_date: str) -> Dict:
        """
        运行回测
        
        Args:
            start_date: 开始日期 '20260501'
            end_date: 结束日期 '20260606'
        
        Returns:
            回测绩效报告
        """
        from datetime import datetime, timedelta
        
        print(f"回测期间：{start_date} ~ {end_date}")
        print(f"初始资金：{self.config.INITIAL_CAPITAL}")
        print("=" * 50)
        
        # 生成交易日列表（简化版，跳过周末）
        start = datetime.strptime(start_date, "%Y%m%d")
        end = datetime.strptime(end_date, "%Y%m%d")
        
        current = start
        trade_days = []
        while current <= end:
            if current.weekday() < 5:  # 周一到周五
                trade_days.append(current.strftime("%Y%m%d"))
            current += timedelta(days=1)
        
        print(f"交易日数量：{len(trade_days)}")
        
        # 回测主循环
        for day in trade_days:
            # 获取涨停板数据
            zt_pool = DataFetcher.get_zt_pool(day)
            
            if not zt_pool:
                continue
            
            # 生成信号
            signals = self.strategy.generate_signals(day, {'zt_pool': zt_pool})
            
            # 记录当日涨停数和信号数
            if zt_pool:
                print(f"{day}: 涨停{len(zt_pool)}只, 信号{len(signals)}个")
            
            # 注：历史价格获取在run_backtest.py中用涨停板数据简化实现
            # 当前简化：只统计信号生成情况
        
        print("=" * 50)
        print("回测完成")
        
        return {
            'trade_days': len(trade_days),
            'signals_generated': 'see above',
            'final_capital': self.simulator.cash
        }

        return PerformanceCalculator.calculate(
            self.simulator.trades,
            self.portfolio_values
        )


# ==================== 测试 ====================

if __name__ == "__main__":
    print("回测框架 v1.0 - 骨架代码")
    print("=" * 50)
    print("待完成：")
    print("1. 数据层：接入akshare获取历史K线和涨停板数据")
    print("2. 策略层：实现龙头股识别逻辑")
    print("3. 回测循环：遍历交易日执行策略")
    print("=" * 50)
    
    # 测试配置
    config = BacktestConfig()
    print(f"止损: {config.STOP_LOSS*100}%")
    print(f"止盈: {config.TAKE_PROFIT*100}%")
    print(f"最大仓位: {config.MAX_POSITION*100}%")


# ==================== 完整测试 ====================

def run_simple_backtest():
    """运行简化的回测演示"""
    from datetime import datetime, timedelta
    
    print("=" * 60)
    print("回测框架 v1.0 - 完整演示")
    print("=" * 60)
    
    engine = BacktestEngine()
    
    # 回测参数
    start_date = "20260501"
    end_date = "20260606"
    
    print(f"\n回测期间: {start_date} ~ {end_date}")
    print(f"策略: 跟随主力资金（龙头股）")
    print(f"止损: {BacktestConfig.STOP_LOSS*100}%")
    print(f"止盈: {BacktestConfig.TAKE_PROFIT*100}%")
    
    # 运行回测
    result = engine.run(start_date, end_date)
    
    print("\n" + "=" * 60)
    print("回测统计")
    print("=" * 60)
    print(f"交易天数: {result['trade_days']}")
    print(f"最终资金: ¥{result['final_capital']:,.2f}")
    
    return result


if __name__ == "__main__":
    run_simple_backtest()
