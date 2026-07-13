#!/usr/bin/env python3
"""
回测报告生成器
输出结构化的回测结果报告
"""

import json
from datetime import datetime
from typing import Dict, List

def generate_report(
    trades: List[Dict],
    performance: Dict,
    config: Dict,
    output_path: str = None
) -> str:
    """
    生成回测报告
    
    Args:
        trades: 交易记录
        performance: 绩效指标
        config: 配置参数
        output_path: 输出路径（可选）
    
    Returns:
        报告文本
    """
    lines = []
    lines.append("=" * 60)
    lines.append("回测报告")
    lines.append("=" * 60)
    lines.append(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    
    # 配置部分
    lines.append("【回测配置】")
    lines.append(f"  初始资金: ¥{config.get('initial_capital', 100000):,.2f}")
    lines.append(f"  止损: {config.get('stop_loss', 0.08)*100:.1f}%")
    lines.append(f"  止盈: {config.get('take_profit', 0.20)*100:.1f}%")
    lines.append(f"  最大仓位: {config.get('max_position', 0.50)*100:.1f}%")
    lines.append(f"  滑点: {config.get('slippage', 0.001)*100:.2f}%")
    lines.append("")
    
    # 绩效部分
    lines.append("【绩效指标】")
    lines.append(f"  总交易: {performance.get('total_trades', 0)}笔")
    lines.append(f"  胜率: {performance.get('win_rate', 0):.1f}%")
    lines.append(f"  盈亏比: {performance.get('profit_ratio', 0):.2f}")
    lines.append(f"  平均盈利: ¥{performance.get('avg_win', 0):.2f}")
    lines.append(f"  平均亏损: ¥{performance.get('avg_loss', 0):.2f}")
    lines.append(f"  最大回撤: {performance.get('max_drawdown', 0):.1f}%")
    lines.append("")
    
    # 交易明细
    lines.append("【交易明细】")
    buy_count = sum(1 for t in trades if t['type'] == 'buy')
    sell_count = sum(1 for t in trades if t['type'] == 'sell')
    lines.append(f"  买入: {buy_count}笔")
    lines.append(f"  卖出: {sell_count}笔")
    lines.append("")
    
    # 盈亏股票
    wins = [t for t in trades if t['type'] == 'sell' and t.get('profit', 0) > 0]
    losses = [t for t in trades if t['type'] == 'sell' and t.get('profit', 0) <= 0]
    
    if wins:
        lines.append("【盈利股票】")
        for t in wins:
            lines.append(f"  {t['symbol']}: +¥{t['profit']:.2f}")
        lines.append("")
    
    if losses:
        lines.append("【亏损股票】")
        for t in losses:
            lines.append(f"  {t['symbol']}: ¥{t['profit']:.2f}")
        lines.append("")
    
    report = "\n".join(lines)
    
    # 保存到文件
    if output_path:
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(report)
        print(f"报告已保存: {output_path}")
    
    return report


if __name__ == "__main__":
    # 测试
    trades = [
        {'type': 'buy', 'symbol': '513100', 'price': 2.20, 'shares': 1000},
        {'type': 'sell', 'symbol': '513100', 'price': 2.40, 'shares': 1000, 'profit': 195.40},
    ]
    
    performance = {
        'total_trades': 2,
        'win_rate': 50.0,
        'profit_ratio': 1.5,
        'avg_win': 195.40,
        'avg_loss': -50.0,
        'max_drawdown': 5.2
    }
    
    config = {
        'initial_capital': 100000,
        'stop_loss': 0.08,
        'take_profit': 0.20,
        'max_position': 0.50,
        'slippage': 0.001
    }
    
    report = generate_report(trades, performance, config)
    print(report)
