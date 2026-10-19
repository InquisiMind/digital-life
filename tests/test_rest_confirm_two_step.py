"""rest 两步式（per-session once）语义回归测试。

设计契约（见 rest 工具 schema description）：
  - 同 session 第一次调 rest（不管 until / reuse / 重叠场景）→ preview + 提示卡，不进 BLOCKED
  - 同 session 第二次调（任意参数）→ 真睡（set_alarm + BLOCKED + __l4_block__）
  - 显式 confirm=true → 跳过提示卡直接睡
  - 同 session 第三次仍直接睡（不重复打扰）
"""
from __future__ import annotations

import json
from unittest.mock import patch, MagicMock
import pytest

import interfaces.tools.action_tools as _at


@pytest.fixture
def isolated_instance(monkeypatch, tmp_path):
    iid = "test-rest-session-iid"
    from infrastructure.config import set_current_instance_id, reset_current_instance_id
    token = set_current_instance_id(iid)
    # 每个 test 用新 session_id + 清理 _rest_card_shown_sessions
    _at._rest_card_shown_sessions.clear()
    yield iid
    _at._rest_card_shown_sessions.clear()
    reset_current_instance_id(token)


def _make_args(**kw):
    args = {"until": "2026-07-13T23:30:00+08:00"}
    args.update(kw)
    return args


def test_first_call_until_returns_preview(isolated_instance, monkeypatch):
    """同 session 首次调 rest(until=...) → preview，不 BLOCKED。"""
    set_alarm_calls = []
    monkeypatch.setattr("domain.lifecycle.alarms.set_alarm",
                        lambda *a, **kw: set_alarm_calls.append((a, kw)))
    monkeypatch.setattr("domain.lifecycle.alarms.list_pending_alarms", lambda *a, **kw: [])
    monkeypatch.setattr("domain.todos.crud.list_tasks", lambda **kw: [], raising=False)
    monkeypatch.setattr("domain.project.loader.load_all_projects", lambda: {}, raising=False)
    monkeypatch.setattr("domain.memory.memory.consciousness.runtime.read_insights", lambda **kw: "", raising=False)
    monkeypatch.setattr("domain.lifecycle.runtime_context.get_current_affair", lambda: "affair-1")
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.update_affair", lambda *a, **kw: None)

    out = _at._handle_rest(_make_args(), session_id="sess-A")
    data = json.loads(out) if isinstance(out, str) else out
    assert data.get("preview") is True
    assert "__l4_block__" not in data
    assert not set_alarm_calls


def test_second_call_until_in_same_session_real_sleeps(isolated_instance, monkeypatch):
    """同 session 第二次调 rest(until=...) → 真睡（不弹提示卡）。"""
    set_alarm_calls = []
    monkeypatch.setattr("domain.lifecycle.alarms.set_alarm",
                        lambda *a, **kw: set_alarm_calls.append((a, kw)))
    monkeypatch.setattr("domain.lifecycle.alarms.list_pending_alarms", lambda *a, **kw: [])
    monkeypatch.setattr("domain.memory.memory.consciousness.runtime.read_insights", lambda **kw: "", raising=False)
    monkeypatch.setattr("domain.lifecycle.runtime_context.get_current_affair", lambda: "affair-2")
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.get_affair", lambda aid: MagicMock())
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.update_affair", lambda *a, **kw: None)
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.set_wait_intent", lambda aid, intent: None)

    # 第一次：preview
    out1 = _at._handle_rest(_make_args(), session_id="sess-B")
    data1 = json.loads(out1) if isinstance(out1, str) else out1
    assert data1.get("preview") is True

    # 第二次：真睡
    out2 = _at._handle_rest(_Make_args(), session_id="sess-B")
    data2 = json.loads(out2) if isinstance(out2, str) else out2
    assert data2.get("__l4_block__") is True, "第二次调用必须真睡"
    assert data2.get("preview") is not True
    assert set_alarm_calls, "第二次调用必须 set_alarm"


