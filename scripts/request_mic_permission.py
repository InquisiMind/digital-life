#!/usr/bin/env python3
"""请求 macOS 麦克风/屏幕录制权限（feature 003-perception 配套）。

macOS 对命令行 python 不弹权限窗（无 bundle identifier），导致 sounddevice
静默返回全 0 数据。本脚本用 AVFoundation 原生 API 主动请求权限，触发系统弹窗。

必须在 DigitalLife.app bundle 内运行才能弹窗（见 scripts/build_app_bundle.sh）。
直接用裸 python 运行会直接返回 denied（不弹窗）——这是 macOS 的限制。

用法：
  # 通过 app bundle 运行（会弹窗）：
  open scripts/DigitalLife.app --args --request-permissions

  # 直接运行（仅查看当前状态，不弹窗）：
  python3 scripts/request_mic_permission.py
"""
from __future__ import annotations

import sys
import time


def check_and_request_mic() -> int:
    """检查并请求麦克风权限。返回状态码：0=已授权，1=未授权/被拒。"""
    try:
        import AVFoundation as AVF
    except ImportError:
        print("PyObjC AVFoundation 不可用，无法请求麦克风权限")
        return 1

    status = AVF.AVCaptureDevice.authorizationStatusForMediaType_(AVF.AVMediaTypeAudio)
    # 0 = notDetermined, 1 = restricted, 2 = authorized, 3 = denied
    status_names = {0: "未决定（应弹窗）", 1: "受限", 2: "已授权 ✓", 3: "已拒绝 ✗"}
    print(f"麦克风权限状态: {status_names.get(status, status)}")

    if status == 2:
        return 0
    if status == 3:
        print("权限已被拒绝，请到 系统设置 → 隐私与安全性 → 麦克风 重新允许")
        return 1
    if status == 1:
        print("权限受限（可能是 MDM 策略）")
        return 1

    # status == 0：请求权限（只有在 app bundle 内才会弹窗）
    print("正在请求麦克风权限（应在 app bundle 内弹出系统对话框）...")
    result = {"granted": None}

    def handler(granted):
        result["granted"] = bool(granted)

    AVF.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
        AVF.AVMediaTypeAudio, handler
    )
    # 等待异步回调（用户可能要几秒才点）
    for _ in range(30):
        if result["granted"] is not None:
            break
        time.sleep(0.5)

    if result["granted"] is None:
        print("请求超时（30s 无响应）——可能没弹窗（非 bundle 运行）")
        return 1
    if result["granted"]:
        print("✓ 麦克风权限已授权")
        return 0
    print("✗ 麦克风权限被拒绝")
    return 1


def verify_recording() -> bool:
    """授权后验证 sounddevice 能否真正录到声音。"""
    try:
        import sounddevice as sd
        import numpy as np
    except ImportError:
        print("sounddevice/numpy 不可用，跳过录音验证")
        return False

    print("录制 2 秒验证（请说话）...")
    data = sd.rec(int(2 * 16000), samplerate=16000, channels=1, dtype="int16")
    sd.wait()
    max_val = int(np.abs(data).max())
    nonzero_pct = float((np.abs(data) > 100).mean() * 100)
    print(f"  最大值: {max_val}, 非零比例: {nonzero_pct:.1f}%")
    if max_val > 100:
        print("✓ 麦克风录音正常")
        return True
    print("✗ 仍然录不到声音（权限可能没生效，需重启进程）")
    return False


def main() -> int:
    print("=== 麦克风权限检查 ===")
    rc = check_and_request_mic()
    if rc == 0:
        verify_recording()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
