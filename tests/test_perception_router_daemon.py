"""HotkeyRouterDaemon 单守护多热键路由回归（2026-10-13 收敛）。

验证（全 mock，不碰麦克风/不 spawn helper）：
  - 路由表加载：config/perception.yaml 解析、缺失/损坏容错
  - start_hotkey_router_daemon：daemon.enabled=false 或无路由 → None
  - 触发路由：热键 A → delegate A 采集；录制中按热键 B → 忽略（单麦）
  - 再按同一热键 → 结束采集（toggle 语义跨路由保留）
  - 跨路由防抖：0.3s 内连续触发只认第一次
"""
from __future__ import annotations

from pathlib import Path

import yaml

import infrastructure.perception.daemon as dm
from infrastructure.perception.config import PerceptionConfig


class _FakeRecorder:
    """替身采集器：接口对齐 _Recorder，绝不碰麦克风/文件系统。"""

    def __init__(self, *, fps, max_seconds, out_dir, on_auto_stop=None):
        self.fps = fps
        self.max_seconds = max_seconds
        self.out_dir = out_dir
        self.on_auto_stop = on_auto_stop
        self._recording = False

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def audio_path(self):
        return None

    def start(self) -> None:
        self._recording = True

    def stop(self) -> dict:
        self._recording = False
        return {"frames": [], "audio": None, "duration": 0.0}


def _make_router(monkeypatch, tmp_path, routes):
    """构造全 mock 的 HotkeyRouterDaemon：不读真实 app.yaml、不落真实目录。"""
    monkeypatch.setattr(dm, "_Recorder", _FakeRecorder)
    monkeypatch.setattr(dm, "_feedback", lambda *a, **kw: None)
    monkeypatch.setattr(dm, "media_dir", lambda iid: tmp_path / iid)
    monkeypatch.setattr(dm, "load_config",
                        lambda iid: PerceptionConfig(enabled=True, hotkey="cmd+shift+x"))
    return dm.HotkeyRouterDaemon(routes)


def test_load_hotkey_routes_parses_yaml(tmp_path):
    p = tmp_path / "perception.yaml"
    p.write_text(
        "daemon:\n  enabled: true\n"
        "routes:\n"
        "  cmd+shift+p: aaa-111\n"
        "  cmd+shift+h: bbb-222\n",
        encoding="utf-8",
    )
    routes = dm.load_hotkey_routes(p)
    assert routes == {"cmd+shift+p": "aaa-111", "cmd+shift+h": "bbb-222"}


def test_load_hotkey_routes_missing_or_broken(tmp_path):
    assert dm.load_hotkey_routes(tmp_path / "nope.yaml") == {}
    p = tmp_path / "broken.yaml"
    p.write_text("routes: [oops", encoding="utf-8")
    assert dm.load_hotkey_routes(p) == {}


def test_start_router_disabled_returns_none(tmp_path, monkeypatch):
    p = tmp_path / "perception.yaml"
    p.write_text("daemon:\n  enabled: false\nroutes:\n  cmd+shift+p: aaa\n",
                 encoding="utf-8")
    assert dm.start_hotkey_router_daemon(config_path=p) is None


def test_start_router_no_routes_returns_none(tmp_path):
    p = tmp_path / "perception.yaml"
    p.write_text("daemon:\n  enabled: true\n", encoding="utf-8")
    assert dm.start_hotkey_router_daemon(config_path=p) is None


