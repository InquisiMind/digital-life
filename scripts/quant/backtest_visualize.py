#!/usr/bin/env python3
"""
回测结果可视化
输出ASCII图表和表格
"""

def draw_bar_chart(data, title="图表", width=40):
    """绘制ASCII柱状图"""
    if not data:
        return
    
    print(f"\n{title}")
    print("-" * (width + 20))
    
    max_val = max(abs(v) for v in data.values())
    if max_val == 0:
        max_val = 1
    
    for label, value in data.items():
        bar_len = int(abs(value) / max_val * width)
        if value >= 0:
            bar = "█" * bar_len
            print(f"{label:15s} | {bar} {value:+.1f}")
        else:
            bar = "░" * bar_len
            print(f"{label:15s} | {bar} {value:.1f}")


def print_trading_summary(trades):
    """打印交易摘要"""
    from collections import defaultdict
    
    print("\n" + "=" * 50)
    print("交易摘要")
    print("=" * 50)
    
    if not trades:
        print("无交易记录")
        return
    
    # 统计
    buys = [t for t in trades if t['type'] == 'buy']
    sells = [t for t in trades if t['type'] == 'sell']
    
    print(f"总交易: {len(trades)}笔")
    print(f"买入: {len(buys)}笔")
    print(f"卖出: {len(sells)}笔")
    
    # 盈亏分布
    if sells:
        profits = [t.get('profit', 0) for t in sells]
        wins = [p for p in profits if p > 0]
        losses = [p for p in profits if p <= 0]
        
        print(f"\n盈利交易: {len(wins)}笔")
        print(f"亏损交易: {len(losses)}笔")
        
        if wins:
            print(f"平均盈利: ¥{sum(wins)/len(wins):.2f}")
        if losses:
            print(f"平均亏损: ¥{sum(losses)/len(losses):.2f}")


# 测试
if __name__ == "__main__":
    # 示例数据
    test_data = {
        "信号生成": 36,
        "执行交易": 2,
        "胜率": 50,
        "收益率": -0.03
    }
    
    draw_bar_chart(test_data, "回测统计")
    print_trading_summary([
        {'type': 'buy', 'symbol': '513100', 'price': 2.20, 'shares': 1000},
        {'type': 'sell', 'symbol': '513100', 'price': 2.40, 'shares': 1000, 'profit': 195.40},
        {'type': 'buy', 'symbol': '159941', 'price': 1.65, 'shares': 2000},
        {'type': 'sell', 'symbol': '159941', 'price': 1.50, 'shares': 2000, 'profit': -306.30},
    ])
