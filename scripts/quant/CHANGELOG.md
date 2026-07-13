# 回测框架 v2.0 更新日志

## 版本历史

### v2.0.0 (2026-06-08)
完整重构，模块化设计

#### 核心模块
- backtest_engine.py - 核心引擎 (534行)
- run_backtest.py - 执行脚本 (157行)
- main.py - 主入口 (55行)

#### 数据模块
- data_preprocess.py - 数据预处理
- data_validator.py - 数据验证
- data_cache.py - 数据缓存
- config_loader.py - 配置加载

#### 策略模块
- signal_generator.py - 信号生成
- strategy_optimize.py - 参数优化
- strategy_compare.py - 策略对比

#### 交易模块
- position_manager.py - 持仓管理
- position_sizing.py - 仓位计算
- market_monitor.py - 市场监控

#### 风险模块
- risk_metrics.py - 风险指标
- stop_profit_strategy.py - 止损止盈

#### 分析模块
- performance_analyzer.py - 绩效分析
- technical_indicators.py - 技术指标
- analyze_result.py - 结果分析

#### 输出模块
- backtest_report.py - 报告生成
- backtest_visualize.py - 可视化
- backtest_export.py - 数据导出
- backtest_logger.py - 日志记录

#### 测试模块
- test_backtest.py - 单元测试
- examples.py - 使用示例

#### 文档
- README.md - 使用文档
- config.ini - 配置模板

## 统计
- 总文件: 31个
- 总代码: 3041行
- 总大小: 87KB

## 功能
- 数据获取、清洗、验证、缓存
- 策略信号生成（龙头股/突破/板块轮动）
- 交易模拟、持仓管理、仓位计算
- 绩效分析、风险指标、技术指标
- 参数优化、策略对比
- 可视化、报告、导出
