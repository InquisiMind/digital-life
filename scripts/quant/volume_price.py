"""
量价配合策略识别模块
核心逻辑：价涨量增验证趋势健康，价涨量缩警示背离
"""

def analyze_volume_price(prices, volumes, window=5):
    """
    分析量价配合关系
    
    参数:
        prices: 收盘价列表 [p1, p2, ..., pn]
        volumes: 成交量列表 [v1, v2, ..., vn]
        window: 分析窗口天数
    
    返回:
        dict: {
            'trend': '上涨'/'下跌'/'震荡',
            'volume_ratio': 量比（近5日均量/前5日均量）,
            'signal': '健康上涨'/'量价背离'/'缩量反弹'/'放量下跌'/'无信号',
            'confidence': 0-1,
            'description': 描述
        }
    """
    if len(prices) < window * 2:
        return {'trend': '数据不足', 'signal': '无信号', 'confidence': 0, 'description': '数据不足'}
    
    # 近window天数据
    recent_prices = prices[-window:]
    recent_volumes = volumes[-window:]
    
    # 前window天数据作为基准
    prev_prices = prices[-window*2:-window]
    prev_volumes = volumes[-window*2:-window]
    
    # 计算价格变化
    price_change = (recent_prices[-1] - recent_prices[0]) / recent_prices[0]
    
    # 计算量比
    recent_avg_vol = sum(recent_volumes) / window
    prev_avg_vol = sum(prev_volumes) / window
    volume_ratio = recent_avg_vol / prev_avg_vol if prev_avg_vol > 0 else 1
    
    # 判断趋势
    if price_change > 0.03:
        trend = '上涨'
    elif price_change < -0.03:
        trend = '下跌'
    else:
        trend = '震荡'
    
    # 量价配合信号判断
    signal = '无信号'
    confidence = 0
    description = ''
    
    if trend == '上涨':
        if volume_ratio >= 1.3:
            # 价涨量增，健康上涨
            signal = '健康上涨'
            confidence = min(0.9, 0.5 + (volume_ratio - 1) * 0.3)
            description = f'价涨{price_change*100:.1f}%量增{volume_ratio:.2f}倍，趋势健康'
        elif volume_ratio < 0.8:
            # 价涨量缩，量价背离
            signal = '量价背离'
            confidence = 0.6
            description = f'价涨{price_change*100:.1f}%但量缩{volume_ratio:.2f}倍，警惕背离'
        else:
            signal = '缩量反弹'
            confidence = 0.4
            description = f'价涨{price_change*100:.1f}%量比{volume_ratio:.2f}，需观察'
    
    elif trend == '下跌':
        if volume_ratio >= 1.3:
            # 价跌量增，恐慌抛售
            signal = '放量下跌'
            confidence = 0.7
            description = f'价跌{abs(price_change)*100:.1f}%量增{volume_ratio:.2f}倍，恐慌抛售'
        else:
            signal = '缩量下跌'
            confidence = 0.3
            description = f'价跌{abs(price_change)*100:.1f}%量缩，卖压减轻'
    
    else:  # 震荡
        signal = '横盘震荡'
        confidence = 0.2
        description = f'价格震荡{price_change*100:.1f}%，量比{volume_ratio:.2f}'
    
    return {
        'trend': trend,
        'volume_ratio': round(volume_ratio, 2),
        'signal': signal,
        'confidence': round(confidence, 2),
        'description': description
    }


def check_volume_breakout(prices, volumes, ma_volume):
    """
    检查放量突破信号
    
    参数:
        prices: 收盘价列表
        volumes: 成交量列表
        ma_volume: 均量线值
    
    返回:
        dict: {'is_breakout': bool, 'volume_ratio': float, 'description': str}
    """
    if len(prices) < 2 or len(volumes) < 2:
        return {'is_breakout': False, 'volume_ratio': 0, 'description': '数据不足'}
    
    # 今日收盘价突破前一日高点
    price_breakout = prices[-1] > prices[-2]
    
    # 今日成交量放大
    today_vol = volumes[-1]
    vol_ratio = today_vol / ma_volume if ma_volume > 0 else 1
    volume_breakout = vol_ratio >= 1.5
    
    is_breakout = price_breakout and volume_breakout
    
    description = ''
    if is_breakout:
        description = f'放量突破：量比{vol_ratio:.2f}'
    elif price_breakout and not volume_breakout:
        description = f'缩量突破，量比仅{vol_ratio:.2f}，需警惕'
    
    return {
        'is_breakout': is_breakout,
        'volume_ratio': round(vol_ratio, 2),
        'description': description
    }


# 测试
if __name__ == '__main__':
    # 模拟数据：健康上涨
    prices1 = [10, 10.2, 10.5, 10.8, 11.2, 11.5, 11.8, 12.0, 12.3, 12.5]
    volumes1 = [100, 110, 120, 130, 140, 150, 160, 170, 180, 190]
    
    result1 = analyze_volume_price(prices1, volumes1)
    print(f"测试1 - 健康上涨: {result1}")
    
    # 模拟数据：量价背离
    prices2 = [10, 10.2, 10.5, 10.8, 11.2, 11.4, 11.6, 11.8, 12.0, 12.1]
    volumes2 = [150, 140, 130, 120, 110, 100, 90, 80, 70, 60]
    
    result2 = analyze_volume_price(prices2, volumes2)
    print(f"测试2 - 量价背离: {result2}")
    
    # 测试放量突破
    ma_vol = sum(volumes1[:5]) / 5
    breakout = check_volume_breakout(prices1[-2:], volumes1[-2:], ma_vol)
    print(f"测试3 - 放量突破: {breakout}")