def _Make_args(**kw):
    """避免命名冲突辅助"""
    args = {"until": "2026-07-13T23:30:00+08:00"}
    args.update(kw)
    return args


def test_first_call_reuse_also_returns_preview(isolated_instance, monkeypatch):
    """首次调 reuse → 也是 preview（不再像旧版默认 confirm=True 直接睡）"""
    list_pending = MagicMock(return_value=[
        {"id": 42, "fire_at": "2026-07-14T08:00:00+08:00", "payload_json": "{}"},
    ])
    monkeypatch.setattr("domain.lifecycle.alarms.list_pending_alarms", list_pending)
    monkeypatch.setattr("domain.memory.memory.consciousness.runtime.read_insights", lambda **kw: "", raising=False)
    monkeypatch.setattr("domain.todos.crud.list_tasks", lambda **kw: [], raising=False)
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.update_affair", lambda *a, **kw: None)
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.set_wait_intent", lambda aid, intent: None)
    cancel_calls = []
    monkeypatch.setattr("domain.lifecycle.alarms.cancel_alarm",
                        lambda *a, **kw: cancel_calls.append((a, kw)))

    out = _at._handle_rest({"reuse": 42}, session_id="sess-C")
    data = json.loads(out) if isinstance(out, str) else out
    assert data.get("preview") is True
    assert data.get("will_reuse_alarm_id") == 42
    assert not cancel_calls, "首次 reuse 不应取消/合并 mental"


def test_second_call_reuse_real_sleeps(isolated_instance, monkeypatch):
    """首次 reuse preview，第二次 reuse 真睡"""
    list_pending = MagicMock(return_value=[
        {"id": 42, "fire_at": "2026-07-14T08:00:00+08:00", "payload_json": "{}"},
    ])
    monkeypatch.setattr("domain.lifecycle.alarms.list_pending_alarms", list_pending)
    monkeypatch.setattr("domain.memory.memory.consciousness.runtime.read_insights", lambda **kw: "", raising=False)
    monkeypatch.setattr("domain.todos.crud.list_tasks", lambda **kw: [], raising=False)
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.get_affair", lambda aid: MagicMock())
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.update_affair", lambda *a, **kw: None)
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.set_wait_intent", lambda aid, intent: None)
    monkeypatch.setattr("domain.lifecycle.alarms.cancel_alarm", lambda *a, **kw: None)
    monkeypatch.setattr("domain.lifecycle.alarms.set_alarm", lambda *a, **kw: None)

    out1 = _at._handle_rest({"reuse": 42}, session_id="sess-D")
    assert json.loads(out1).get("preview") is True

    out2 = _at._handle_rest({"reuse": 42}, session_id="sess-D")
    data2 = json.loads(out2) if isinstance(out2, str) else out2
    assert data2.get("__l4_block__") is True
    assert data2.get("reused_alarm_id") == 42


def test_explicit_confirm_true_skips_preview_first_call(isolated_instance, monkeypatch):
    """rest(until=..., confirm=true) 第一次就走真睡（跳过提示卡）。"""
    monkeypatch.setattr("domain.lifecycle.alarms.list_pending_alarms", lambda *a, **kw: [])
    monkeypatch.setattr("domain.memory.memory.consciousness.runtime.read_insights", lambda **kw: "", raising=False)
    monkeypatch.setattr("domain.lifecycle.runtime_context.get_current_affair", lambda: "affair-X")
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.get_affair", lambda aid: MagicMock())
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.update_affair", lambda *a, **kw: None)
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.set_wait_intent", lambda aid, intent: None)
    monkeypatch.setattr("domain.lifecycle.alarms.set_alarm", lambda *a, **kw: None)

    out = _at._handle_rest(_make_args(confirm=True), session_id="sess-E")
    data = json.loads(out) if isinstance(out, str) else out
    assert data.get("__l4_block__") is True
    assert data.get("preview") is not True


