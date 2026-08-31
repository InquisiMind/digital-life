#!/usr/bin/env python3
"""macOS 权限自动请求脚本
运行后会触发系统弹窗，用户点允许即可。
"""
import subprocess
import sys

def request_accessibility():
    """触发辅助功能权限弹窗"""
    try:
        from AppKit import NSApplication, NSObject
        from ApplicationServices import AXIsProcessTrustedWithOptions
        options = {"AXTrustedCheckOptionPrompt": True}
        result = AXIsProcessTrustedWithOptions(options)
        if result:
            print("辅助功能权限已授权")
        else:
            print("辅助功能权限弹窗已触发，请在系统设置中添加 Python 并开启开关")
    except Exception as e:
        print(f"触发辅助功能弹窗失败: {e}")
        print("请手动添加：系统设置 - 隐私与安全 - 辅助功能 - 添加 Python")

def request_microphone():
    """触发麦克风权限弹窗"""
    try:
        import sounddevice as sd
        # 尝试录音 0.1 秒，触发系统弹窗
        sd.rec(int(0.1 * 44100), samplerate=44100, channels=1, dtype='float32')
        sd.wait()
        print("麦克风权限已授权（或已触发弹窗）")
    except Exception as e:
        err = str(e)
        if "Input device" in err or "Permission" in err or "Internal" in err:
            print("麦克风权限弹窗已触发，请点击允许")
        else:
            print(f"麦克风触发失败: {err}")
            print("请手动添加：系统设置 - 隐私与安全 - 麦克风 - 添加 Python")

if __name__ == "__main__":
    print("=" * 50)
    print("macOS 权限自动请求")
    print("=" * 50)
    print()
    print("[1/2] 请求辅助功能权限（用于全局快捷键监听）...")
    request_accessibility()
    print()
    print("[2/2] 请求麦克风权限（用于语音录音）...")
    request_microphone()
    print()
    print("=" * 50)
    print("如果看到弹窗，点击允许即可。")
    print("如果没有弹窗，手动到 系统设置 - 隐私与安全 添加。")
    print("=" * 50)
