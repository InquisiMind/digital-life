"""工具可见性引擎（domain/tool_visibility.py）单元测试。

覆盖：授予存储 upsert/收回、wake 级生命周期、装配管线五层
（场景工具集 / 实例白名单 / capability 闸 / 主体旗标 / 授予覆盖默认）。
"""

import importlib

import pytest

from domain import tool_visibility as tv
from infrastructure.persistence import tool_visibility as store


@pytest.fixture()
def grants_env(tmp_path, monkeypatch):
    """授予库重定向到 tmp + 引擎进程内 wake 池清零。"""
    monkeypatch.setenv(
        "DIGITAL_LIFE_TOOL_VISIBILITY_DB", str(tmp_path / "data" / "tool_visibility.db")
    )
    store.reset_for_test()
    tv._wake_grants.clear()
    yield
    store.reset_for_test()
    tv._wake_grants.clear()


class _FakeRegistry:
    """assemble 内 `from interfaces.tools.registry import registry` 的替身。"""

    def __init__(self, toolsets=None, metadata=None):
        self._toolsets = toolsets or {}
        self._metadata = metadata or {}

    def get_toolset_for_tool(self, name):
        return self._toolsets.get(name)

    def get_tool_metadata(self, name):
        return self._metadata.get(name, {})


@pytest.fixture()
def fake_registry(monkeypatch):
    def _install(toolsets=None, metadata=None):
        reg = _FakeRegistry(toolsets, metadata)
        # 注意不能用 "pkg.mod.attr" 字符串形式——interfaces.tools 包把
        # registry 单例 re-export 成了同名属性，会遮蔽子模块路径。
        mod = importlib.import_module("interfaces.tools.registry")
        monkeypatch.setattr(mod, "registry", reg)
        return reg

    return _install


# ── 存储层 ────────────────────────────────────────────────────────────


def test_store_upsert_replace_and_delete(grants_env):
    store.upsert_grant("svc-1", "role", ["b", "a"])
    store.upsert_grant("svc-1", "preview:rest", ["t1"])
    assert store.list_grants("svc-1") == {"role": ["a", "b"], "preview:rest": ["t1"]}

    # 同 (runtime_id, source) 再发 = 整行替换
    store.upsert_grant("svc-1", "role", ["c"])
    assert store.list_grants("svc-1") == {"role": ["c"], "preview:rest": ["t1"]}

    assert store.delete_grant("svc-1", "role") is True
    assert store.delete_grant("svc-1", "role") is False  # 幂等
    assert store.list_grants("svc-1") == {"preview:rest": ["t1"]}


def test_store_durable_grant_survives_new_connections(grants_env):
    """落库即持久：换连接（模拟 worker 进程）仍读得到。"""
    store.upsert_grant("svc-9", "role", ["x"])
    # reset_for_test 不调用——新连接走同一文件
    store._SCHEMA_READY = False
    assert store.list_grants("svc-9") == {"role": ["x"]}


def test_store_empty_runtime_id_is_noop(grants_env):
    store.upsert_grant("", "role", ["x"])
    assert store.list_grants("") == {}
    assert store.delete_grant("", "role") is False


# ── wake 级生命周期 ───────────────────────────────────────────────────


def test_wake_grant_lifecycle(grants_env):
    tv.grant_wake("svc-2", ["record_thought"], source="preview:rest")
    assert tv.wake_grants("svc-2") == {"preview:rest": {"record_thought"}}
    assert tv.grants_for("svc-2") == {"record_thought"}

    tv.revoke("svc-2", source="preview:rest")
    assert tv.wake_grants("svc-2") == {}
    assert tv.grants_for("svc-2") == set()


def test_clear_wake_drops_all_sources(grants_env):
    tv.grant_wake("svc-3", ["a"], source="s1")
    tv.grant_wake("svc-3", ["b"], source="s2")
    tv.clear_wake("svc-3")
    assert tv.wake_grants("svc-3") == {}


def test_grant_ignores_empty_inputs(grants_env):
    tv.grant("", ["a"], source="s")
    tv.grant("svc-4", [], source="s")
    tv.grant("svc-4", ["a"], source="")
    assert tv.wake_grants("svc-4") == {}
    assert store.list_grants("svc-4") == {}


def test_revoke_clears_both_durable_and_wake(grants_env):
    tv.grant("svc-5", ["a"], source="role", durable=True)
    tv.grant_wake("svc-5", ["b"], source="role")  # 同 source 双层（异常态也能收干净）
    tv.revoke("svc-5", source="role")
    assert tv.grants_for("svc-5") == set()


# ── 装配管线 ──────────────────────────────────────────────────────────


