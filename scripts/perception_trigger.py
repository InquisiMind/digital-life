#!/usr/bin/env python3
"""感知触发脚本（配合 macOS 快捷指令使用）。

用法（在 macOS 快捷指令里绑 "运行 Shell 脚本"）：
  /usr/bin/python3 scripts/perception_trigger.py <instance_id> [seconds]

行为：
  - 如果没有正在录制的进程 → 开始录制（截图+录音），后台跑
  - 如果有正在录制的进程 → 停止录制，触发 pipeline 处理 + 上报

第一次按快捷键 = 开始，第二次按 = 结束。不需要辅助功能权限。
"""
from __future__ import annotations

import os
import sys
import time
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PID_FILE = Path("/tmp/perception_recording.pid")
START_FILE = Path("/tmp/perception_recording_start")


def is_recording() -> bool:
    """检查是否有正在录制的进程。"""
    if not PID_FILE.exists():
        return False
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)  # 检查进程是否存活
        return True
    except (ValueError, ProcessLookupError, PermissionError):
        PID_FILE.unlink(missing_ok=True)
        return False


def start_recording(instance_id: str) -> None:
    """开始录制——后台跑 perception_capture.py。"""
    capture_script = REPO_ROOT / "scripts" / "perception_capture.py"
    log_file = REPO_ROOT / "var" / "logs" / "perception_capture.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    proc = subprocess.Popen(
        [sys.executable, str(capture_script), "--instance", instance_id],
        stdout=open(log_file, "a"),
        stderr=subprocess.STDOUT,
        start_new_session=True,  # 独立进程组（快捷指令退出后继续跑）
    )
    PID_FILE.write_text(str(proc.pid))
    START_FILE.write_text(str(time.time()))
    print(f"录制开始 (pid={proc.pid})")


def stop_recording(instance_id: str) -> None:
    """停止录制——发停止信号，等处理完成。"""
    if not PID_FILE.exists():
        print("没有正在录制的进程")
        return

    pid = int(PID_FILE.read_text().strip())
    try:
        # 发 SIGTERM（perception_capture.py 会捕获并处理）
        os.kill(pid, 15)  # SIGTERM
        # 等最多 60 秒（pipeline 处理时间）
        for _ in range(120):
            try:
                os.kill(pid, 0)
                time.sleep(0.5)
            except ProcessLookupError:
                break
    except ProcessLookupError:
        pass

    PID_FILE.unlink(missing_ok=True)
    START_FILE.unlink(missing_ok=True)
    print("录制结束，已处理")


def main() -> int:
    instance_id = sys.argv[1] if len(sys.argv) > 1 else ""
    if not instance_id:
        # 从环境变量读默认实例
        instance_id = os.environ.get("DIGITAL_LIFE_INSTANCE_ID", "")
    if not instance_id:
        print("需要指定实例 ID")
        return 1

    if is_recording():
        stop_recording(instance_id)
    else:
        start_recording(instance_id)

    return 0


if __name__ == "__main__":
    sys.exit(main())
