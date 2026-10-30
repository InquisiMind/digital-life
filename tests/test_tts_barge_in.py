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


# ── 队列模式：串行 + 打断清空（2026-10-28 起为 合成∥播放 流水线契约）────────

def _patch_synth(monkeypatch):
    """mock 合成（立即产出 WAV）+ 播放换成 sleep 短命进程跟踪起止。

    流水线语义：speak() 入队 → 合成线程 _synth_one → ready 缓冲 → 播放线程
    _play_bytes 按序消费。串行性由 _play_bytes 内 register→wait→unregister
    保证（同一时刻只有一个被跟踪进程，并行会互相顶掉）。
    """
    events: list[tuple] = []

    def fake_synth_one(text, voice, rate, gen):
        return b"RIFF-fake-wav", "wav"

    def fake_play_bytes(data, ext, gen, text, iid="", seq=-1):
        events.append(("start", text, gen))
        # 首段播长一点，给"打断发生在播放中"留出稳定时间窗（防测试竞态）
        dur = "2.0" if text == "正在播的" else "0.3"
        proc = subprocess.Popen(["sleep", dur],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        vo._register_playback(proc)
        try:
            proc.wait(timeout=10)
        finally:
            vo._unregister_playback(proc)
        events.append(("end", text, gen))

    monkeypatch.setattr(vo, "_synth_one", fake_synth_one)
    monkeypatch.setattr(vo, "_play_bytes", fake_play_bytes)
    return events


def _wait_events(events, n, timeout=10.0):
    import time as _t
    deadline = _t.time() + timeout
    while _t.time() < deadline and len(events) < n:
        _t.sleep(0.05)
    return [e for e in events if e[0] == "start"]


def test_queue_plays_serially_in_order(monkeypatch):
    """三次 speak 串行播放，顺序保持（无叠音）。"""
    events = _patch_synth(monkeypatch)
    vo.speak("第一段")
    vo.speak("第二段")
    vo.speak("第三段")
    starts = _wait_events(events, 6)
    assert [s[1] for s in starts] == ["第一段", "第二段", "第三段"]
    assert vo._tts_queue.empty()


def test_stop_playback_drops_queued_items(monkeypatch):
    """打断时：正在播的停 + 队列里未播的全部丢弃（代数+1，旧代不再播）。"""
    events = _patch_synth(monkeypatch)
    vo.speak("正在播的")
    vo.speak("排队1")
    vo.speak("排队2")
    # 等第一段开播即停（首段 2s 长播放，stop 必落在播放中）
    deadline = time.time() + 10
    while time.time() < deadline and not any(e[0] == "start" for e in events):
        time.sleep(0.02)
    assert vo.stop_playback() is True
    time.sleep(0.6)  # 给播放线程时间消化作废项
    assert vo._tts_queue.empty()
    # 排队项不再播出（只播过第一段）
    final_starts = [e for e in events if e[0] == "start"]
    assert all(s[1] == "正在播的" for s in final_starts), final_starts
