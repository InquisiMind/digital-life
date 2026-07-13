# 回测框架 v2.0

完整的量化策略回测框架，支持策略开发、测试和优化。

## 快速开始

```bash
# 运行回测
python3 run_backtest.py

# 运行测试
python3 test_backtest.py

# 参数优化
python3 strategy_optimize.py

# 策略对比
python3 strategy_compare.py
```

## 文件结构

| 文件 | 功能 | 行数 |
|-----|------|-----|
| backtest_engine.py | 核心引擎 | 534 |
| run_backtest.py | 执行脚本 | 157 |
| test_backtest.py | 单元测试 | 80 |
| data_preprocess.py | 数据预处理 | 60 |
| backtest_report.py | 报告生成 | 115 |
| backtest_visualize.py | 可视化 | 80 |
| backtest_export.py | 数据导出 | 75 |
| risk_metrics.py | 风险指标 | 120 |
| strategy_optimize.py | 参数优化 | 59 |
| strategy_compare.py | 策略对比 | 60 |
| data_cache.py | 数据缓存 | 94 |

**总计**: 12个Python文件，1434行代码

## 核心API

### 配置 (BacktestConfig)
```python
config = BacktestConfig()
config.STOP_LOSS = 0.08  # 止损8%
config.TAKE_PROFIT = 0.20  # 止盈20%
```

### 交易模拟器 (TradingSimulator)
```python
sim = TradingSimulator(config)
sim.execute_buy('513100', 2.20, 1000)  # 买入
sim.execute_sell('513100', 2.40, 1000, '止盈')  # 卖出
```

### 数据获取 (DataFetcher)
```python
zt_pool = DataFetcher.get_zt_pool('20260605')  # 涨停板数据
history = DataFetcher.get_stock_history('513100')  # 历史K线
```

### 绩效计算 (PerformanceCalculator)
```python
perf = PerformanceCalculator.calculate(trades, portfolio_values)
# 返回: win_rate, profit_ratio, max_drawdown
```

## 策略参数

| 参数 | 默认值 | 范围 |
|-----|--------|-----|
| 止损 | 8% | 5-10% |
| 止盈 | 20% | 15-30% |
| 最大仓位 | 50% | 30-50% |
| 滑点 | 0.1% | - |

## 最优配置

基于参数优化结果：
- **止损**: 5%
- **止盈**: 30%
- **最大仓位**: 50%
- **期望收益**: 16%（胜率60%时）

## 测试覆盖

- ✅ 配置测试
- ✅ 交易模拟器测试
- ✅ 绩效计算测试
- ✅ 数据预处理测试

## 待完善

1. 历史K线数据获取（接口偶发断连）
2. 多策略并行回测
3. 实时数据接入
