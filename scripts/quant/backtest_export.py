#!/usr/bin/env python3
"""
回测数据导出
支持CSV和JSON格式
"""

import json
import csv
from datetime import datetime
from typing import Dict, List

def export_trades_csv(trades: List[Dict], output_path: str):
    """导出交易记录为CSV"""
    if not trades:
        return
    
    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=trades[0].keys())
        writer.writeheader()
        writer.writerows(trades)
    
    print(f"✅ CSV导出: {output_path}")


def export_report_json(report: Dict, output_path: str):
    """导出报告为JSON"""
    report['export_time'] = datetime.now().isoformat()
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    
    print(f"✅ JSON导出: {output_path}")


def generate_full_report(
    trades: List[Dict],
    performance: Dict,
    config: Dict,
    output_dir: str = "."
):
    """生成完整报告"""
    from pathlib import Path
    
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # CSV
    if trades:
        csv_path = f"{output_dir}/trades_{timestamp}.csv"
        export_trades_csv(trades, csv_path)
    
    # JSON
    report = {
        'performance': performance,
        'config': config,
        'trade_count': len(trades)
    }
    json_path = f"{output_dir}/report_{timestamp}.json"
    export_report_json(report, json_path)
    
    print(f"\n报告生成完成，时间戳: {timestamp}")


# 测试
if __name__ == "__main__":
    test_trades = [
        {'type': 'buy', 'symbol': '513100', 'price': 2.20, 'shares': 1000, 'commission': 5.0},
        {'type': 'sell', 'symbol': '513100', 'price': 2.40, 'shares': 1000, 'commission': 5.0, 'profit': 195.40},
    ]
    
    test_perf = {'win_rate': 50.0, 'profit_ratio': 1.5}
    test_config = {'stop_loss': 0.08, 'take_profit': 0.20}
    
    generate_full_report(test_trades, test_perf, test_config, "/tmp/backtest_reports")
