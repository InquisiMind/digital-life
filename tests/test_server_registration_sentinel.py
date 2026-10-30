"""server.py 关键注册哨兵（2026-10-30 11:25 被外部覆盖事故的防线）。

当天 server.py 被某外部操作覆盖成旧版：voice_ws 注册块、SIGUSR1 barge-in、
单守护收敛全部丢失，/voice 404、按键打断失灵——服务看起来"正常跑"但能力
静默退化。本测试锁住这些注册必须在源码里存在，覆盖再现时 pytest 即刻报警。
（覆盖来源至今未明，只能靠哨兵兜底；哨兵只看源码文本，不起服务。）
"""
from __future__ import annotations

from pathlib import Path

_SERVER = Path(__file__).resolve().parents[1] / "infrastructure" / "http" / "server.py"


def test_voice_ws_registration_present():
    src = _SERVER.read_text(encoding="utf-8")
    assert "add_voice_ws_routes" in src, "voice_ws 注册块丢失（网页语音 404）"


def test_barge_in_signal_path_present():
    src = _SERVER.read_text(encoding="utf-8")
    assert "signal.SIGUSR1" in src, "SIGUSR1 barge-in 路径丢失（worker 侧或 master 侧）"
    assert "start_hotkey_router_daemon" in src, "master 侧热键路由守护丢失"
    assert "_barge_in_worker" in src, "master→worker 的 barge-in 发信函数丢失"


def test_perception_single_guard_not_regressed():
    """单守护收敛（2026-10-13）不能被回退成逐实例启动（N 实例 = N 热键监听）。"""
    src = _SERVER.read_text(encoding="utf-8")
    assert "start_perception_daemon(instance_id)" not in src, (
        "逐实例感知守护回归（worker 各自启动 PerceptionDaemon 旧版）"
    )