def test_overlap_with_pending_does_show_card_first_time(isolated_instance, monkeypatch):
    """until=X 和现有 timer 重叠 → 第一次也展示提示卡（不再 tool_error 后失去机会）。"""
    monkeypatch.setattr("domain.lifecycle.alarms.list_pending_alarms", lambda *a, **kw: [
        {"id": 88, "fire_at": "2026-07-13T23:35:00+08:00", "payload_json": "{}"}  # 5min 内重叠
    ])
    monkeypatch.setattr("domain.memory.memory.consciousness.runtime.read_insights", lambda **kw: "", raising=False)
    monkeypatch.setattr("domain.todos.crud.list_tasks", lambda **kw: [], raising=False)

    out = _at._handle_rest(_make_args(until="2026-07-13T23:30:00+08:00"), session_id="sess-F")
    data = json.loads(out) if isinstance(out, str) else out
    assert data.get("preview") is True, "重叠场景首次应展示提示卡"
    assert data.get("overlap") is True
    assert data.get("overlap_alarm_id") == 88
    assert "pre_rest_card" in data


def test_overlap_second_call_reuse_real_sleeps(isolated_instance, monkeypatch):
    """看完重叠提示卡后，第二次 reuse 真睡（不卡）。"""
    list_pending = MagicMock(return_value=[
        {"id": 88, "fire_at": "2026-07-13T23:35:00+08:00", "payload_json": "{}"}
    ])
    monkeypatch.setattr("domain.lifecycle.alarms.list_pending_alarms", list_pending)
    monkeypatch.setattr("domain.memory.memory.consciousness.runtime.read_insights", lambda **kw: "", raising=False)
    monkeypatch.setattr("domain.todos.crud.list_tasks", lambda **kw: [], raising=False)
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.get_affair", lambda aid: MagicMock())
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.update_affair", lambda *a, **kw: None)
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.set_wait_intent", lambda aid, intent: None)
    monkeypatch.setattr("domain.lifecycle.alarms.cancel_alarm", lambda *a, **kw: None)
    monkeypatch.setattr("domain.lifecycle.alarms.set_alarm", lambda *a, **kw: None)

    # 第一次 until 触发重叠 → preview
    out1 = _at._handle_rest(_make_args(until="2026-07-13T23:30:00+08:00"), session_id="sess-G")
    data1 = json.loads(out1) if isinstance(out1, str) else out1
    assert data1.get("preview") is True and data1.get("overlap") is True

    # 第二次 reuse=88 → 真睡
    out2 = _at._handle_rest({"reuse": 88}, session_id="sess-G")
    data2 = json.loads(out2) if isinstance(out2, str) else out2
    assert data2.get("__l4_block__") is True
    assert data2.get("reused_alarm_id") == 88


def test_different_session_each_shows_card(isolated_instance, monkeypatch):
    """两个 session 各自独立看到提示卡（per-session once，不是 process-wide once）。"""
    monkeypatch.setattr("domain.lifecycle.alarms.list_pending_alarms", lambda *a, **kw: [])
    monkeypatch.setattr("domain.memory.memory.consciousness.runtime.read_insights", lambda **kw: "", raising=False)
    monkeypatch.setattr("domain.todos.crud.list_tasks", lambda **kw: [], raising=False)

    out1 = _at._handle_rest(_make_args(), session_id="sess-H1")
    out2 = _at._handle_rest(_make_args(), session_id="sess-H2")
    d1 = json.loads(out1) if isinstance(out1, str) else out1
    d2 = json.loads(out2) if isinstance(out2, str) else out2
    assert d1.get("preview") is True
    assert d2.get("preview") is True, "不同 session 都应看到提示卡"


# ── 无参静眠（2026-09-30：闹钟时间可为空，没事不必硬设时间）─────────────


