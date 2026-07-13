"""
回测框架 v2.0
完整的量化策略回测系统

模块:
- backtest_engine: 核心引擎
- data_fetcher: 数据获取
- trading_simulator: 交易模拟
- performance_analyzer: 绩效分析
- signal_generator: 信号生成
- position_manager: 持仓管理
- market_monitor: 市场监控
- data_validator: 数据验证
- data_preprocess: 数据预处理
"""

from .backtest_engine import (
    BacktestConfig,
    TradingSimulator,
    PerformanceCalculator,
    DataFetcher,
    BacktestEngine,
    Strategy,
    FollowCapitalFlow
)

__version__ = "2.0.0"
__author__ = "Alpha"
