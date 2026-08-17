"""飞书全接管白名单/黑名单过滤测试（_filter_chats_by_config）。

验证：
  - 未配置 / mode=all → 不过滤（兼容旧行为）
  - allowlist：只保留 chat_id 或群名命中的会话
  - blocklist：排除命中的会话
  - allowlist 为空 → 警告 + 不过滤（避免配置错误拉不到任何群）
  - 配置文件损坏 → 兜底不过滤
"""
from __future__ import annotations

import pytest

from interfaces.social.feishu_takeover import _filter_chats_by_config

CHATS = {
    "oc_aaa": {"name": "数字生命讨论群", "type": "group"},
    "oc_bbb": {"name": "家庭群", "type": "group"},
    "oc_ccc": {"name": "Work Group", "type": "group"},
}


@pytest.fixture
def app_yaml(tmp_path, monkeypatch):
    """把 apps/<iid>/config/app.yaml 指到 tmp_path。"""
    iid = "test-takeover-iid"
    cfg_dir = tmp_path / "apps" / iid / "config"
    cfg_dir.mkdir(parents=True)

    def _write(content: str):
        (cfg_dir / "app.yaml").write_text(content, encoding="utf-8")

    from infrastructure import config as infra_config
    monkeypatch.setattr(infra_config, "get_project_root", lambda: tmp_path)
    return iid, _write


def test_no_config_no_filter(app_yaml):
    iid, _ = app_yaml
    assert _filter_chats_by_config(CHATS, iid) == CHATS


def test_mode_all_no_filter(app_yaml):
    iid, write = app_yaml
    write("social:\n  takeover:\n    mode: all\n")
    assert _filter_chats_by_config(CHATS, iid) == CHATS


def test_allowlist_by_name_and_id(app_yaml):
    iid, write = app_yaml
    write(
        "social:\n  takeover:\n    mode: allowlist\n"
        "    allowlist:\n      - 数字生命讨论群\n      - oc_ccc\n"
    )
    kept = _filter_chats_by_config(CHATS, iid)
    assert set(kept) == {"oc_aaa", "oc_ccc"}


def test_allowlist_case_insensitive(app_yaml):
    iid, write = app_yaml
    write("social:\n  takeover:\n    mode: allowlist\n    allowlist:\n      - work group\n")
    kept = _filter_chats_by_config(CHATS, iid)
    assert set(kept) == {"oc_ccc"}


def test_empty_allowlist_falls_back(app_yaml):
    """allowlist 为空 → 不过滤（配置错误兜底，不能拉不到任何群）。"""
    iid, write = app_yaml
    write("social:\n  takeover:\n    mode: allowlist\n    allowlist: []\n")
    assert _filter_chats_by_config(CHATS, iid) == CHATS


def test_blocklist(app_yaml):
    iid, write = app_yaml
    write("social:\n  takeover:\n    mode: blocklist\n    blocklist:\n      - 家庭群\n")
    kept = _filter_chats_by_config(CHATS, iid)
    assert "oc_bbb" not in kept
    assert set(kept) == {"oc_aaa", "oc_ccc"}


def test_corrupt_yaml_falls_back(app_yaml):
    iid, write = app_yaml
    write("social: [broken: {unclosed\n")
    assert _filter_chats_by_config(CHATS, iid) == CHATS


def test_string_entry_accepted(app_yaml):
    """单字符串（非数组）容错处理。"""
    iid, write = app_yaml
    write("social:\n  takeover:\n    mode: blocklist\n    blocklist: 家庭群\n")
    kept = _filter_chats_by_config(CHATS, iid)
    assert "oc_bbb" not in kept
