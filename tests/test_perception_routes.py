"""application.api.perception_routes HTTP endpoint 集成测试（spec FR-010/FR-012/FR-013）。

测试不依赖运行的 server——直接调用 handler 函数（参照 test_system_routes.py 范式，
用 ``asyncio.run`` 包裹 async handler）。

验证：
  - 直传模式（result 已结构化）→ endpoint emit perception_signal 事件，返回 ok
  - 实例解析：instance_id 为空 → resolve 到默认实例
  - 缺 source → 400
  - 媒体路径模式 → 调 run_pipeline
"""
from __future__ import annotations

import asyncio
import json

import pytest

from domain.lifecycle import events as ev


@pytest.fixture
def isolated_env(monkeypatch, tmp_path):
    """隔离实例上下文 + events DB + 默认实例解析。"""
    monkeypatch.setattr("infrastructure.config.get_project_root", lambda: tmp_path)
    # 让 resolve_instance_id("") 返回固定默认实例（避免依赖真实 registry 排序）
    monkeypatch.setattr(
        "infrastructure.config.resolve_instance_id",
        lambda raw="": "default-iid" if not raw else raw,
    )
    monkeypatch.setattr("infrastructure.config.is_instance_active", lambda iid: True)
    # 避免 _wake_or_inject 真的去查 affair
    monkeypatch.setattr(ev, "_wake_or_inject", lambda eid: None)
    import infrastructure.persistence.instance.factory as fac

    fac._BUNDLE_CACHE.clear()
    yield tmp_path
    fac._BUNDLE_CACHE.clear()


def test_direct_mode_emits_event(isolated_env, monkeypatch):
    """直传模式：body 带 result → emit perception_signal，返回 ok。"""
    from application.api import perception_routes as pr

    captured: dict = {}

    def fake_emit(kind, payload=None, fire_at=None, channel=None):
        captured["kind"] = kind
        captured["payload"] = payload or {}
        return 99999

    # perception_routes 内部 `from domain.lifecycle.events import emit_event`
    monkeypatch.setattr("domain.lifecycle.events.emit_event", fake_emit)

    body = {
        "instance_id": "",
        "source": "hotkey_screen",
        "result": {
            "summary": "屏幕上显示某股票行情",
            "details": {"screen": "stock"},
            "ok": True,
        },
        "media_path": "/tmp/cap.mp4",
    }

    async def _go():
        resp = await pr._run_perception(body, "default-iid")
        return json.loads(resp.text)

    data = asyncio.run(_go())
    assert data["ok"] is True
    assert data["perception_ok"] is True
    assert data["event_id"] == 99999
    assert captured["kind"] == "perception_signal"
    assert captured["payload"]["source"] == "hotkey_screen"
    assert "股票行情" in captured["payload"]["summary"]
    assert captured["payload"]["media_path"] == "/tmp/cap.mp4"


def test_missing_source_returns_400(isolated_env):
    """缺 source 字段 → _handle 返回 400。"""
    from application.api import perception_routes as pr

    class FakeRequest:
        async def json(self):
            return {"instance_id": "", "result": {"summary": "x"}}

    resp = asyncio.run(pr._handle_perception_trigger(FakeRequest()))  # type: ignore[arg-type]
    assert resp.status == 400


def test_media_mode_runs_pipeline(isolated_env, monkeypatch):
    """媒体路径模式：body 带 frame_paths → 调 run_pipeline（mock 掉）。"""
    from application.api import perception_routes as pr
    from infrastructure.perception.pipeline import PipelineResult

    called = {}

    def fake_run_pipeline(**kwargs):
        called.update(kwargs)
        return PipelineResult(
            ok=True, source=kwargs.get("source", ""),
            summary="mocked 理解", details={"mock": True}, frames_used=3,
        )

    monkeypatch.setattr("infrastructure.perception.run_pipeline", fake_run_pipeline)
    # perception_routes._run_perception 每次调用都 `from infrastructure.perception import`，
    # 所以 patch 源头模块即可生效。

    body = {
        "instance_id": "explicit-iid",
        "source": "sense_screen",
        "frame_paths": ["/tmp/a.png", "/tmp/b.png"],
        "media_path": "/tmp/a.png",
    }

    async def _go():
        resp = await pr._run_perception(body, "explicit-iid")
        return json.loads(resp.text)

    data = asyncio.run(_go())
    assert data["ok"] is True
    assert data["frames_used"] == 3
    assert called["instance_id"] == "explicit-iid"
    assert called["frame_image_paths"] == ["/tmp/a.png", "/tmp/b.png"]


def test_default_instance_resolved(isolated_env, monkeypatch):
    """instance_id 为空 → _handle 解析到默认实例并 set 三件套（spec FR-012）。

    走完整 _handle_perception_trigger（它负责 resolve + set contextvar），
    而非 _run_perception（后者假定上下文已设好）。
    """
    from application.api import perception_routes as pr

    seen_iid: list[str] = []
    monkeypatch.setattr(ev, "_wake_or_inject", lambda eid: None)

    def fake_emit(kind, payload=None, fire_at=None, channel=None):
        seen_iid.append(ev._get_instance_channel())
        return 1

    monkeypatch.setattr("domain.lifecycle.events.emit_event", fake_emit)

    class FakeRequest:
        async def json(self):
            return {"instance_id": "", "source": "hotkey_screen",
                    "result": {"summary": "x", "ok": True}}

    asyncio.run(pr._handle_perception_trigger(FakeRequest()))  # type: ignore[arg-type]
    # resolve_instance_id("") 被 mock 成 "default-iid"，emit 时 channel 应是它
    assert seen_iid and seen_iid[0] == "instance:default-iid"


def test_routes_registration():
    """add_perception_routes 注册路由不报错。"""
    from aiohttp import web
    from application.api import perception_routes as pr

    app = web.Application()
    pr.add_perception_routes(app)
    # 确认路由注册了
    paths = [r.resource.canonical for r in app.router.routes() if r.resource]
    assert any("perception/trigger" in p for p in paths)
