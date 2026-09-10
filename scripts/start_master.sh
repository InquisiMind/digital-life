#!/bin/bash
# ADR-0001 master launchd 启动 wrapper（draft，拍板后落 scripts/）
# 复刻 social-takeover 已验证 6 天的结构；解释器保持现运行环境（miniconda 3.11）
export DIGITAL_LIFE_ROOT="/Users/zhanghaopu/Documents/项目材料/探索项目/数字生命"
export HOME="/Users/zhanghaopu"
cd "$DIGITAL_LIFE_ROOT"
exec /Users/zhanghaopu/miniconda3/bin/python3.11 gateway/main.py
