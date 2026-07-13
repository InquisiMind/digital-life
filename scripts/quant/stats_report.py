#!/usr/bin/env python3
"""
统计报告生成
"""

from typing import Dict, List

def generate_summary_report(data: Dict) -> str:
    """生成摘要报告"""
    lines = []
    lines.append("=" * 50)
    lines.append("回测统计报告")
    lines.append("=" * 50)
    
    # 基本信息
    lines.append(f"\n【基本信息】")
    lines.append(f"  文件数量: {data.get('files', 0)}")
    lines.append(f"  代码行数: {data.get('lines', 0)}")
    lines.append(f"  文件大小: {data.get('size', 0):.1f}KB")
    
    # 模块统计
    modules = data.get('modules', {})
    lines.append(f"\n【模块统计】")
    for name, count in modules.items():
        lines.append(f"  {name}: {count}个")
    
    # 测试结果
    tests = data.get('tests', {})
    lines.append(f"\n【测试结果】")
    lines.append(f"  通过: {tests.get('passed', 0)}")
    lines.append(f"  失败: {tests.get('failed', 0)}")
    
    lines.append("\n" + "=" * 50)
    
    return "\n".join(lines)


if __name__ == "__main__":
    print("=== 统计报告测试 ===\n")
    
    data = {
        'files': 37,
        'lines': 3505,
        'size': 99.9,
        'modules': {
            '核心引擎': 3,
            '数据处理': 5,
            '策略分析': 4,
            '交易执行': 3,
            '风险控制': 3,
            '技术分析': 3,
            '绩效分析': 2,
            '输出报告': 4,
        },
        'tests': {
            'passed': 25,
            'failed': 0,
        }
    }
    
    report = generate_summary_report(data)
    print(report)
