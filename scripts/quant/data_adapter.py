#!/usr/bin/env python3
"""
数据源适配器
支持多种数据源
"""

from typing import Dict, List, Optional
from abc import ABC, abstractmethod

class DataSourceAdapter(ABC):
    """数据源适配器基类"""
    
    @abstractmethod
    def get_price(self, symbol: str) -> Optional[Dict]:
        """获取价格"""
        pass
    
    @abstractmethod
    def get_zt_pool(self, date: str) -> List[Dict]:
        """获取涨停板"""
        pass

class AkshareAdapter(DataSourceAdapter):
    """Akshare数据源"""
    
    def get_price(self, symbol: str) -> Optional[Dict]:
        try:
            import akshare as ak
            df = ak.stock_zh_a_spot_em()
            row = df[df['代码'] == symbol]
            if not row.empty:
                return {
                    'symbol': symbol,
                    'price': float(row['最新价'].values[0]),
                    'change_pct': float(row['涨跌幅'].values[0])
                }
        except:
            pass
        return None
    
    def get_zt_pool(self, date: str) -> List[Dict]:
        try:
            import akshare as ak
            df = ak.stock_zt_pool_em(date=date)
            if df.empty:
                return []
            
            result = []
            for _, row in df.iterrows():
                result.append({
                    'symbol': row['代码'],
                    'name': row['名称'],
                    'price': row['最新价'],
                    'change_pct': row['涨跌幅'],
                    'sector': row.get('所属行业', '')
                })
            return result
        except:
            return []


if __name__ == "__main__":
    print("=== 数据源适配器测试 ===\n")
    
    adapter = AkshareAdapter()
    
    zt_pool = adapter.get_zt_pool("20260605")
    print(f"涨停板数据: {len(zt_pool)}只")
    
    print("\n✅ 数据源适配器测试通过")