def test_assemble_toolset_and_whitelist_layers(fake_registry):
    fake_registry(toolsets={"a": "actions", "b": "senses", "c": "senses", "d": "terminal"})

    # 场景工具集：toolset 名或工具名命中
    out = tv.assemble(base=["a", "b", "c", "d"], toolsets=["senses", "a"])
    assert out == ["a", "b", "c"]

    # 白名单：工具名直命中 + toolset 名命中；只会收紧
    out = tv.assemble(base=["a", "b", "c", "d"], toolsets=["senses", "a"], whitelist=["terminal", "b"])
    assert out == ["b"]

    # 均不配置 = 全量
    assert tv.assemble(base=["a", "b", "c", "d"]) == ["a", "b", "c", "d"]


def test_assemble_capability_blocked_excluded(grants_env, fake_registry, monkeypatch):
    fake_registry()
    monkeypatch.setattr(
        "domain.service.capabilities.gated_tool_names", lambda rid=None: {"vital_tool"}
    )
    out = tv.assemble(base=["ok", "vital_tool"], runtime_id="svc-x")
    assert out == ["ok"]


def test_assemble_service_only_excluded_from_all_defaults(grants_env, fake_registry, monkeypatch):
    """service_only 从实例与服务的默认面都排除——唯一入口是授予（岗位收紧的前提）。"""
    fake_registry(metadata={"collab": {"service_only": True}})
    monkeypatch.setattr(
        "infrastructure.persistence.services_registry.resolve_service_def",
        lambda rid: None if rid == "inst-1" else "echo-def",
    )

    base = ["plain", "collab"]
    assert tv.assemble(base=base, runtime_id="inst-1") == ["plain"]
    assert tv.assemble(base=base, runtime_id="svc-1") == ["plain"]

    # 授予后服务可见；实例授予同样可见（接管类场景的预留通路）
    tv.grant("svc-1", ["collab"], source="role")
    assert tv.assemble(base=base, runtime_id="svc-1") == ["plain", "collab"]


def test_assemble_host_only_hidden_for_service(grants_env, fake_registry, monkeypatch):
    fake_registry(metadata={"express_to_human": {"host_only": True}})
    monkeypatch.setattr(
        "infrastructure.persistence.services_registry.resolve_service_def",
        lambda rid: "echo-def",
    )
    assert tv.assemble(base=["plain", "express_to_human"], runtime_id="svc-1") == ["plain"]


def test_grant_overrides_every_default_exclusion(grants_env, fake_registry, monkeypatch):
    """授予优先：service_only / capability 关闭都拦不住通行证。"""
    fake_registry(metadata={"collab": {"service_only": True}})
    monkeypatch.setattr(
        "domain.service.capabilities.gated_tool_names", lambda rid=None: {"vital_tool"}
    )
    monkeypatch.setattr(
        "infrastructure.persistence.services_registry.resolve_service_def", lambda rid: None
    )

    tv.grant("inst-1", ["collab", "vital_tool"], source="takeover:feishu")
    out = tv.assemble(base=["plain", "collab", "vital_tool"], runtime_id="inst-1")
    # 默认面保序，授予新增按字典序追加在后
    assert out == ["plain", "collab", "vital_tool"]


def test_assemble_grant_no_duplicates_and_stable_order(grants_env, fake_registry):
    fake_registry()
    tv.grant("svc-6", ["zulu", "alpha"], source="role")
    out = tv.assemble(base=["alpha", "mike"], runtime_id="svc-6")
    assert out == ["alpha", "mike", "zulu"]  # 已在默认面的不重复；新增按字典序追加


# ── 等价迁移闸：引擎 vs 旧管线冻结副本（五画像，真实注册表）─────────────


def _legacy_allowed(service_id):
    """旧 project_tools_allowed 的冻结副本：行 tools → def tools → None。"""
    import yaml

    from infrastructure.config import get_project_root
    from infrastructure.persistence import services_registry as sr

    svc = sr.lookup_service(service_id) or {}
    tools = svc.get("tools")
    if isinstance(tools, list) and tools:
        return set(tools)
    def_id = sr.resolve_service_def(service_id) or ""
    if def_id:
        cfg = get_project_root() / "apps" / def_id / "config" / "app.yaml"
        try:
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        except Exception:
            return None
        d = data.get("tools")
        if isinstance(d, list) and d:
            return {str(x) for x in d}
    return None


