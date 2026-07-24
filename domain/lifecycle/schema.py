"""Schema 集中入口 — 一次性建实例需要的所有 SQLite 表。

设计目的：
  各 domain 模块原本懒加载 `CREATE TABLE IF NOT EXISTS`，散落多处，
  新实例启动时不可靠（部分表只在被调用时才建）。
  此处统一调用所有模块的 schema 初始化函数，幂等可重复调用。

调用方：
  - digital-life init 实例初始化时
  - infrastructure/http/server.py:run_instance_gateway 进程启动时（兜底）

涉及的 DB：
  - state.db（主实例库，most tables）
  - 同一进程上下文中的 sessions.db / tasks.db（subsystems 各自管理）
  - 注意：本函数只保证 state.db 表齐全；其他 DB 由各 domain 自建
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)


class StateDbCorruptError(RuntimeError):
    """state.db 完整性自检失败时抛出。

    根因：本机 Mac 频繁睡眠 + 早期未启用 synchronous=FULL，反复出现
    ``database disk image is malformed``（6/29、7/3、7/7 三次复发）。一旦主库
    b-tree 损坏，所有 EMIT 写入都会失败但 handler 会把 ``event_id=-1`` 当成
    "FAILED-or-merged" 静默吞掉，导致实例长期植物人状态。该异常由启动入口
    ``init_all_schemas`` 抛出，由 supervisor 决定是否继续启动；不应被普通
    业务代码 catch。
    """


def _check_state_db_integrity(db_path: Path | None = None) -> None:
    """启动前对 state.db 跑 PRAGMA integrity_check。

    仅在文件已存在时执行；新实例（state.db 不存在）跳过——子模块会自动建库。
    若发现损坏：log ERROR 显眼告警 + 抛 ``StateDbCorruptError``。

    可选 ``db_path`` 参数：生产调用方传 None 走 ``get_instance_state_db_path``
    （基于实例 context）；测试可直接传 tmp_path。
    """
    if db_path is None:
        try:
            from infrastructure.config import get_instance_state_db_path
        except Exception as exc:  # pragma: no cover - defensive: config import 链故障
            logger.warning("integrity check skipped: %s", exc)
            return
        db_path = get_instance_state_db_path()

    if not db_path.exists():
        return  # 新实例，子模块 schema init 时会创建

    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            row = conn.execute("PRAGMA integrity_check;").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.error(
            "STATE_DB_INTEGRITY_FAIL path=%s reason=sqlite_error (%s) — "
            "instance may be corrupted; restore from backup before restarting",
            db_path,
            exc,
        )
        raise StateDbCorruptError(f"state.db unreadable at {db_path}: {exc}") from exc

    result = row[0] if row else "unknown"
    if result != "ok":
        logger.error(
            "STATE_DB_INTEGRITY_FAIL path=%s result=%r — EMIT/loader will fail; "
            "restore from backup before restarting",
            db_path,
            result,
        )
        raise StateDbCorruptError(f"state.db corrupted at {db_path}: {result}")


def init_all_schemas() -> None:
    """初始化当前实例上下文（ContextVar）对应 state.db 的全量表。

    必须在 set_current_instance_id / set_instance_context 设置好之后调用。
    若 state.db 不存在，各模块会自动创建（含父目录）。
    """
    # 启动时完整性自检：损坏则抛 StateDbCorruptError，避免像之前那样静默腐败一周。
    # 由调用方（supervisor / init script）决定 catch 后是停机还是带病启动。
    _check_state_db_integrity()

    failed: list[str] = []

    # 1) affairs / wait_intents / events / heartbeats / vitals / wallet / nurture_log / timers
    try:
        from domain.lifecycle.affairs.runtime import init_db as _affairs_init
        _affairs_init()
    except Exception as exc:
        failed.append(f"affairs: {exc}")
        logger.warning("schema init affairs failed: %s", exc)

    # 2) sessions / messages（SessionDB）
    try:
        from infrastructure.ai.session_db import SessionDB
        SessionDB()  # 内部按 ContextVar 解析 path
    except Exception as exc:
        failed.append(f"sessions: {exc}")
        logger.warning("schema init sessions failed: %s", exc)

    # 3) contacts / contact_ids（含 v1→v2 自动迁移）
    try:
        from domain.contacts import ensure_schema as _contacts_schema
        _contacts_schema()
    except Exception as exc:
        failed.append(f"contacts: {exc}")
        logger.warning("schema init contacts failed: %s", exc)

    # 4) conversation_log（sense_conversation 用）
    try:
        from domain.lifecycle.conversation_log import _ensure_table
        conn = _ensure_table()
        conn.close()
    except Exception as exc:
        failed.append(f"conversation_log: {exc}")
        logger.warning("schema init conversation_log failed: %s", exc)

    # 5) flow_event_logs / flow_event_log_events
    try:
        from infrastructure.config import get_instance_state_db_path
        from infrastructure.persistence.repositories.flow_event_log import (
            SQLiteFlowEventLogRepository,
        )
        SQLiteFlowEventLogRepository(db_path=get_instance_state_db_path())
    except Exception as exc:
        failed.append(f"flow_event_log: {exc}")
        logger.warning("schema init flow_event_log failed: %s", exc)

    # 6) alert_history（员工控制台告警）
    try:
        from application.console.alerts import AlertManager
        AlertManager()
    except Exception as exc:
        failed.append(f"alerts: {exc}")
        logger.warning("schema init alerts failed: %s", exc)

    # 7) tasks.py 子系统有自己的 tasks.db（lazy 在 get_db() 中建表）
    try:
        from domain.todos._infra import get_db as _tasks_get_db
        conn = _tasks_get_db()
        conn.close()
    except Exception as exc:
        failed.append(f"tasks: {exc}")
        logger.warning("schema init tasks failed: %s", exc)

    # 8) 新版分库：runtime_log / memory / vitals / workflow
    #    （每实例 4 个 .db，Schema 在 InstanceDB.__init__ 时自动建）
    try:
        from infrastructure.persistence.instance import get_instance_bundle
        bundle = get_instance_bundle()
        # Force schema creation by touching each connection. The InstanceDB
        # base class executes SCHEMA_SQL on init, so simply resolving the
        # bundle is enough. Bundle is memoized; later callers get the same.
        _ = (bundle.audit, bundle.memory, bundle.vitals, bundle.workflow)
    except Exception as exc:
        failed.append(f"instance_db: {exc}")
        logger.warning("schema init instance_db failed: %s", exc)

    # 9) budget_log（token 用量累加，预算闸门依赖）
    #    state.db 同库附加。LLM call 完成后 record 一笔，预算闸门读小时/日累计。
    try:
        from infrastructure.budget import get_token_tracker
        get_token_tracker()  # 构造时自动幂等建表
    except Exception as exc:
        failed.append(f"budget: {exc}")
        logger.warning("schema init budget failed: %s", exc)

    # 10) attachments（多模态附件 registry，state.db 同库附加）
    try:
        from infrastructure.persistence.instance.attachments import ensure_schema as _att_ensure
        from infrastructure.config import get_app_instance_id
        iid = get_app_instance_id() or ""
        if iid:
            _att_ensure(iid)
    except Exception as exc:
        failed.append(f"attachments: {exc}")
        logger.warning("schema init attachments failed: %s", exc)

    # 11) social_feed（社交接管消息持久化, state.db 同库）
    try:
        from domain.social.store import ensure_schema as _sf_ensure
        _sf_ensure()
    except Exception as exc:
        failed.append(f"social_feed: {exc}")
        logger.warning("schema init social_feed failed: %s", exc)

    if failed:
        logger.warning("init_all_schemas: %d failures: %s", len(failed), "; ".join(failed))
    else:
        logger.info("init_all_schemas: all subsystems initialized")

    # 注: personal_assistant 项目不再由 schema 常驻创建。
    # 解耦设计: 接管(OAuth 成功)→ 创建项目+待办; 取消接管 → 停项目+清待办。
    # 见 domain/social/bootstrap.py


def activate_personal_assistant(instance_id: str) -> None:
    """接管激活: 创建 personal_assistant 项目 + 日常待办。

    由 OAuth callback 调用(terraform_flamingo 之类)。幂等:
    项目已存在 → 不重复创建; 待办已存在 → 不重复创建。
    """
    if not instance_id:
        return
    import pathlib

    # 1. 创建项目目录 + project.yaml(如果不存在)
    project_dir = pathlib.Path("projects") / "personal_assistant"
    project_yaml = project_dir / "project.yaml"
    if not project_yaml.exists():
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / "memory").mkdir(exist_ok=True)
        (project_dir / "docs").mkdir(exist_ok=True)
        project_yaml.write_text(
            "project:\n"
            "  id: personal_assistant\n"
            "  name: 个人助理\n"
            "  description: >-\n"
            "    辅助用户的生活和工作。核心是想用户所想——跟进用户所有收到的消息,\n"
            "    将需要用户做的事处理掉或提醒。用 sense_social_feed 获取消息,\n"
            "    silent rule 默认不发言, 真正有 actionable 时建待办或提醒用户。\n"
            "  status: active\n"
            f"  manager: {instance_id}\n"
            "  goal:\n"
            "    statement: 辅助用户生活和工作——想用户所想, 处理能处理的事, 提醒需要提醒的。\n"
            "positions:\n"
            "  - id: real_human_assistant\n"
            "    name: 真人助理\n"
            "    responsibilities:\n"
            "      - 跟进用户所有飞书 IM 消息(用 sense_social_feed)\n"
            "      - 考虑哪些是用户要做的, 哪些需要提醒他\n"
            "      - 将需要用户做的事形成 assistance 类待办, 自己能做的直接做\n"
            "    assignees:\n"
            f"      - {instance_id}\n",
            encoding="utf-8",
        )
        logger.info("personal_assistant: project.yaml created")

    # 2. 创建日常待办(如果不存在)
    try:
        from domain.todos.crud import list_tasks, create_task
        existing = list_tasks(project_id="personal_assistant", status_filter=None)
        active_existing = [
            t for t in existing
            if (t.get("status", "") if isinstance(t, dict) else getattr(t, "status", "")) != "cancelled"
        ]
        if any("偶尔瞅一眼" in (t.get("title", "") if isinstance(t, dict) else getattr(t, "title", "")) for t in active_existing):
            return  # 已有活跃待办
        create_task(
            title="常驻: 偶尔瞅一眼用户新消息 + 想想能帮上什么",
            description=(
                "偶尔可以瞅一下是否用户有新的消息来了, 看一下是否有值得关注的——\n"
                "用户需要处理的、或者需要未来提醒他的。考虑一下如何帮助用户。\n\n"
                "用 sense_social_feed 获取最新的用户消息(飞书群, daemon 每 30min 拉取)。\n"
                "拿到消息后按 personal_assistant skill 的方法论执行:\n"
                "需要做的形成待办, 需要提醒的发 express_to_human, 没什么就跳过。\n\n"
                "不强制每次有产出。这条是你「持续关注用户」的常驻信号。"
            ),
            priority="medium",
            tags=["social", "assistance"],
            status="in_progress",
            source="project:personal_assistant",
            type="assistance",
        )
        logger.info("personal_assistant: seeded daily check todo for %s", instance_id[:8])
    except Exception as exc:
        logger.warning("personal_assistant: create todo failed: %s", exc)


def deactivate_personal_assistant(instance_id: str) -> None:
    """接管取消: 停项目 + 清待办。

    由 social/revoke 调用。项目 status 改 inactive, 待办全部 cancel。
    不删项目文件(保留历史记忆), 只让它从 todos 面板消失。
    """
    import pathlib
    # 1. 项目 status → inactive
    project_yaml = pathlib.Path("projects") / "personal_assistant" / "project.yaml"
    if project_yaml.exists():
        import yaml
        raw = yaml.safe_load(project_yaml.read_text(encoding="utf-8")) or {}
        proj = raw.get("project", {})
        proj["status"] = "inactive"
        project_yaml.write_text(yaml.dump(raw, allow_unicode=True), encoding="utf-8")
        logger.info("personal_assistant: project set to inactive")

    # 2. 待办全部 cancel
    try:
        from domain.todos.crud import list_tasks, update_task
        tasks = list_tasks(project_id="personal_assistant", status_filter=None)
        cancelled = 0
        for t in tasks:
            tid = t.get("id") if isinstance(t, dict) else getattr(t, "id", "")
            if tid:
                update_task(tid, status="cancelled")
                cancelled += 1
        logger.info("personal_assistant: cancelled %d todos", cancelled)
    except Exception as exc:
        logger.warning("personal_assistant: cancel todos failed: %s", exc)