def test_hotkey_routes_to_correct_instance(monkeypatch, tmp_path):
    router = _make_router(monkeypatch, tmp_path,
                          {"cmd+shift+p": "zero-iid", "cmd+shift+h": "alpha-iid"})
    router._on_triggered("cmd+shift+p")
    zero_rec = router._delegates["cmd+shift+p"]._recorder
    alpha_rec = router._delegates["cmd+shift+h"]._recorder
    assert zero_rec.is_recording is True
    assert alpha_rec.is_recording is False

    # 录制中按另一实例的热键 → 单麦闸门忽略
    router._last_trigger_ts = 0.0  # 越过防抖
    router._on_triggered("cmd+shift+h")
    assert alpha_rec.is_recording is False
    assert zero_rec.is_recording is True

    # 再按同一热键 → 结束采集（toggle 语义；finish 是异步线程，轮询等待）。
    # delegate 自身还有一层 0.8s 防抖（真实按键场景防 Carbon 重复回调），
    # 测试时钟内直接归零。
    router._last_trigger_ts = 0.0
    router._delegates["cmd+shift+p"]._last_toggle_ts = 0.0
    router._on_triggered("cmd+shift+p")
    import time
    for _ in range(100):
        if not zero_rec.is_recording:
            break
        time.sleep(0.01)
    assert zero_rec.is_recording is False


def test_router_cross_route_debounce(monkeypatch, tmp_path):
    router = _make_router(monkeypatch, tmp_path,
                          {"cmd+shift+p": "zero-iid", "cmd+shift+h": "alpha-iid"})
    router._on_triggered("cmd+shift+p")
    # 0.3s 内切到另一热键 → 被防抖吞掉（既不结束 A 也不开始 B）
    router._on_triggered("cmd+shift+h")
    assert router._delegates["cmd+shift+p"]._recorder.is_recording is True
    assert router._delegates["cmd+shift+h"]._recorder.is_recording is False


def test_router_stop_terminates_helpers(monkeypatch, tmp_path):
    router = _make_router(monkeypatch, tmp_path, {"cmd+shift+p": "zero-iid"})

    class _FakeProc:
        def __init__(self):
            self.terminated = False

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

    fake = _FakeProc()
    router._helper_procs.append(("cmd+shift+p", fake))
    router.stop()
    assert fake.terminated is True
    assert router._helper_procs == []


def test_perception_daemon_uses_injected_barge_in(monkeypatch, tmp_path):
    """barge_in 钩子：注入后采集开始时调用，且不再走进程内 stop_playback。"""
    monkeypatch.setattr(dm, "_Recorder", _FakeRecorder)
    monkeypatch.setattr(dm, "_feedback", lambda *a, **kw: None)
    monkeypatch.setattr(dm, "media_dir", lambda iid: tmp_path / iid)
    # 进程内路径若被误走会 import voice_output——在这里放哨
    import sys
    monkeypatch.setitem(sys.modules, "infrastructure.perception.voice_output",
                        type(sys)("voice_output_sentinel"))

    called = []

    d = dm.PerceptionDaemon("iid-barge", PerceptionConfig(enabled=True),
                            barge_in=lambda: called.append(1) or True)
    d._toggle()
    assert called == [1]


def test_router_wires_barge_in_fn_per_route(monkeypatch, tmp_path):
    """router 把 barge_in_fn(instance_id) 注入各 delegate；只在开始采集时触发。"""
    barge_calls = []

    def barge_in_fn(iid):
        barge_calls.append(iid)
        return True

    monkeypatch.setattr(dm, "_Recorder", _FakeRecorder)
    monkeypatch.setattr(dm, "_feedback", lambda *a, **kw: None)
    monkeypatch.setattr(dm, "media_dir", lambda iid: tmp_path / iid)
    monkeypatch.setattr(dm, "load_config",
                        lambda iid: PerceptionConfig(enabled=True, hotkey="cmd+shift+x"))
    router = dm.HotkeyRouterDaemon(
        {"cmd+shift+p": "zero-iid", "cmd+shift+h": "alpha-iid"},
        barge_in_fn=barge_in_fn,
    )

    router._on_triggered("cmd+shift+h")
    assert barge_calls == ["alpha-iid"], barge_calls

    # 结束采集（toggle 的停止分支）不应再触发 barge-in
    router._last_trigger_ts = 0.0
    router._delegates["cmd+shift+h"]._last_toggle_ts = 0.0
    router._on_triggered("cmd+shift+h")
    assert barge_calls == ["alpha-iid"], barge_calls
