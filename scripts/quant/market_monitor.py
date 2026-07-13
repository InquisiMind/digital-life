#!/usr/bin/env python3
"""
市场状态监控
判断市场强弱和风险水平
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from etf_price_v2 import get_etf_price
from typing import Dict

def get_market_status() -> Dict:
    """获取市场状态"""
    try:
        # 获取纳指ETF数据
        etf = get_etf_price('513100', use_cache=True)
        
        if 'error' in etf:
            return {'status': 'error', 'message': etf['error']}
        
        price = etf['price']
        pre_close = etf['pre_close']
        change_pct = etf['change_pct']
        
        # 判断市场强弱
        if change_pct > 2:
            market_strength = '强势'
            risk_level = '低'
        elif change_pct > 0:
            market_strength = '偏强'
            risk_level = '中低'
        elif change_pct > -2:
            market_strength = '偏弱'
            risk_level = '中'
        else:
            market_strength = '弱势'
            risk_level = '高'
        
        return {
            'symbol': '513100',
            'name': etf['name'],
            'price': price,
            'change_pct': change_pct,
            'market_strength': market_strength,
            'risk_level': risk_level,
            'suggestion': get_trading_suggestion(change_pct)
        }
    except Exception as e:
        return {'status': 'error', 'message': str(e)}

def get_trading_suggestion(change_pct: float) -> str:
    """获取交易建议"""
    if change_pct > 3:
        return "市场强势，可考虑积极参与"
    elif change_pct > 0:
        return "市场偏强，谨慎参与"
    elif change_pct > -2:
        return "市场偏弱，观望为主"
    else:
        return "市场弱势，建议空仓等待"


if __name__ == "__main__":
    print("=== 市场状态监控 ===\n")
    
    status = get_market_status()
    
    if status.get('status') == 'error':
        print(f"错误: {status['message']}")
    else:
        print(f"标的: {status['name']} ({status['symbol']})")
        print(f"价格: ¥{status['price']}")
        print(f"涨跌: {status['change_pct']:+.2f}%")
        print(f"市场强度: {status['market_strength']}")
        print(f"风险等级: {status['risk_level']}")
        print(f"交易建议: {status['suggestion']}")