def test_no_args_first_call_preview_offers_silent_option(isolated_instance, monkeypatch):
    """首次 rest() 无参 → 提示卡 preview，note 里给出静眠选项（不是逼着设闹钟）。"""
    set_alarm_calls = []
    monkeypatch.setattr("domain.lifecycle.alarms.set_alarm",
                        lambda *a, **kw: set_alarm_calls.append((a, kw)))
    monkeypatch.setattr("domain.lifecycle.alarms.list_pending_alarms", lambda *a, **kw: [])
    monkeypatch.setattr("domain.todos.crud.list_tasks", lambda **kw: [], raising=False)
    monkeypatch.setattr("domain.memory.memory.consciousness.runtime.read_insights", lambda **kw: "", raising=False)

    out = _at._handle_rest({}, session_id="sess-I")
    data = json.loads(out) if isinstance(out, str) else out
    assert data.get("preview") is True
    assert "__l4_block__" not in data
    assert "静眠" in data.get("note", "")
    assert not set_alarm_calls


def test_no_args_second_call_silent_sleeps(isolated_instance, monkeypatch):
    """第二次 rest() 无参 → 静眠真睡：BLOCKED + WaitIntent(EVENT)，不设任何闹钟。"""
    set_alarm_calls = []
    monkeypatch.setattr("domain.lifecycle.alarms.set_alarm",
                        lambda *a, **kw: set_alarm_calls.append((a, kw)))
    monkeypatch.setattr("domain.lifecycle.alarms.list_pending_alarms", lambda *a, **kw: [])
    monkeypatch.setattr("domain.memory.memory.consciousness.runtime.read_insights", lambda **kw: "", raising=False)
    monkeypatch.setattr("domain.todos.crud.list_tasks", lambda **kw: [], raising=False)
    monkeypatch.setattr("domain.lifecycle.runtime_context.get_current_affair", lambda: "affair-S1")
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.get_affair", lambda aid: MagicMock())
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.update_affair", lambda *a, **kw: None)
    intent_calls = []
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.set_wait_intent",
                        lambda aid, intent: intent_calls.append((aid, intent)))

    out1 = _at._handle_rest({}, session_id="sess-J")
    assert json.loads(out1).get("preview") is True

    out2 = _at._handle_rest({}, session_id="sess-J")
    data = json.loads(out2) if isinstance(out2, str) else out2
    assert data.get("__l4_block__") is True, "第二次无参必须真睡"
    assert data.get("silent") is True
    assert data.get("wake_at") is None
    assert not set_alarm_calls, "静眠不设闹钟"
    assert intent_calls, "静眠要落 WaitIntent"
    _aid, intent = intent_calls[0]
    assert _aid == "affair-S1"
    from domain.lifecycle.state_machine import WaitType
    assert intent.wait_type == WaitType.EVENT
    assert intent.resume_when == ""


def test_confirm_true_no_args_silent_sleeps_first_call(isolated_instance, monkeypatch):
    """rest(confirm=true) 无参 → 首次直接静眠（一步睡，跳过提示卡）。"""
    set_alarm_calls = []
    monkeypatch.setattr("domain.lifecycle.alarms.set_alarm",
                        lambda *a, **kw: set_alarm_calls.append((a, kw)))
    monkeypatch.setattr("domain.lifecycle.alarms.list_pending_alarms", lambda *a, **kw: [])
    monkeypatch.setattr("domain.memory.memory.consciousness.runtime.read_insights", lambda **kw: "", raising=False)
    monkeypatch.setattr("domain.lifecycle.runtime_context.get_current_affair", lambda: "affair-S2")
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.get_affair", lambda aid: MagicMock())
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.update_affair", lambda *a, **kw: None)
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.set_wait_intent", lambda aid, intent: None)

    out = _at._handle_rest({"confirm": True}, session_id="sess-K")
    data = json.loads(out) if isinstance(out, str) else out
    assert data.get("__l4_block__") is True
    assert data.get("silent") is True
    assert data.get("preview") is not True
    assert not set_alarm_calls, "静眠不设闹钟"


# ── 能力闸与团队段（2026-09-30 解耦重构：精力跟着 capability 闸走，
#    团队跟着项目成员身份走——都和"是不是服务型"无关）─────────────────


