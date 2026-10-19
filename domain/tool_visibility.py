"""工具可见性引擎 — 可见 = 默认名单 ∪ 通行证池（2026-10-16 收敛）。

两个正交概念（zhp 定稿语义）：

  - 默认名单（固定层）：agent 天生可见的工具。推导 = 注册表全量
    ∩ 场景工具集(enabled_toolsets，L4 wake reason 级) ∩ 实例白名单
    (apps/<iid>/config/app.yaml tools.whitelist) − capability 关闭工具
    − 主体旗标排除（见下）。
  - 通行证池（可扩展层）：条件成立才可见。唯一写入口 grant/revoke。
    **授予优先**——池内有就可见，覆盖默认层的任何排除（service_only /
    capability 关闭都拦不住授予）。

长期授予落库（infrastructure/persistence/tool_visibility.py；发放侧在
建项目/master 进程，消费侧在服务 worker 独立进程，须共享库）。本轮（wake
级）授予在进程内存：AIAgent 每 wake 新建，__post_init__ 调 clear_wake
即"每轮开始时通行证作废"。

主体旗标（注册期声明在工具上，基座只认旗标不认业务清单）：
  - service_only=True：从所有默认面排除。协作工具——实例间协作走 broadcast
    对等链语义不同，服务侧则必须经岗位授予才可见（这是岗位收紧能成立的
    前提）；实例今后经授予同样可获得（接管类场景的预留通路）。
  - host_only=True：服务默认面排除。express_to_human——服务没有"对人的
    嘴"；web 咨询部署无飞书凭据时调用必败诱发模型连环重试（2026-10-13
    svc-657a5b078201 事故），对客户/群发言统一走 send_chat_message。
  - capability_scope="shared"：agent 自注册（register_tool）的 shared 层
    工具，从所有默认面排除。注册者注册时即获授予；其他实例/服务显式授予才
    可见。personal/project 自注册工具由 manifest 文件布局天然限定（只装进
    作者/项目成员自己的进程），不另设闸。

已知的等价性事实（迁移自旧管线，勿"顺手修"）：
  - get_tool_whitelist 读 apps/<iid>/config/app.yaml 原路径，不走路由
    中枢——服务型实例该文件不存在，白名单层对服务恒 None。行为与旧
    管线一致。
  - capability 闸消费 domain/service/capabilities.py 的既有函数；该模块
    的其他消费方（vitals 持久化、记忆索引、精力话术）不经过本引擎。
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger("digital_life.domain.tool_visibility")

# 授予来源约定：业务发放统一走 apply_role_face（source="role"）；
# preview 条件暴露用 "preview:<工具名>"；将来的接管等场景自选 source。
SOURCE_ROLE = "role"

# wake 级授予：进程内 {runtime_id: {source: set(tools)}}。
# 服务 worker 与 master 各自一份、互不可见——wake 语义本就限定单进程一轮。
_wake_grants: dict[str, dict[str, set[str]]] = {}
_lock = threading.Lock()


# ── 写入口（业务唯一 API）─────────────────────────────────────────────


def grant(runtime_id: str, tools, *, source: str, durable: bool = True) -> None:
    """发放通行证。durable=True 落库（直到 revoke），False 仅本轮（wake 清零）。"""
    tools = sorted({str(t) for t in (tools or []) if t})
    if not runtime_id or not source or not tools:
        return
    if durable:
        from infrastructure.persistence import tool_visibility as store

        store.upsert_grant(runtime_id, source, tools)
        return
    with _lock:
        _wake_grants.setdefault(runtime_id, {})[source] = set(tools)


def grant_wake(runtime_id: str, tools, *, source: str) -> None:
    """本轮授予的具名形式（preview 条件暴露走这里）。"""
    grant(runtime_id, tools, source=source, durable=False)


def revoke(runtime_id: str, *, source: str) -> None:
    """按来源收回：长期行删除 + 本轮池清除。不存在时静默。"""
    if not runtime_id or not source:
        return
    from infrastructure.persistence import tool_visibility as store

    try:
        store.delete_grant(runtime_id, source)
    except Exception:
        logger.warning("revoke: grant delete failed runtime=%s source=%s", runtime_id, source, exc_info=True)
    with _lock:
        grants = _wake_grants.get(runtime_id)
        if grants is not None:
            grants.pop(source, None)
            if not grants:
                _wake_grants.pop(runtime_id, None)


def clear_wake(runtime_id: str) -> None:
    """清空该运行体的全部本轮授予（AIAgent 每 wake 新建时调用）。"""
    with _lock:
        _wake_grants.pop(runtime_id, None)


# ── 读取 ──────────────────────────────────────────────────────────────


def durable_grants(runtime_id: str) -> dict[str, list[str]]:
    from infrastructure.persistence import tool_visibility as store

    return store.list_grants(runtime_id)


def wake_grants(runtime_id: str) -> dict[str, set[str]]:
    with _lock:
        return {s: set(t) for s, t in _wake_grants.get(runtime_id, {}).items()}


def grants_for(runtime_id: str) -> set[str]:
    """通行证池全量（长期 ∪ 本轮）。"""
    pool: set[str] = set()
    for tools in durable_grants(runtime_id).values():
        pool.update(tools)
    for tools in wake_grants(runtime_id).values():
        pool.update(tools)
    return pool


# ── 装配（唯一评估点）────────────────────────────────────────────────


def assemble(
    *,
    base,
    toolsets=None,
    whitelist=None,
    runtime_id: str = "",
) -> list[str]:
    """计算生效工具面。参数与旧管线逐层对应：

    base       注册表当前全部可见工具名（registry.get_all_tool_names()）。
    toolsets   场景工具集（L4 wake reason 级）；None=不筛选。条目可为
               toolset 名或工具名（旧语义原样）。
    whitelist  实例白名单（apps/<iid>/config/app.yaml tools.whitelist）；
               None=不筛选。只会收紧。
    runtime_id 运行体 id——capability 闸、主体旗标、授予池都按它解析。
    """
    from interfaces.tools.registry import registry

    names = [n for n in (base or [])]

    # ① 场景工具集
    if toolsets:
        ts = set(toolsets)
        names = [n for n in names if registry.get_toolset_for_tool(n) in ts or n in ts]

    # ② 实例白名单（只会收紧；服务型调用方传 None，与旧管线一致）
    if whitelist:
        wl = set(whitelist)
        names = [n for n in names if n in wl or registry.get_toolset_for_tool(n) in wl]

    # ③ capability 闸：服务型默认无精力/作息"躯体"，对应工具默认面不给。
    #    不靠提示词自觉，装配层就不给（设计文档 v1.1 特性 4）。
    blocked = _capability_blocked(runtime_id)
    if blocked:
        names = [n for n in names if n not in blocked]

    # ④ 主体旗标：service_only 对所有默认面排除（协作工具的唯一入口是
    #    授予——这是岗位收紧能成立的前提）；host_only 仅服务默认面排除；
    #    capability_scope=shared（agent 自注册的 shared 工具）对所有默认面
    #    排除——"全实例可选启用"按字面落实：注册者注册时即授予，其他人显式
    #    授予才可见，不再漫灌。
    is_service = _is_service(runtime_id)
    dropped = set()
    for n in names:
        meta = registry.get_tool_metadata(n)
        if (
            meta.get("service_only")
            or (is_service and meta.get("host_only"))
            or meta.get("capability_scope") == "shared"
        ):
            dropped.add(n)
    if dropped:
        names = [n for n in names if n not in dropped]

    # ⑤ 通行证池：授予优先——池内即可见，覆盖以上一切排除。
    #    位于白名单之后（躯体/授予是机制，不受业务白名单管——沿袭 V6 规范）。
    pool = grants_for(runtime_id)
    if pool:
        seen = set(names)
        names = names + sorted(pool - seen)

    return names


def _is_service(runtime_id: str) -> bool:
    if not runtime_id:
        return False  # 无 id 按实例兜底（与 capability_enabled 同方向）
    try:
        from infrastructure.persistence.services_registry import resolve_service_def

        return resolve_service_def(runtime_id) is not None
    except Exception:
        logger.debug("service resolution failed; treating as instance", exc_info=True)
        return False


def _capability_blocked(runtime_id: str) -> set[str]:
    try:
        from domain.service.capabilities import gated_tool_names

        return gated_tool_names(runtime_id)
    except Exception:
        logger.debug("capability gate resolution failed; treating all on", exc_info=True)
        return set()