def _legacy_face(*, runtime_id, enabled_toolsets, whitelist):
    """2026-10-16 迁移前装配管线的冻结副本（五层顺序），等价迁移基准。

    引擎演化时本函数不动；迁移期结束后整体删除。
    """
    import importlib

    reg = importlib.import_module("interfaces.tools.registry").registry
    # 算法冻结、数据随业务有意演进（如 2026-10-16 扩组 suspend/resume/complete）——
    # 常量经 tool_face 活引用，本副本冻结的只是五层顺序与交集语义。
    from business.consulting.tool_face import SERVICE_PROJECT_TOOLS
    from infrastructure.config import get_app_instance_id

    names = list(reg.get_all_tool_names())
    if enabled_toolsets:
        ts = set(enabled_toolsets)
        names = [n for n in names if reg.get_toolset_for_tool(n) in ts or n in ts]
    if whitelist:
        wl = set(whitelist)
        names = [n for n in names if n in wl or reg.get_toolset_for_tool(n) in wl]
    from domain.service.capabilities import gated_tool_names

    blocked = gated_tool_names()  # 旧管线无参调用，ContextVar 解析运行体
    if blocked:
        names = [n for n in names if n not in blocked]

    # 有意行为变更（非迁移漂移）2026-10-16：agent 自注册的 shared 工具不再
    # 漫灌到全员（zero/alpha 的 Bing 爬虫漫灌进咨询服务事故），仅经授予可见。
    # 旧管线从未有此闸；全量跑时真实 registry 可能已被其他测试热加载进
    # app_* shared 工具，这里按新规则同样剔除，闸只比对其余语义。
    names = [n for n in names if reg.get_tool_metadata(n).get("capability_scope") != "shared"]

    # 旧 filter_tool_face：
    is_svc = False
    try:
        from infrastructure.persistence.services_registry import resolve_service_def

        is_svc = resolve_service_def(runtime_id or "") is not None
    except Exception:
        pass
    # service_only 旗标的排除语义与引擎对齐（2026-10-28 起 service_only 不再
    # 是 consulting 专属清单——任何业务都可按注册表元数据声明，如协作台工具）。
    # consulting 协作工具维持旧授予面路径（allowed 交集），仅对清单之外的
    # service_only 工具做默认面排除（引擎语义：无授予不可见）。冻结的只是
    # 五层顺序与交集算法，旗标语义随引擎走。
    svc_only = {n for n in names if reg.get_tool_metadata(n).get("service_only")}
    extra_svc_only = svc_only - set(SERVICE_PROJECT_TOOLS)
    if not is_svc:
        return [n for n in names
                if n not in SERVICE_PROJECT_TOOLS and n not in extra_svc_only]
    out = [n for n in names if n != "express_to_human" and n not in extra_svc_only]
    allowed = _legacy_allowed(get_app_instance_id())
    if allowed is not None:
        out = [n for n in out if n not in SERVICE_PROJECT_TOOLS or n in allowed]
    return out


PM_TOOLS = [
    "sense_project_peers", "send_chat_message", "project_file_list",
    "project_file_read", "project_file_write", "deliver_to_shared",
    "project_todo_create", "project_todo_list", "project_todo_update",
    "sense_conversation", "search_history", "sense_file", "skill_view",
    "rest", "suspend_project", "resume_project", "project_complete",
    "sense_image", "register_attachment",
]
SOLO_TOOLS = [
    "sense_project_peers", "send_chat_message", "project_file_list",
    "project_file_read", "project_file_write", "deliver_to_shared",
    "project_todo_list", "project_todo_update", "execute_code",
    "write_file", "sense_file", "skill_view", "search_history",
    "rest", "sense_conversation",
]
DEF_FALLBACK_TOOLS = ["send_chat_message", "project_file_read", "rest", "skill_view"]


@pytest.fixture()
def persona_env(tmp_path, monkeypatch):
    """真实注册表 + 隔离项目根/服务库/授予库（与迁移前刻画环境同构）。"""
    import os

    monkeypatch.setenv("DIGITAL_LIFE_SERVICES_DB", str(tmp_path / "data" / "services.db"))
    monkeypatch.setenv(
        "DIGITAL_LIFE_TOOL_VISIBILITY_DB", str(tmp_path / "data" / "tool_visibility.db")
    )
    monkeypatch.setattr("infrastructure.config.get_project_root", lambda: tmp_path)
    from infrastructure.persistence import services_registry as sr

    sr.reset_for_test()
    store.reset_for_test()
    tv._wake_grants.clear()
    from interfaces.tools import ensure_loaded

    ensure_loaded()
    yield tmp_path
    os.environ.pop("DIGITAL_LIFE_INSTANCE_ID", None)
    sr.reset_for_test()
    store.reset_for_test()
    tv._wake_grants.clear()


def _make_def(root, def_id, tools=None):
    d = root / "apps" / def_id
    (d / "config").mkdir(parents=True, exist_ok=True)
    (d / "persona").mkdir(parents=True, exist_ok=True)
    cfg = "runtime_kind: definition\ndisplay_name: %s\nactive: false\n" % def_id
    if tools:
        cfg += "tools:\n" + "".join("  - %s\n" % t for t in tools)
    (d / "config" / "app.yaml").write_text(cfg, encoding="utf-8")
    (d / "persona" / "LIFE_PERSONA.md").write_text("# p\n", encoding="utf-8")