def _patch_team_world(monkeypatch, *, service=True, project_status="active", todos=None):
    """把当前测试实例挂进团队世界。

    service=True：伪装挂项目的服务（vitals 能力默认关）。
    service=False：伪装被聘为 PM 的实例（vitals 恒开，实例全能力在）。
    """
    import infrastructure.persistence.services_registry as sr_mod
    import business.consulting.projects_registry as pr_mod

    iid = "test-rest-session-iid"
    if service:
        monkeypatch.setattr(
            sr_mod, "resolve_service_def", lambda rid: {"def_id": "echo-def"} if rid else None)
        monkeypatch.setattr(
            sr_mod, "lookup_service",
            lambda rid: ({"service_id": rid, "project_id": "proj-1",
                          "display_name": "行业研究员", "capabilities": {}}
                         if rid == iid else {}))
    else:
        monkeypatch.setattr(sr_mod, "resolve_service_def", lambda rid: None)
        monkeypatch.setattr(sr_mod, "lookup_service", lambda rid: {})
        monkeypatch.setattr(
            pr_mod, "list_projects",
            lambda status=None: [{"project_id": "proj-1", "pm_id": iid,
                                  "status": "active", "name": "实例PM的项目"}])
    monkeypatch.setattr(
        pr_mod, "lookup_project",
        lambda pid: ({"project_id": pid, "status": project_status, "name": "团队项目"}
                     if pid == "proj-1" else None))
    monkeypatch.setattr(
        pr_mod, "list_project_todos", lambda pid, **kw: (todos or []))
    # 个人世界（实例段）置空：团队世界的运行体大多没有个人数据
    monkeypatch.setattr("domain.todos.crud.list_tasks", lambda **kw: [], raising=False)
    monkeypatch.setattr("domain.project.loader.load_all_projects", lambda: {}, raising=False)
    monkeypatch.setattr("domain.memory.memory.consciousness.runtime.read_insights", lambda **kw: "", raising=False)
    monkeypatch.setattr("domain.lifecycle.alarms.set_alarm", lambda *a, **kw: None)
    monkeypatch.setattr("domain.lifecycle.alarms.list_pending_alarms", lambda *a, **kw: [])


def test_service_preview_no_energy_with_team_card(isolated_instance, monkeypatch):
    """挂项目的服务（vitals 关）：无 energy 字段；提示卡带团队段（项目待办）。"""
    _patch_team_world(monkeypatch, todos=[
        {"todo_id": "t1", "title": "行业研究初稿", "status": "in_progress",
         "assign_role": "行业研究员"},
        {"todo_id": "t2", "title": "等待客户材料", "status": "open",
         "assign_role": "项目经办"},
    ])

    out = _at._handle_rest({}, session_id="sess-SVC1")
    data = json.loads(out) if isinstance(out, str) else out
    assert data.get("preview") is True
    assert "energy" not in data, "vitals 关的运行体不该有精力上下文"
    card = data.get("pre_rest_card") or ""
    assert "项目待办" in card
    assert "分给我的 1 项" in card
    assert "灵感碎片" not in card, "个人世界栏目没数据就不该出现"


def test_instance_pm_gets_team_card_and_energy(isolated_instance, monkeypatch):
    """正交性：实例被聘为 PM 也带队——精力上下文在（实例恒开），
    团队段同时在。团队≠服务型。"""
    _patch_team_world(monkeypatch, service=False, todos=[
        {"todo_id": "t1", "title": "行业研究初稿", "status": "in_progress",
         "assign_role": "行业研究员"},
    ])

    out = _at._handle_rest({}, session_id="sess-PM1")
    data = json.loads(out) if isinstance(out, str) else out
    assert data.get("preview") is True
    assert isinstance(data.get("energy"), (int, float)), "实例 vitals 恒开"
    card = data.get("pre_rest_card") or ""
    assert "项目待办" in card, "实例 PM 的卡也有团队段"


