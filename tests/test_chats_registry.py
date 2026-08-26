"""窗口档案（chats 表）与 ID 语义测试。

产品模型（2026-08-26 联系人机制重构）：
  OC = 窗口 ID（群/私聊一律平等）；dm|group 是 type 字段（只写真值，不猜前缀）
  OU = 用户 ID
  名称三层：事件自带 → 本地缓存（contacts/chats）→ 飞书 API 兜底
"""
from __future__ import annotations

import pytest

from domain.contacts import (
    upsert_chat,
    lookup_chat,
    search_chats,
    list_chats,
)


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """chats 表测试指向 tmp（避开真实实例库）。"""
    monkeypatch.setattr(
        "infrastructure.config.get_runtime_state_db_path",
        lambda: tmp_path / "state.db",
    )
    yield tmp_path


def test_upsert_and_lookup_roundtrip(temp_db):
    upsert_chat("oc_test_group", "三人协作群", "group")
    ch = lookup_chat("oc_test_group")
    assert ch == {"chat_id": "oc_test_group", "name": "三人协作群", "type": "group", "notes": ""}


def test_upsert_dm_window(temp_db):
    """私聊窗口与群窗口一律平等建档。"""
    upsert_chat("oc_dm_window", "张浩普", "dm")
    ch = lookup_chat("oc_dm_window")
    assert ch["type"] == "dm" and ch["name"] == "张浩普"


def test_type_only_accepts_truth_values(temp_db):
    """非法 type（如从前缀猜的）被拒：留空中性，绝不猜。"""
    upsert_chat("oc_x", "n", "p2p")  # 非法值 → 空
    assert lookup_chat("oc_x")["type"] == ""


def test_upsert_empty_values_do_not_overwrite(temp_db):
    """空 name/type 不覆盖已有（增量信息只增不减）。"""
    upsert_chat("oc_y", "旧名", "group")
    upsert_chat("oc_y", "", "")  # 后续消息没带信息
    ch = lookup_chat("oc_y")
    assert ch["name"] == "旧名" and ch["type"] == "group"


def test_search_chats_by_name(temp_db):
    upsert_chat("oc_a", "三人协作群", "group")
    upsert_chat("oc_b", "项目群", "group")
    hits = search_chats("协作")
    assert len(hits) == 1 and hits[0]["chat_id"] == "oc_a"


def test_list_chats(temp_db):
    upsert_chat("oc_a", "a", "group")
    upsert_chat("oc_b", "b", "dm")
    assert len(list_chats()) == 2


def test_lookup_unknown_returns_none(temp_db):
    """无档案 → None（中性显示，不猜类型）。"""
    assert lookup_chat("oc_never_seen") is None


def test_dm_window_is_not_group_by_prefix(temp_db):
    """核心回归：私聊窗口 oc_ 开头，type=dm——前缀推断时代它会被标 group。"""
    upsert_chat("oc_55fbac_priv", "", "dm")
    assert lookup_chat("oc_55fbac_priv")["type"] == "dm"


def test_update_chat_notes(temp_db):
    """窗口备注可更新（群聊语境由人维护，模型可见）。"""
    from domain.contacts import update_chat_notes
    upsert_chat("oc_grp", "群", "group")
    assert update_chat_notes("oc_grp", "开发讨论群") is True
    assert lookup_chat("oc_grp")["notes"] == "开发讨论群"
    assert update_chat_notes("oc_none", "x") is False  # 不存在的窗口
