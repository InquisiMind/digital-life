# 回测框架 API 文档

## 核心模块

### backtest_engine

```python
from backtest_engine import BacktestConfig, TradingSimulator, DataFetcher

# 配置
config = BacktestConfig()
config.STOP_LOSS = 0.08  # 止损8%
config.TAKE_PROFIT = 0.20  # 止盈20%

# 交易模拟
sim = TradingSimulator(config)
sim.execute_buy('513100', 2.20, 1000)
sim.execute_sell('513100', 2.40, 1000, '止盈')

# 数据获取
zt_pool = DataFetcher.get_zt_pool('20260605')
```

### signal_generator

```python
from signal_generator import generate_dragon_head_signals

signals = generate_dragon_head_signals(zt_pool)
# 返回: [{'symbol': '...', 'name': '...', 'price': ..., 'reason': '...'}]
```

### performance_analyzer

```python
from performance_analyzer import analyze_performance, get_optimization_suggestions

analysis = analyze_performance(trades, initial_capital)
suggestions = get_optimization_suggestions(analysis)
```

### risk_metrics

```python
from risk_metrics import calculate_sharpe_ratio, calculate_max_drawdown

sharpe = calculate_sharpe_ratio(returns, risk_free_rate=0.02)
max_dd = calculate_max_drawdown(portfolio_values)
```

### technical_indicators

```python
from technical_indicators import calculate_ma, calculate_rsi, detect_trend

ma5 = calculate_ma(prices, 5)
rsi = calculate_rsi(prices, 14)
trend = detect_trend(prices, 20)
```

## 使用示例

```python
# 完整回测流程
from backtest_engine import BacktestConfig, TradingSimulator, DataFetcher
from signal_generator import generate_dragon_head_signals

# 1. 初始化
config = BacktestConfig()
sim = TradingSimulator(config)

# 2. 获取数据
zt_pool = DataFetcher.get_zt_pool('20260605')

# 3. 生成信号
signals = generate_dragon_head_signals(zt_pool)

# 4. 执行交易
for sig in signals[:3]:
    shares = int(100000 * 0.5 / sig['price'] / 100) * 100
    sim.execute_buy(sig['symbol'], sig['price'], shares)

# 5. 查看结果
print(f"持仓: {len(sim.positions)}只")
print(f"现金: {sim.cash}")
```