def _make_instance(root, iid, whitelist):
    d = root / "apps" / iid / "config"
    d.mkdir(parents=True, exist_ok=True)
    (d / "app.yaml").write_text(
        "tools:\n  whitelist:\n" + "".join("    - %s\n" % w for w in whitelist),
        encoding="utf-8",
    )


def _with_runtime(runtime_id, fn):
    from infrastructure.config import reset_current_instance_id, set_current_instance_id

    token = set_current_instance_id(runtime_id)
    try:
        return fn()
    finally:
        reset_current_instance_id(token)


def _face(runtime_id, whitelist=None):
    """走真实 AIAgent 装配路径（迁移前=旧管线，迁移后=引擎）。需在 _with_runtime 内调用。"""
    from domain.lifecycle.wake import L4_TOOLSETS
    from infrastructure.ai.agent import AIAgent
    from infrastructure.config import get_tool_whitelist

    agent = AIAgent.__new__(AIAgent)
    agent.enabled_toolsets = list(L4_TOOLSETS)
    agent.tool_whitelist = whitelist if whitelist is not None else get_tool_whitelist(runtime_id)
    return sorted(agent._enabled_tool_names())


def _build_personas(root):
    """五个画像：实例带白名单 / PM 服务 / 无 tools 成员 / solo 服务 / 开 vitals 服务。

    服务经 create_service + apply_role_face 创建——与生产发放流程同路
    （岗位面经授予生效，运行时不再读服务行）。
    """
    from business.consulting.tool_face import apply_role_face
    from domain.service import create_service
    from infrastructure.persistence import services_registry as sr

    _make_instance(root, "inst-a", ["actions", "senses", "web_search"])
    _make_def(root, "pm-def")
    pm = create_service("pm-def", display_name="PM")["service_id"]
    apply_role_face(pm, PM_TOOLS)
    _make_def(root, "aud-def", DEF_FALLBACK_TOOLS)
    aud = create_service("aud-def", display_name="审计")["service_id"]
    apply_role_face(aud, None)
    _make_def(root, "solo-def")
    solo = create_service("solo-def", display_name="单聊")["service_id"]
    apply_role_face(solo, SOLO_TOOLS)
    _make_def(root, "vit-def")
    vit = create_service("vit-def", display_name="有躯体")["service_id"]
    apply_role_face(vit, None)
    sr.update_service_fields(vit, capabilities={"vitals": True})
    return {"inst-a": ["actions", "senses", "web_search"], pm: None, aud: None, solo: None, vit: None}


def test_engine_equals_legacy_pipeline_five_personas(persona_env):
    """等价迁移闸：引擎装配结果与旧管线冻结副本逐画像相等。

    五画像 = 实例带白名单 / PM 岗位面 / 无 tools 成员（def 回退）/ solo 服务 /
    开 vitals 服务。persona_env 用真实注册表（含业务协作工具），与生产装配
    同构；任何一侧改动导致面漂移都会在此红。
    """
    from domain.lifecycle.wake import L4_TOOLSETS
    from infrastructure.config import get_tool_whitelist

    cases = _build_personas(persona_env)
    for rid, wl in cases.items():
        eff_wl = wl if wl is not None else get_tool_whitelist(rid)

        def _both():
            return (
                _face(rid, wl),
                sorted(_legacy_face(runtime_id=rid, enabled_toolsets=list(L4_TOOLSETS), whitelist=eff_wl)),
            )

        real, legacy = _with_runtime(rid, _both)
        assert real == legacy, f"{rid}: {set(real) ^ set(legacy)}"


def test_face_semantic_invariants(persona_env):
    """语义不变式（对注册表污染不敏感的关键签名）。"""
    cases = _build_personas(persona_env)
    pm_id = [r for r in cases if r.startswith("svc-")][0]

    inst = _with_runtime("inst-a", lambda: _face("inst-a", ["actions", "senses", "web_search"]))
    assert "express_to_human" in inst and "terminal" in inst
    assert "send_chat_message" not in inst and "sense_project_peers" not in inst  # 实例无协作工具
    assert "suspend_project" not in inst and "resume_project" not in inst  # 项目动作只在岗位授予

    pm = _with_runtime(pm_id, lambda: _face(pm_id))
    assert "send_chat_message" in pm and "project_todo_create" in pm  # 岗位面内
    assert {"suspend_project", "resume_project", "project_complete"} <= set(pm)  # PM 动作经授予
    assert "express_to_human" not in pm  # 服务无对人的嘴
    assert "todo" not in pm and "sense_vitals" not in pm  # capability 默认关
    assert "terminal" in pm  # 通用工具服务全量可见

    solo_id = [r for r in cases if r.startswith("svc-")][2]
    solo = _with_runtime(solo_id, lambda: _face(solo_id))
    assert "project_todo_create" not in solo and "project_todo_list" in solo  # solo 角色无 create