def test_suspended_project_silent_sleep_via_seam(isolated_instance, monkeypatch):
    """项目挂起 → rest 两步式与其余壳子完全一致：无参先出预览卡，确认后
    静眠（不设闹钟）。接缝已删（静眠判定归 suspend_project 状态与模型
    自制，rest 不读业务状态），此用例守住"rest 行为不因挂起而特殊"。"""
    _patch_team_world(monkeypatch, project_status="suspended", todos=[])
    set_alarm_calls = []
    monkeypatch.setattr("domain.lifecycle.alarms.set_alarm",
                        lambda *a, **kw: set_alarm_calls.append((a, kw)))
    monkeypatch.setattr("domain.lifecycle.alarms.list_pending_alarms", lambda *a, **kw: [])
    # 个人世界（实例段）置空：团队世界的运行体大多没有个人数据
    monkeypatch.setattr("domain.todos.crud.list_tasks", lambda **kw: [], raising=False)
    monkeypatch.setattr("domain.project.loader.load_all_projects", lambda: {}, raising=False)
    monkeypatch.setattr("domain.memory.memory.consciousness.runtime.read_insights", lambda **kw: "", raising=False)
    monkeypatch.setattr("domain.lifecycle.runtime_context.get_current_affair", lambda: "aff-SS")
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.get_affair", lambda aid: MagicMock())
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.update_affair", lambda *a, **kw: None)
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.set_wait_intent", lambda aid, intent: None)

    out = _at._handle_rest({}, session_id="sess-SS1")
    data = json.loads(out) if isinstance(out, str) else out
    assert data.get("preview") is True, "挂起也不例外：第一步是预览卡"
    assert not set_alarm_calls

    out2 = _at._handle_rest({"confirm": True}, session_id="sess-SS1")
    data2 = json.loads(out2) if isinstance(out2, str) else out2
    assert data2.get("silent") is True and data2.get("__l4_block__") is True
    assert data2.get("wake_at") is None
    assert not set_alarm_calls


def test_no_energy_capability_message_generic(isolated_instance, monkeypatch):
    """vitals 关的运行体 rest(until, confirm)：话术无精力字样。"""
    _patch_team_world(monkeypatch, todos=[])
    monkeypatch.setattr("domain.lifecycle.runtime_context.get_current_affair", lambda: "aff-SVC")
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.get_affair", lambda aid: MagicMock())
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.update_affair", lambda *a, **kw: None)
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.set_wait_intent", lambda aid, intent: None)

    out = _at._handle_rest(
        {"confirm": True, "until": "2026-07-15T15:00:00+08:00"}, session_id="sess-SVC2")
    data = json.loads(out) if isinstance(out, str) else out
    assert data.get("__l4_block__") is True
    assert "精力" not in data.get("message", "")
    assert "提前唤醒" in data.get("message", "")


def test_instance_rest_keeps_energy_context(isolated_instance, monkeypatch):
    """实例路径回归：energy 字段与精力话术原样保留（服务分流不误伤实例）。"""
    monkeypatch.setattr("domain.lifecycle.alarms.set_alarm", lambda *a, **kw: None)
    monkeypatch.setattr("domain.lifecycle.alarms.list_pending_alarms", lambda *a, **kw: [])
    monkeypatch.setattr("domain.lifecycle.runtime_context.get_current_affair", lambda: "aff-INST")
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.get_affair", lambda aid: MagicMock())
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.update_affair", lambda *a, **kw: None)
    monkeypatch.setattr("domain.lifecycle.affairs.runtime.set_wait_intent", lambda aid, intent: None)

    out = _at._handle_rest(
        {"confirm": True, "until": "2026-07-15T15:00:00+08:00"}, session_id="sess-INST")
    data = json.loads(out) if isinstance(out, str) else out
    assert data.get("__l4_block__") is True
    assert "精力" in data.get("message", "")

    # 预览路径 energy 字段保留
    out2 = _at._handle_rest({}, session_id="sess-INST2")
    d2 = json.loads(out2) if isinstance(out2, str) else out2
    assert d2.get("preview") is True
    assert isinstance(d2.get("energy"), (int, float)), "实例预览保留 energy 快照"
