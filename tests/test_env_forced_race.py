"""B根治（#3627 系列）：FORCED_ENV_KEYS 多实例竞态的两组修复。

  刀1 scheduler：唤醒路径先设实例上下文再 load_runtime_dotenv（写入者身份正确）
  刀2 action_tools：env 兜底按 app_id 反查归属实例文件配对读 secret（末写者污染免疫）
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# ── 构造临时双实例目录树 ───────────────────────────────────────────

APP_ID_A = "cli_aaa_owner"
APP_ID_B = "cli_bbb_owner"
SECRET_A = "secret_of_instance_A"
SECRET_B = "secret_of_instance_B"


@pytest.fixture()
def two_instance_tree(tmp_path, monkeypatch):
    for iid, app_id, secret in (
        ("inst-alpha", APP_ID_A, SECRET_A),
        ("inst-beta", APP_ID_B, SECRET_B),
    ):
        cfg_dir = tmp_path / "apps" / iid / "config"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "app.yaml").write_text(
            f"channels:\n  feishu:\n    app_id: {app_id}\n", encoding="utf-8"
        )
        (cfg_dir / "secrets.env").write_text(
            f"FEISHU_APP_SECRET={secret}\n", encoding="utf-8"
        )
    import infrastructure.config as iconfig
    monkeypatch.setattr(iconfig, "get_project_root", lambda: tmp_path)
    return tmp_path


# ── 刀2：_resolve_secret_for_env_app_id 配对语义 ───────────────────

def test_resolver_returns_owner_secret_under_env_pollution(two_instance_tree):
    """env 里 secret 已被实例 B 污染（末写者），凭 app_id=A 反查必须拿到 A 的 secret。"""
    from interfaces.tools.action_tools import _resolve_secret_for_env_app_id
    assert _resolve_secret_for_env_app_id(APP_ID_A) == SECRET_A
    assert _resolve_secret_for_env_app_id(APP_ID_B) == SECRET_B


def test_resolver_empty_for_unknown_app_id(two_instance_tree):
    from interfaces.tools.action_tools import _resolve_secret_for_env_app_id
    assert _resolve_secret_for_env_app_id("cli_nobody") == ""
    assert _resolve_secret_for_env_app_id("") == ""


def test_resolver_tolerates_broken_instance_dir(two_instance_tree):
    """某个实例 app.yaml 损坏 → 跳过，不炸整个扫描。"""
    broken = two_instance_tree / "apps" / "inst-alpha" / "config" / "app.yaml"
    broken.write_text("{{{{not yaml", encoding="utf-8")
    from interfaces.tools.action_tools import _resolve_secret_for_env_app_id
    assert _resolve_secret_for_env_app_id(APP_ID_B) == SECRET_B
    assert _resolve_secret_for_env_app_id(APP_ID_A) == ""


# ── 刀1：scheduler 唤醒顺序——上下文先于 dotenv 加载 ────────────────

def test_wake_sets_instance_context_before_dotenv(monkeypatch):
    """load_runtime_dotenv 必须发生在 set_current_instance_id 之后（路径解析依赖上下文）。"""
    order: list[str] = []
    import infrastructure.config as iconfig
    import infrastructure.ai as iai
    import domain.lifecycle.scheduler as sched

    monkeypatch.setattr(
        iconfig, "set_current_instance_id",
        lambda iid: order.append(f"set_ctx:{iid}") or "token",
    )
    monkeypatch.setattr(iconfig, "reset_current_instance_id", lambda tok: order.append("reset_ctx"))
    monkeypatch.setattr(iai, "load_runtime_dotenv", lambda **kw: order.append("dotenv"))
    monkeypatch.setattr(iconfig, "get_runtime_home", lambda: Path("/tmp"))
    monkeypatch.setattr(iconfig, "get_runtime_env_path", lambda: Path("/tmp/.env"))
    monkeypatch.setattr(sched, "_wake_digital_life_inner_safe", lambda *a, **kw: {"ok": True})

    sched._wake_digital_life_inner("affair-1", "test", instance_id="inst-alpha")

    assert order[0] == "set_ctx:inst-alpha", f"上下文必须最先设置，实际顺序: {order}"
    assert "dotenv" in order and order.index("dotenv") > order.index("set_ctx:inst-alpha")
    assert order[-1] == "reset_ctx"


def test_wake_without_instance_id_still_loads_dotenv(monkeypatch):
    """instance_id 为空时（兼容路径）：跳过上下文但不跳过 env 加载。"""
    order: list[str] = []
    import infrastructure.config as iconfig
    import infrastructure.ai as iai
    import domain.lifecycle.scheduler as sched

    monkeypatch.setattr(iconfig, "set_current_instance_id", lambda iid: (_ for _ in ()).throw(AssertionError("不应设置上下文")))
    monkeypatch.setattr(iai, "load_runtime_dotenv", lambda **kw: order.append("dotenv"))
    monkeypatch.setattr(iconfig, "get_runtime_home", lambda: Path("/tmp"))
    monkeypatch.setattr(iconfig, "get_runtime_env_path", lambda: Path("/tmp/.env"))
    monkeypatch.setattr(sched, "_wake_digital_life_inner_safe", lambda *a, **kw: {"ok": True})

    out = sched._wake_digital_life_inner("affair-1", "test")
    assert out == {"ok": True} and order == ["dotenv"]
