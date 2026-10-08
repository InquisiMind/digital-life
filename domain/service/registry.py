"""服务型运行体的领域操作：创建 / 归档 / 启用 / 服务级闸规则。

领域规则（《总体设计文档》v1.1）：
  - 服务必须从 agent 定义出发创建：def 必须是标记了
    ``runtime_kind: definition`` 的定义目录（apps/{def}/config/app.yaml）。
  - 目录骨架（D1=B）：apps/{def}/services/{service_id}/ 下只放运行时数据
    （data/、workspace/）；persona/skills/app.yaml/secrets 是定义层资产，
    由路径中枢二跳共享，不复制进服务目录（避免漂移）。
  - 服务级触发源默认关（特性 3）：订阅默认只有消息类——消息是对话服务的
    存在意义；其余事件（闹钟/项目/广播）须显式写入 subscriptions。
  - 归档 archived = 事实上的封存：事件层拒绝（messaging.emit_to_service）
    + 调度循环不再拉起（service_runner 只扫 active），不需要状态机
    （特性 1：开关 + 记忆持久天然满足"完成后封存、新对话可再入"）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from infrastructure.persistence import services_registry

logger = logging.getLogger("digital_life.domain.service")

# 服务级订阅永远放行的事件种类（对齐实例级订阅闸的 _ALWAYS_PASSTHROUGH_KINDS
# 中与对话相关的子集；birth 是服务自身首次引导的内部事件，不经此闸）。
SERVICE_PASSTHROUGH_KINDS = {"message", "group_message"}


def validate_agent_def(def_id: str) -> Path:
    """校验 agent 定义目录存在且标记为 definition，返回其路径。

    定义目录必须显式标记 runtime_kind: definition——没有标记的 apps/ 条目
    会被当作普通实例（supervisor 会拉起它）。这是防止"定义被当实例跑"的闸。
    """
    import yaml

    from infrastructure.config import get_project_root

    cfg_path = get_project_root() / "apps" / def_id / "config" / "app.yaml"
    if not cfg_path.exists():
        raise ValueError(
            f"agent 定义不存在: apps/{def_id}/config/app.yaml（先创建定义目录并标记 "
            "runtime_kind: definition，参考 scripts/service_create.py bootstrap-echo）"
        )
    try:
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise ValueError(f"agent 定义 app.yaml 解析失败: {cfg_path} ({exc})") from exc
    if not isinstance(cfg, dict) or str(cfg.get("runtime_kind") or "").strip() != "definition":
        raise ValueError(
            f"apps/{def_id} 未标记 runtime_kind: definition——"
            "普通实例不能作为服务定义（它会被 supervisor 当实例拉起）"
        )
    return cfg_path.parent.parent


def create_service(
    agent_def_id: str,
    *,
    service_id: str | None = None,
    project_id: str = "",
    display_name: str = "",
    capabilities: dict | None = None,
    subscriptions: list | None = None,
    tools: list | None = None,
) -> dict:
    """注册服务并落目录骨架，返回服务行。

    新建服务要指定的四件事（设计文档 6.1）：基于哪个定义（此处）、属于哪个
    项目（project_id，可空=未挂项目）、初始上下文与对客身份（上层职责，
    刀 3 管理面接入；此处先落注册表字段）。tools：岗位工具面（模版角色）。
    """
    validate_agent_def(agent_def_id)
    svc = services_registry.create_service(
        agent_def_id,
        service_id=service_id,
        project_id=project_id,
        display_name=display_name,
        capabilities=capabilities,
        subscriptions=subscriptions,
        tools=tools,
    )
    sid = svc["service_id"]
    from infrastructure.config import get_project_root

    svc_dir = get_project_root() / "apps" / agent_def_id / "services" / sid
    (svc_dir / "data").mkdir(parents=True, exist_ok=True)
    (svc_dir / "workspace").mkdir(parents=True, exist_ok=True)
    logger.info(
        "SERVICE_SKELETON service_id=%s dir=%s", sid, svc_dir
    )
    return svc


def archive_service(service_id: str) -> bool:
    """归档（服务开关关）：事件层拒绝 + 调度不再拉起。记忆保留，可再启用。"""
    ok = services_registry.set_service_status(service_id, services_registry.SERVICE_STATUS_ARCHIVED)
    if ok:
        logger.info("SERVICE_ARCHIVED service_id=%s", service_id)
    return ok


def activate_service(service_id: str) -> bool:
    """恢复接收：新消息到来自然唤醒并接上原上下文（服务记忆还在库里）。"""
    ok = services_registry.set_service_status(service_id, services_registry.SERVICE_STATUS_ACTIVE)
    if ok:
        logger.info("SERVICE_ACTIVATED service_id=%s", service_id)
    return ok


def get_service(service_id: str) -> Optional[dict]:
    return services_registry.lookup_service(service_id)


def list_services(status: str | None = None) -> list[dict]:
    return services_registry.list_services(status=status)


def service_allows_kind(svc: dict, kind: str) -> bool:
    """服务级订阅闸（特性 3）。消息类永远放行；其余按 subscriptions（all / 列表）。"""
    if kind in SERVICE_PASSTHROUGH_KINDS:
        return True
    subs = svc.get("subscriptions") or []
    if subs == "all":
        return True
    return kind in subs


# ── 项目关闭（僵尸项目治理）：归档 + 取消成员闹钟 + 成员静眠 ────────────


def close_project(project_id: str, *, reason: str = "") -> dict:
    from infrastructure.persistence.services_registry import (
        SERVICE_STATUS_ARCHIVED,
        list_services_by_project,
        lookup_project,
        set_service_status,
        update_project_fields,
    )
    """优雅关闭项目：客户弃单/静默超时的统一收口。

    做四件事（幂等）：
    1. 项目 status → archived；
    2. 删除全部成员未消费的未来 timer 事件（闹钟循环的根源）；
    3. 成员服务 status → archived（事件层拒绝，永不唤醒）；
    4. 返回收尾统计。
    记忆/产物/待办全部保留——重新激活走 activate。
    """
    import sqlite3 as _sq

    from infrastructure.config import resolve_runtime_dir

    project = lookup_project(project_id)
    if project is None:
        return {"ok": False, "error": "项目不存在"}
    members = list_services_by_project(project_id)
    cancelled_timers = 0
    for m in members:
        sid = m["service_id"]
        db = resolve_runtime_dir(sid) / "data" / "state.db"
        if db.exists():
            try:
                conn = _sq.connect(str(db), timeout=3.0)
                try:
                    cur = conn.execute(
                        "DELETE FROM events WHERE kind = 'timer'"
                        " AND consumed_at IS NULL"
                    )
                    cancelled_timers += cur.rowcount
                    conn.commit()
                finally:
                    conn.close()
            except _sq.Error:
                pass
        set_service_status(sid, SERVICE_STATUS_ARCHIVED)
    update_project_fields(project_id, status=SERVICE_STATUS_ARCHIVED)
    logger.info(
        "PROJECT_CLOSED project=%s members=%d cancelled_timers=%d reason=%s",
        project_id[:12], len(members), cancelled_timers, reason[:40],
    )
    return {"ok": True, "members": len(members), "cancelled_timers": cancelled_timers}


PROJECT_STATUS_SUSPENDED = "suspended"


def suspend_project(project_id: str, *, reason: str = "", actor: str = "") -> dict:
    """挂起项目：我们能做的都结束了、阻塞在客户侧。

    与 close（结束）不同——挂起可复活：取消全员未来闹钟（自转根源），
    项目标记 suspended；客户下次发言自动恢复。记忆产物全保留。
    """
    from infrastructure.persistence.services_registry import (
        list_services_by_project, lookup_project, update_project_fields,
    )
    import sqlite3 as _sq

    from infrastructure.config import resolve_runtime_dir

    project = lookup_project(project_id)
    if project is None:
        return {"ok": False, "error": "项目不存在"}
    if project.get("status") != "active":
        return {"ok": False, "error": f"项目状态 {project.get('status')}，仅活跃项目可挂起"}
    members = list_services_by_project(project_id)
    cancelled = 0
    for m in members:
        db = resolve_runtime_dir(m["service_id"]) / "data" / "state.db"
        if db.exists():
            try:
                conn = _sq.connect(str(db), timeout=3.0)
                try:
                    cur = conn.execute(
                        "DELETE FROM events WHERE kind = 'timer' AND consumed_at IS NULL"
                    )
                    cancelled += cur.rowcount
                    conn.commit()
                finally:
                    conn.close()
            except _sq.Error:
                pass
    update_project_fields(project_id, status=PROJECT_STATUS_SUSPENDED)
    logger.info("PROJECT_SUSPENDED project=%s members=%d cancelled_timers=%d actor=%s reason=%s",
                project_id[:12], len(members), cancelled, actor[:12] or "system", reason[:60])
    return {"ok": True, "members": len(members), "cancelled_timers": cancelled,
            "note": "项目已挂起：闹钟已取消，等待客户回来自动恢复"}


def resume_project(project_id: str, *, reason: str = "") -> dict:
    """恢复挂起的项目（客户回来自动触发，也可显式调用）。"""
    from infrastructure.persistence.services_registry import (
        lookup_project, update_project_fields,
    )

    project = lookup_project(project_id)
    if project is None:
        return {"ok": False, "error": "项目不存在"}
    if project.get("status") != PROJECT_STATUS_SUSPENDED:
        return {"ok": False, "error": f"项目状态 {project.get('status')}，仅挂起项目可恢复"}
    update_project_fields(project_id, status="active")
    logger.info("PROJECT_RESUMED project=%s reason=%s", project_id[:12], reason[:60])
    return {"ok": True}


def last_external_activity(project_id: str) -> float:
    """项目最近一次真实事件入队时间（客户/群/任务/催办）。

    全员信箱取非 timer 事件的最大 created_at（ISO）。判"项目是否还活着"。
    系统自身发的治理信号（project_stall/project_quiet）不算真实事件——
    否则信号本身会刷新静默时钟，PM 决定"再等等"后会被反复催。
    """
    import sqlite3 as _sq
    from datetime import datetime as _dt

    from infrastructure.config import resolve_runtime_dir
    from infrastructure.persistence.services_registry import list_services_by_project

    best = 0.0
    for m in list_services_by_project(project_id):
        db = resolve_runtime_dir(m["service_id"]) / "data" / "state.db"
        if not db.exists():
            continue
        try:
            conn = _sq.connect(str(db), timeout=3.0)
            try:
                row = conn.execute(
                    "SELECT created_at FROM events"
                    " WHERE kind NOT IN ('timer', 'project_stall', 'project_quiet')"
                    " ORDER BY event_id DESC LIMIT 1"
                ).fetchone()
            finally:
                conn.close()
            if row and row[0]:
                try:
                    ts = _dt.fromisoformat(str(row[0])).timestamp()
                    best = max(best, ts)
                except (TypeError, ValueError):
                    try:
                        best = max(best, float(row[0]))
                    except (TypeError, ValueError):
                        pass
        except _sq.Error:
            continue
    return best


def last_customer_activity(project_id: str) -> float:
    from infrastructure.persistence.services_registry import lookup_project
    """项目里客户最后一次真实发言的时间戳（PM 私库 customer 窗口最后一条入站）。

    无记录返回 0。这是"客户是否还理这个项目"的唯一判据——
    不看 agent 闹钟/群聊（那些是自转，不是客户意图）。
    """
    import sqlite3 as _sq

    from infrastructure.config import resolve_runtime_dir

    project = lookup_project(project_id)
    if project is None:
        return 0.0
    pm = project.get("pm_id") or ""
    if not pm:
        return 0.0
    db = resolve_runtime_dir(pm) / "data" / "messages.db"
    if not db.exists():
        return 0.0
    try:
        conn = _sq.connect(str(db), timeout=3.0)
        try:
            row = conn.execute(
                "SELECT ts FROM messages WHERE chat_id LIKE 'customer:%'"
                " AND direction = 'in' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        if not (row and row[0]):
            return 0.0
        try:  # ts 为 ISO 字符串（含 +08:00）；兼容历史数值型
            from datetime import datetime as _dt

            return _dt.fromisoformat(str(row[0])).timestamp()
        except (TypeError, ValueError):
            try:
                return float(row[0])
            except (TypeError, ValueError):
                return 0.0
    except _sq.Error:
        return 0.0
