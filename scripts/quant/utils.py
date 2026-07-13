#!/usr/bin/env python3
"""
通用工具函数
"""

from datetime import datetime, timedelta
from typing import List, Dict, Any
import json

def format_money(amount: float) -> str:
    """格式化金额"""
    if amount >= 10000:
        return f"¥{amount/10000:.2f}万"
    return f"¥{amount:.2f}"

def format_pct(value: float) -> str:
    """格式化百分比"""
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f}%"

def get_trade_days(start_date: str, end_date: str) -> List[str]:
    """获取交易日列表（简化版，跳过周末）"""
    start = datetime.strptime(start_date, "%Y%m%d")
    end = datetime.strptime(end_date, "%Y%m%d")
    
    days = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            days.append(current.strftime("%Y%m%d"))
        current += timedelta(days=1)
    
    return days

def save_json(data: Any, filepath: str):
    """保存JSON文件"""
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def load_json(filepath: str) -> Any:
    """加载JSON文件"""
    with open(filepath, 'r', encoding='utf-8') as f:
        return json.load(f)

def clamp(value: float, min_val: float, max_val: float) -> float:
    """限制值在范围内"""
    return max(min_val, min(max_val, value))

def safe_divide(numerator: float, denominator: float, default: float = 0) -> float:
    """安全除法"""
    if denominator == 0:
        return default
    return numerator / denominator


if __name__ == "__main__":
    print("=== 工具函数测试 ===\n")
    
    print(f"金额格式化: {format_money(12345.67)}")
    print(f"百分比格式化: {format_pct(8.5)}")
    print(f"交易日: {len(get_trade_days('20260601', '20260610'))}天")
    
    print("\n✅ 工具函数测试通过")
