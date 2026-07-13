#!/usr/bin/env python3
"""
风险评估模块
"""

from typing import Dict, List
from enum import Enum

class RiskLevel(Enum):
    LOW = "低风险"
    MEDIUM = "中风险"
    HIGH = "高风险"
    EXTREME = "极高风险"

def assess_portfolio_risk(positions: List[Dict], capital: float) -> Dict:
    """评估组合风险"""
    if not positions:
        return {'level': RiskLevel.LOW, 'score': 0}
    
    # 计算仓位占比
    position_value = sum(p['cost_price'] * p['shares'] for p in positions)
    position_pct = position_value / capital
    
    # 计算集中度
    max_position = max(p['cost_price'] * p['shares'] for p in positions) / position_value if position_value > 0 else 0
    
    # 风险评分（0-100）
    score = 0
    
    # 仓位风险
    if position_pct > 0.8:
        score += 40
    elif position_pct > 0.5:
        score += 20
    elif position_pct > 0.3:
        score += 10
    
    # 集中度风险
    if max_position > 0.5:
        score += 30
    elif max_position > 0.3:
        score += 15
    
    # 持仓数量风险
    if len(positions) == 1:
        score += 20
    elif len(positions) == 2:
        score += 10
    
    # 确定风险等级
    if score >= 70:
        level = RiskLevel.EXTREME
    elif score >= 50:
        level = RiskLevel.HIGH
    elif score >= 30:
        level = RiskLevel.MEDIUM
    else:
        level = RiskLevel.LOW
    
    return {
        'level': level,
        'score': score,
        'position_pct': position_pct * 100,
        'concentration': max_position * 100,
        'num_positions': len(positions)
    }

def get_risk_warning(assessment: Dict) -> str:
    """获取风险预警"""
    level = assessment['level']
    
    warnings = {
        RiskLevel.LOW: "风险可控，可正常操作",
        RiskLevel.MEDIUM: "风险适中，建议适度分散",
        RiskLevel.HIGH: "风险较高，建议降低仓位或分散持仓",
        RiskLevel.EXTREME: "风险极高，建议立即降低仓位"
    }
    
    return warnings.get(level, "未知风险")


if __name__ == "__main__":
    print("=== 风险评估测试 ===\n")
    
    # 测试组合
    positions = [
        {'symbol': '513100', 'cost_price': 2.20, 'shares': 1000},
        {'symbol': '159941', 'cost_price': 1.65, 'shares': 2000},
    ]
    
    assessment = assess_portfolio_risk(positions, 100000)
    
    print(f"风险等级: {assessment['level'].value}")
    print(f"风险评分: {assessment['score']}")
    print(f"仓位占比: {assessment['position_pct']:.1f}%")
    print(f"集中度: {assessment['concentration']:.1f}%")
    print(f"预警: {get_risk_warning(assessment)}")
    
    print("\n✅ 风险评估测试通过")
