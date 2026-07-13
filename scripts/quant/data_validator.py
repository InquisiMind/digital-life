#!/usr/bin/env python3
"""
数据验证模块
检查数据完整性和有效性
"""

from typing import Dict, List, Tuple

def validate_stock_data(data: Dict) -> Tuple[bool, List[str]]:
    """验证股票数据"""
    errors = []
    
    # 必要字段
    required = ['symbol', 'price']
    for field in required:
        if field not in data or data[field] is None:
            errors.append(f"缺少必要字段: {field}")
    
    # 价格有效性
    if 'price' in data:
        try:
            price = float(data['price'])
            if price <= 0:
                errors.append("价格必须大于0")
        except:
            errors.append("价格格式无效")
    
    # 代码格式
    if 'symbol' in data:
        symbol = str(data['symbol'])
        if len(symbol) != 6 or not symbol.isdigit():
            errors.append("股票代码应为6位数字")
    
    return len(errors) == 0, errors

def validate_trading_params(params: Dict) -> Tuple[bool, List[str]]:
    """验证交易参数"""
    errors = []
    
    # 止损范围
    if 'stop_loss' in params:
        sl = params['stop_loss']
        if not (0.01 <= sl <= 0.20):
            errors.append("止损应在1%-20%之间")
    
    # 止盈范围
    if 'take_profit' in params:
        tp = params['take_profit']
        if not (0.05 <= tp <= 0.50):
            errors.append("止盈应在5%-50%之间")
    
    # 仓位范围
    if 'max_position' in params:
        pos = params['max_position']
        if not (0.1 <= pos <= 1.0):
            errors.append("仓位应在10%-100%之间")
    
    return len(errors) == 0, errors

def validate_backtest_config(config) -> Tuple[bool, List[str]]:
    """验证回测配置"""
    errors = []
    
    if config.STOP_LOSS >= config.TAKE_PROFIT:
        errors.append("止损不应大于止盈")
    
    if config.STOP_LOSS <= 0:
        errors.append("止损必须大于0")
    
    if config.INITIAL_CAPITAL <= 0:
        errors.append("初始资金必须大于0")
    
    return len(errors) == 0, errors


if __name__ == "__main__":
    # 测试
    print("=== 数据验证测试 ===")
    
    # 股票数据
    ok, errs = validate_stock_data({'symbol': '002594', 'price': 90.0})
    print(f"股票数据验证: {'通过' if ok else errs}")
    
    # 交易参数
    ok, errs = validate_trading_params({'stop_loss': 0.08, 'take_profit': 0.20})
    print(f"交易参数验证: {'通过' if ok else errs}")
    
    print("\n✅ 数据验证模块测试通过")
