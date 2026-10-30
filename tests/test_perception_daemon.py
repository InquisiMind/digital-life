"""感知 daemon + config 集成测试（feature 003 闭环）。

验证：
  - start_perception_daemon 在 enabled=false 时返回 None
  - PerceptionConfig 的 enabled/hotkey 字段正确从 app.yaml 读取
  - config_center 的 perception section 字段完整、path 正确
  - daemon 的 start/stop 不抛异常（不依赖真实 pynput 权限）
"""
from __future__ import annotations

import pytest

from infrastructure.perception.config import PerceptionConfig, DEFAULT_HOTKEY


# ── PerceptionConfig 新字段 ──────────────────────────────────────────────────


def test_config_defaults_disabled():
    """默认 enabled=False（安全：不开箱即用，需显式开启）。"""
    cfg = PerceptionConfig()
    assert cfg.enabled is False
    assert cfg.hotkey == DEFAULT_HOTKEY


def test_config_enabled_hotkey_from_dict():
    """enabled/hotkey 能从 dict 构造。"""
    cfg = PerceptionConfig(enabled=True, hotkey="cmd+shift+a")
    assert cfg.enabled is True
    assert cfg.hotkey == "cmd+shift+a"


def test_load_config_reads_enabled_hotkey(monkeypatch, tmp_path):
    """load_config 从 app.yaml 的 perception 段读 enabled/hotkey。"""
    import infrastructure.perception.config as pcfg

    monkeypatch.setattr(pcfg, "_project_root", lambda: tmp_path)
    iid = "test-load-iid"
    apps_cfg = tmp_path / "apps" / iid / "config"
    apps_cfg.mkdir(parents=True)
    (apps_cfg / "app.yaml").write_text(
        "display_name: test\n"
        "model:\n  base_url: https://x\n  name: m\n"
        "perception:\n"
        "  enabled: true\n"
        "  hotkey: 'cmd+shift+z'\n"
        "  frame_fps: 3.0\n",
        encoding="utf-8",
    )
    cfg = pcfg.load_config(iid)
    assert cfg.enabled is True
    assert cfg.hotkey == "cmd+shift+z"
    assert cfg.frame_fps == 3.0


def test_load_config_default_hotkey_when_empty(monkeypatch, tmp_path):
    """hotkey 配空 → 用 DEFAULT_HOTKEY。"""
    monkeypatch.setattr("infrastructure.config.get_project_root", lambda: tmp_path)
    monkeypatch.setattr("infrastructure.config.get_app_instance_id", lambda: "")
    from infrastructure.perception.config import load_config

    cfg = load_config("")
    assert cfg.enabled is False
    assert cfg.hotkey == DEFAULT_HOTKEY


# ── start_perception_daemon 工厂 ─────────────────────────────────────────────


def test_start_daemon_disabled_returns_none(monkeypatch):
    """enabled=False → 返回 None（静默跳过，不启动）。"""
    import infrastructure.perception.daemon as dm

    monkeypatch.setattr(dm, "load_config", lambda iid: PerceptionConfig(enabled=False))
    assert dm.start_perception_daemon("any-iid") is None


def test_start_daemon_enabled_returns_object(monkeypatch, tmp_path):
    """enabled=True → 返回 PerceptionDaemon 对象（即使 listener 起不来也返回）。"""
    import infrastructure.perception.daemon as dm

    monkeypatch.setattr(dm, "load_config", lambda iid: PerceptionConfig(
        enabled=True, hotkey="cmd+shift+q", api_key="fake"))
    # media_dir 写 state.json 需要 tmp
    monkeypatch.setattr(dm, "media_dir", lambda iid: tmp_path)
    # 不 spawn 真 Swift helper——否则测试期间注册真全局热键 cmd+shift+q，
    # 套件运行期间误按会真开麦（本测试只验证工厂返回值，不验证 listener）。
    monkeypatch.setattr(dm.PerceptionDaemon, "start", lambda self: None)
    daemon = dm.start_perception_daemon("test-enabled-iid")
    assert daemon is not None
    assert daemon.instance_id == "test-enabled-iid"
    assert daemon.config.hotkey == "cmd+shift+q"
    # stop 不抛
    daemon.stop()


class _FakeRecorder:
    """替身采集器：接口对齐 _Recorder，绝不碰麦克风/文件系统。

    历史事故：test_daemon_toggle_idempotent 曾用真 _Recorder 跑 _toggle()，
    每次 pytest 批量运行都真开麦录 ~3 秒 + 发系统通知（9/21-10/9 用户听到
    的"没按键也录音/叮声"），并在 apps/test-toggle-iid/ 落盘真实 wav。
    """

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


def test_daemon_toggle_idempotent(monkeypatch, tmp_path):
    """_toggle 在非录制状态调 _recorder.start，录制状态调 _finish。不抛。"""
    import infrastructure.perception.daemon as dm

    # 采集器必须 mock：真 _Recorder 会 spawn /usr/bin/python3 麦克风子进程
    # 并把 wav 落到真实 media_dir（test-toggle-iid 目录就是这么来的）。
    monkeypatch.setattr(dm, "_Recorder", _FakeRecorder)
    # state.json / wav 落点一并隔离（test_daemon_toggle_idempotent 同款防护）
    monkeypatch.setattr(dm, "media_dir", lambda iid: tmp_path)
    # _feedback 会发 macOS 通知 + 叮声，测试期一并静音。
    monkeypatch.setattr(dm, "_feedback", lambda *a, **kw: None)

    daemon = dm.PerceptionDaemon(
        "test-toggle-iid",
        PerceptionConfig(enabled=True, hotkey="cmd+shift+t"),
    )
    # _toggle 不依赖 listener，可直接调
    daemon._toggle()  # 开始（fake recorder）
    daemon._toggle()  # 结束（_finish_recording → 无音频不上报，不抛）
    daemon.stop()


# ── config_center perception section ────────────────────────────────────────


def test_config_center_has_perception_section():
    """config_center 注册了 perception section。"""
    from application.console.config_center import SECTION_META

    assert "perception" in SECTION_META
    meta = SECTION_META["perception"]
    assert "感知" in meta["label"]


def test_config_center_perception_fields_complete():
    """perception 字段齐全且 path 指向 perception.*。"""
    from application.console.config_center import FIELDS

    perc = [f for f in FIELDS if f.section == "perception"]
    keys = {f.key for f in perc}
    assert "perception.enabled" in keys
    assert "perception.hotkey" in keys
    assert "perception.vision_model" in keys
    assert "perception.frame_fps" in keys
    for f in perc:
        assert f.path and f.path.startswith("perception.")


def test_config_center_perception_enabled_is_boolean():
    """enabled 字段类型是 boolean（前端渲染 el-switch）。"""
    from application.console.config_center import FIELDS

    enabled = next(f for f in FIELDS if f.key == "perception.enabled")
    assert enabled.value_type == "boolean"
    assert enabled.default is False
