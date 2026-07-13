"""per-instance routines.yaml 作息下沉测试。

2026-06-29 作息从单份全局 config/routines.yaml 下沉到 per-instance
apps/{iid}/config/routines.yaml。load_routines(instance_id) 三层路径：
  1. 实例有 routines.yaml → 用实例版（覆盖全局）
  2. 实例无 / 读失败 → 回退全局
  3. 全局也无 → 内置 _default_routines

关键不变性（向后兼容）：load_routines()（不传 instance_id）→ 永远读全局。
这是 console API / get_quiet_hours / resolve_routine_prompt 等无实例上下文
调用保持原行为的基础。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from domain.lifecycle import routine_scheduler as rs


@pytest.fixture
def isolated_routines(tmp_path, monkeypatch):
    """隔离 routines 文件系统到 tmp_path。

    全局 routines 写到 tmp_path/global_routines.yaml，实例 routines 写到
    tmp_path/apps/{iid}/config/routines.yaml。monkeypatch routine_scheduler
    内的路径解析，让 load_routines 去读 tmp 而不是真实仓库。
    """
    # 全局 routines 模板
    global_path = tmp_path / "config" / "routines.yaml"
    global_path.parent.mkdir(parents=True, exist_ok=True)
    global_path.write_text(
        "routines:\n"
        "- id: morning_plan\n"
        "  name: 早起计划\n"
        "  time: '08:00'\n"
        "  enabled: true\n"
        "  recurrence: daily\n"
        "  prompt_template: 早晨\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(rs, "_CONFIG_PATH", global_path)

    # 复用 infrastructure.config.get_instance_routines_path 的真实解析
    # （它走 get_instance_config_path → 项目根 / apps/{iid} / config），所以
    # 也要把项目根 monkeypatch 到 tmp_path。
    from infrastructure import config as infra_config
    monkeypatch.setattr(infra_config, "get_project_root", lambda: tmp_path)

    class _Helper:
        config_dir = tmp_path / "apps"

        def write_instance(self, iid: str, content: str) -> Path:
            p = self.config_dir / iid / "config" / "routines.yaml"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            return p

    return _Helper()


def test_load_routines_instance_overrides_global(isolated_routines):
    """实例有 routines.yaml → 用实例版（覆盖全局）。

    场景: 全局 08:00, 实例版改成 08:20（错峰）→ load_routines(iid) 返回 08:20。
    """
    isolated_routines.write_instance(
        "inst-zz",
        "routines:\n"
        "- id: morning_plan\n  name: 早起\n  time: '08:20'\n"
        "  enabled: true\n  recurrence: daily\n  prompt_template: x\n",
    )
    routines = rs.load_routines("inst-zz")
    assert len(routines) == 1
    assert routines[0]["time"] == "08:20", "实例版应覆盖全局时间"
    assert routines[0]["name"] == "早起"


def test_load_routines_instance_missing_falls_back_to_global(isolated_routines):
    """实例无 routines.yaml → 回退全局（老实例兼容）。"""
    # 不给 inst-no-file 写任何 routines.yaml
    routines = rs.load_routines("inst-no-file")
    assert len(routines) == 1
    assert routines[0]["time"] == "08:00", "回退全局"
    assert routines[0]["id"] == "morning_plan"


def test_load_routines_no_context_reads_global(isolated_routines, monkeypatch):
    """无 ContextVar 实例上下文时，load_routines() → 读全局。

    （2026-06-29 BUG-B/C 联动修复后）load_routines() 默认回退 _current_instance_id()；
    当上下文为空（pytest 顶层 / 全局工具）时，才读全局作模板兜底。
    """
    # 模拟"无实例上下文"：_current_instance_id 返回 None
    monkeypatch.setattr(rs, "_current_instance_id", lambda: None)

    # 即使有 inst-zz 实例版存在，无 ContextVar 时也不应被它影响 → 读全局
    isolated_routines.write_instance(
        "inst-zz",
        "routines:\n- id: x\n  time: '09:00'\n  name: '错峰'\n",
    )
    routines = rs.load_routines()
    assert routines[0]["time"] == "08:00", "无 ContextVar → 全局(08:00)"


def test_load_routines_falls_back_to_context_var(isolated_routines, monkeypatch):
    """【2026-06-29 BUG-B/C 修复】load_routines() 缺省 → 回退 ContextVar 取当前实例。

    关键：6 处 console 调用 + get_schedule_overview + get_quiet_hours 全部
    调 load_routines() 不传 instance，修复后自动读"当前实例"的 per-instance 作息。
    靠的就是这层回退。本测试显式模拟"在实例 inst-zz 的上下文里"调 load_routines()。
    """
    # 模拟 ContextVar 染到 inst-zz
    monkeypatch.setattr(rs, "_current_instance_id", lambda: "inst-zz")
    isolated_routines.write_instance(
        "inst-zz",
        "routines:\n"
        "- id: morning_plan\n  name: 早起\n  time: '08:20'\n"
        "  enabled: true\n  recurrence: daily\n  prompt_template: x\n",
    )
    routines = rs.load_routines()  # 不传 instance_id
    assert routines[0]["time"] == "08:20", "回退 ContextVar 应读 inst-zz 实例版(08:20)"


def test_load_routines_instance_empty_yaml_falls_back(isolated_routines):
    """实例文件存在但 routines 列表空 → 回退全局（健壮性，视为'未配置'）。"""
    isolated_routines.write_instance("inst-empty", "routines: []\n")
    routines = rs.load_routines("inst-empty")
    assert len(routines) == 1
    assert routines[0]["time"] == "08:00", "空 routines 应回退全局而非返回[]"


def test_load_routines_instance_corrupt_yaml_falls_back(isolated_routines, caplog):
    """实例文件损坏（非法 YAML）→ 回退全局（不抛异常阻断调度）。"""
    isolated_routines.write_instance("inst-bad", ": : :not valid yaml: : :\n")
    routines = rs.load_routines("inst-bad")
    assert len(routines) == 1
    assert routines[0]["time"] == "08:00", "损坏 yaml 应回退全局不崩"


def test_load_global_missing_uses_defaults(tmp_path, monkeypatch):
    """全局 routines.yaml 也不存在 → 内置 _default_routines（最末兜底）。"""
    # 指一个不存在的全局路径，无实例上下文也不传 instance
    monkeypatch.setattr(rs, "_current_instance_id", lambda: None)
    monkeypatch.setattr(rs, "_CONFIG_PATH", tmp_path / "nonexistent.yaml")
    routines = rs.load_routines()
    # _default_routines 至少返回 morning_plan
    assert isinstance(routines, list) and routines
    ids = [r.get("id") for r in routines]
    assert "morning_plan" in ids


# ── save_routines: per-instance 必需 iid（BUG-A 修复）──────────────────────


def test_save_routines_requires_instance_context(isolated_routines, monkeypatch):
    """【BUG-A 修复】save_routines 无实例且无 ContextVar → ValueError。

    绝不静默写全局（那正是 BUG-A：前端在某实例页面编辑作息结果改了全局，
    污染所有实例）。找不到实例上下文就明确报错。
    """
    monkeypatch.setattr(rs, "_current_instance_id", lambda: None)
    with pytest.raises(ValueError, match="实例上下文"):
        rs.save_routines([{"id": "x", "time": "09:00"}])


def test_save_routines_via_context_var_writes_instance_file(isolated_routines, monkeypatch):
    """save_routines 缺省 iid → 回退 ContextVar → 写到该实例的 routines.yaml。

    且能被 load_routines(iid) 读回（write→read 回环验证 per-instance 落点正确）。
    """
    monkeypatch.setattr(rs, "_current_instance_id", lambda: "inst-zz")
    new_routines = [
        {"id": "morning_plan", "name": "早起", "time": "08:20",
         "enabled": True, "recurrence": "daily", "prompt_template": "x"},
    ]
    path = rs.save_routines(new_routines)
    # 写到的路径应在 inst-zz 的 config 下
    assert "inst-zz" in str(path), f"应写 inst-zz 路径，实际 {path}"
    assert path.exists()

    # 读回：用显式 iid 读，应拿到刚写的实例版
    read_back = rs.load_routines("inst-zz")
    assert read_back[0]["time"] == "08:20", "写回的实例作息能被读到"
    assert read_back[0]["name"] == "早起"


def test_save_routines_explicit_iid_writes_target_instance(isolated_routines, monkeypatch):
    """显式传 instance_id → 写到指定实例（与 ContextVar 无关）。"""
    # ContextVar 指向 inst-zz，但显式传 inst-other → 应写 inst-other
    monkeypatch.setattr(rs, "_current_instance_id", lambda: "inst-zz")
    path = rs.save_routines([{"id": "x", "time": "10:00"}], instance_id="inst-other")
    assert "inst-other" in str(path)
    assert path.exists()


def test_save_routines_does_not_touch_global(isolated_routines, monkeypatch):
    """save_routines 永远不写全局 config/routines.yaml（BUG-A 防回退）。

    即使实例上下文齐全，也只写 apps/{iid}/ 下，全局保持不动。
    """
    # 全局 routines.yaml 的 mtime 是基线
    global_path = rs._CONFIG_PATH
    global_mtime_before = global_path.stat().st_mtime

    monkeypatch.setattr(rs, "_current_instance_id", lambda: "inst-zz")
    rs.save_routines([{"id": "x", "time": "07:00"}])

    global_mtime_after = global_path.stat().st_mtime
    assert global_mtime_after == global_mtime_before, "全局 routines.yaml 不应被改动"
