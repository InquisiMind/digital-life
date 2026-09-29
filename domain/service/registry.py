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
) -> dict:
    """注册服务并落目录骨架，返回服务行。

    新建服务要指定的四件事（设计文档 6.1）：基于哪个定义（此处）、属于哪个
    项目（project_id，可空=未挂项目）、初始上下文与对客身份（上层职责，
    刀 3/4 管理面接入；此处先落注册表字段）。
    """
    validate_agent_def(agent_def_id)
    svc = services_registry.create_service(
        agent_def_id,
        service_id=service_id,
        project_id=project_id,
        display_name=display_name,
        capabilities=capabilities,
        subscriptions=subscriptions,
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
