#!/usr/bin/env python3
"""
配置文件加载
"""

import os
import configparser
from typing import Dict, Any

DEFAULT_CONFIG = {
    'trading': {
        'stop_loss': 0.08,
        'take_profit': 0.20,
        'max_position': 0.50,
        'slippage': 0.001,
        'commission': 0.0003,
    },
    'account': {
        'initial_capital': 100000,
    },
    'strategy': {
        'min_continuous': 1,
        'max_break_count': 0,
        'limit_before_hour': 10,
    }
}

def load_config(config_path: str = None) -> Dict[str, Any]:
    """加载配置文件"""
    config = DEFAULT_CONFIG.copy()
    
    if config_path and os.path.exists(config_path):
        parser = configparser.ConfigParser()
        parser.read(config_path)
        
        # 读取配置
        for section in parser.sections():
            if section not in config:
                config[section] = {}
            
            for key, value in parser.items(section):
                # 尝试转换类型
                try:
                    if '.' in value:
                        config[section][key] = float(value)
                    else:
                        config[section][key] = int(value)
                except ValueError:
                    config[section][key] = value
    
    return config

def get_trading_config(config: Dict) -> Dict:
    """获取交易配置"""
    return config.get('trading', DEFAULT_CONFIG['trading'])

def get_account_config(config: Dict) -> Dict:
    """获取账户配置"""
    return config.get('account', DEFAULT_CONFIG['account'])


if __name__ == "__main__":
    print("=== 配置加载测试 ===\n")
    
    config = load_config()
    
    print("默认配置:")
    for section, values in config.items():
        print(f"  [{section}]")
        for k, v in values.items():
            print(f"    {k} = {v}")
    
    print("\n✅ 配置加载测试通过")
