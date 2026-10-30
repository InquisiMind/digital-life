"""Agent 分组注册表（data/agent_def_groups.json）——管理台对 def 集合的组织视图。

文件夹心智：一个 def 只属于一个组；assignments 未出现的 def = 未分组。
组的定义（名字/顺序）与归属同住一个文件（单一事实源），PUT 整体保存幂等。
"""

from __future__ import annotations

import asyncio
import json

import pytest


class _FakeRequest:
    def __init__(self, body=None):
        self._body = body or {}

    async def json(self):
        return self._body


@pytest.fixture()
def groups_env(tmp_path, monkeypatch):
    monkeypatch.setattr("infrastructure.config.get_project_root", lambda: tmp_path)
    # 造两个真实 def（PUT 校验归属的 def 必须存在）
    for d in ("consulting-partner-def", "echo-def", "strategy4-pm"):
        cfg = tmp_path / "apps" / d / "config"
        cfg.mkdir(parents=True)
        (cfg / "app.yaml").write_text("runtime_kind: definition\n", encoding="utf-8")
    return tmp_path


def _get() -> dict:
    from business.consulting import agent_admin_routes as r

    resp = asyncio.run(r.handle_def_groups_get(_FakeRequest()))
    return json.loads(resp.text)


def _put(body: dict) -> tuple[int, dict]:
    from business.consulting import agent_admin_routes as r

    resp = asyncio.run(r.handle_def_groups_put(_FakeRequest(body)))
    return resp.status, json.loads(resp.text)


def test_roundtrip_and_single_source(groups_env):
    # 空注册表：无组、无归属
    assert _get() == {"ok": True, "groups": [], "assignments": {}, "layout": {}}

    body = {
        "groups": [{"id": "g1", "name": "咨询4"}, {"id": "g2", "name": "测试"}],
        "assignments": {"strategy4-pm": "g1", "consulting-partner-def": "g1"},
        "layout": {"d:strategy4-pm": {"c": 3, "r": 1}, "g:g1": {"c": 0, "r": 0},
                   "d:ghost-def": {"c": 1, "r": 1}, "x:bad": {"c": 0, "r": 0},
                   "d:echo-def": {"c": "bad", "r": 1}, "g:g2": {"c": 99, "r": 0}},
    }
    status, d = _put(body)
    assert status == 200 and d["ok"]
    assert [g["id"] for g in d["groups"]] == ["g1", "g2"]          # 顺序保留
    assert d["assignments"] == body["assignments"]
    # 落盘即注册表（单一来源）
    store = json.loads((groups_env / "data" / "agent_def_groups.json").read_text(encoding="utf-8"))
    assert store["groups"] == d["groups"]
    # GET 读回一致
    assert _get()["assignments"] == body["assignments"]
    # layout 透传：合法坐标保留，ghost def / 非法 kind / 越界坐标剔除
    assert _get()["layout"] == {"d:strategy4-pm": {"c": 3, "r": 1}, "g:g1": {"c": 0, "r": 0}}


def test_put_filters_garbage(groups_env):
    """组 id 重复/缺 id/缺名、归属指向不存在的组或 def —— 全部静默剔除。"""
    _put({"groups": [{"id": "g1", "name": "咨询4"}],
          "assignments": {"strategy4-pm": "g1"}})
    status, d = _put({
        "groups": [
            {"id": "g1", "name": "咨询4"},
            {"id": "g1", "name": "重 id"},
            {"id": "", "name": "缺 id"},
            {"id": "g3"},                       # 缺名
            "not-a-dict",
            {"id": "g2", "name": "测试"},
        ],
        "assignments": {
            "strategy4-pm": "g1",
            "strategy4-pm": "g2",               # 重复键（json 解析后者胜）
            "echo-def": "g-nope",               # 组不存在
            "ghost-def": "g1",                  # def 不存在
            "echo-def": "g2",
        },
    })
    assert status == 200
    assert [g["id"] for g in d["groups"]] == ["g1", "g2"]
    assert d["assignments"] == {"strategy4-pm": "g2", "echo-def": "g2"}


def test_delete_group_orphan_assignments_lazy_reclaimed(groups_env):
    """删组后残留归属：读侧懒回收（未分组），文件里可以留但 GET 看不见。"""
    _put({"groups": [{"id": "g1", "name": "咨询4"}],
          "assignments": {"strategy4-pm": "g1"}})
    # 保留 assignments 不动、只删组（模拟手写文件/异常序列）
    store = groups_env / "data" / "agent_def_groups.json"
    data = json.loads(store.read_text(encoding="utf-8"))
    data["groups"] = []
    store.write_text(json.dumps(data), encoding="utf-8")
    d = _get()
    assert d["groups"] == [] and d["assignments"] == {}
    # 指向已删 def 的归属同样回收
    data["groups"] = [{"id": "g9", "name": "x"}]
    data["assignments"] = {"deleted-def": "g9"}
    store.write_text(json.dumps(data), encoding="utf-8")
    assert _get()["assignments"] == {}


def test_put_rejects_invalid_body(groups_env):
    """非法请求体（非 JSON / 非对象）必须 400 拒绝——绝不静默落空表。

    2026-10-22 实测事故：前端把 undefined 序列化成字符串 "undefined" 发来，
    旧实现 except 静默当 {} 处理，一次拖拽清空了整个注册表。
    """
    from business.consulting import agent_admin_routes as r

    _put({"groups": [{"id": "g1", "name": "咨询4"}],
          "assignments": {"echo-def": "g1"}})

    class _RawBody:
        async def json(self):
            raise ValueError("not json")

    resp = asyncio.run(r.handle_def_groups_put(_RawBody()))
    assert resp.status == 400

    class _NotDict:
        async def json(self):
            return "undefined"

    resp2 = asyncio.run(r.handle_def_groups_put(_NotDict()))
    assert resp2.status == 400

    # 拒绝后注册表原样保留
    d = _get()
    assert [g["id"] for g in d["groups"]] == ["g1"]
    assert d["assignments"] == {"echo-def": "g1"}
