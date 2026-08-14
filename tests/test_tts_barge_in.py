"""TTS 播放打断（barge-in）测试。

验证 stop_playback 的核心语义：
  - 无播放 / 已结束 → False（幂等，不打断别人）
  - 有播放 → SIGTERM 停掉 → True
  - say 降级路径同样被跟踪
用真实短命子进程（sleep）模拟播放进程，不 mock。
"""
from __future__ import annotations

import subprocess
import time

from infrastructure.perception import voice_output as vo


def _spawn(seconds: int = 30) -> subprocess.Popen:
    return subprocess.Popen(["sleep", str(seconds)])


def test_stop_playback_noop_when_idle():
    assert vo.stop_playback() is False
    assert vo.stop_playback(grace_seconds=0.5) is False


def test_stop_playback_terminates_tracked_process():
    proc = _spawn()
    vo._register_playback(proc)
    assert proc.poll() is None
    t0 = time.time()
    assert vo.stop_playback(grace_seconds=2.0) is True
    proc.wait(timeout=2)
    assert time.time() - t0 < 2.0  # 立即停，不是等 grace 超时
    # 幂等：已结束的进程再停 → False
    assert vo.stop_playback() is False


def test_stop_playback_kills_stubborn_process():
    """SIGTERM 杀不掉（trap 忽略）的进程，grace 后 SIGKILL。"""
    # sh trap 忽略 TERM，只能 KILL 杀
    proc = subprocess.Popen(
        ["sh", "-c", "trap '' TERM; sleep 30"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    vo._register_playback(proc)
    t0 = time.time()
    assert vo.stop_playback(grace_seconds=0.5) is True
    proc.wait(timeout=2)
    assert proc.returncode != 0
    assert time.time() - t0 < 2.0


def test_unregister_keeps_newer_playback():
    """旧线程退出 unregister 时不得清掉新注册的播放进程。"""
    old, new = _spawn(), _spawn()
    vo._register_playback(old)
    vo._register_playback(new)   # 新播放顶掉旧引用
    vo._unregister_playback(old) # 旧线程退出
    assert vo._playback_proc is new  # 新播放仍在跟踪
    assert vo.stop_playback() is True
    new.wait(timeout=2)
    # 清理
    old.terminate()
    old.wait(timeout=2)
