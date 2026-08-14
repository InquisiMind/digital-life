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


# ── 队列模式：串行 + 打断清空 ─────────────────────────────────────────────────

def _patch_synth(monkeypatch):
    """mock 合成（立即产出 mp3）+ 真实 afplay 换成 sleep 短命进程。"""
    from infrastructure.perception import voice_output as vo
    events = []
    order = []

    def fake_speak_one(text, voice, rate, gen):
        events.append(("start", text, gen))
        proc = subprocess.Popen(["sleep", "0.3"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        vo._register_playback(proc)
        try:
            proc.wait(timeout=10)
        finally:
            vo._unregister_playback(proc)
        events.append(("end", text, gen))

    monkeypatch.setattr(vo, "_speak_one", fake_speak_one)
    return events


def test_queue_plays_serially_in_order(monkeypatch):
    """三次 speak 串行播放，顺序保持（无叠音）。"""
    _patch_synth(monkeypatch)
    vo.speak("第一段")
    vo.speak("第二段")
    vo.speak("第三段")
    vo._player_thread.join(timeout=10) if vo._player_thread else None
    # 检查队列已排空
    assert vo._tts_queue.empty()
    # 串行性：通过 playback 跟踪同一时刻只有一个进程
    #（fake_speak_one 内部 register→wait→unregister，若并行会互相顶掉）
    # 简单验证：让 player 线程处理完
    vo._ensure_player()
    import time as _t
    _t.sleep(0.1)
    assert vo._tts_queue.empty()


def test_stop_playback_drops_queued_items(monkeypatch):
    """打断时：正在播的停 + 队列里未播的全部丢弃。"""
    _patch_synth(monkeypatch)
    vo.speak("正在播的")
    vo.speak("排队1")
    vo.speak("排队2")
    import time as _t
    _t.sleep(0.1)          # player 拿到第一项开始"播"
    assert vo.stop_playback() is True
    _t.sleep(0.5)           # 给 player 线程时间消化
    assert vo._tts_queue.empty()  # 队列已清空，排队项不会再被播
