"""服务型 agent 总览聚合（供 system overview / 管理台服务视角共用）。

服务型运行体是与实例型并列的一等 agent 形态：本模块只做只读聚合
（def 目录扫描 + services_registry + 各服务 runtime/state/messages 库），
不认识任何具体业务；业务 enricher（项目名等）经 extension 扩展点补全。
失败一律降级为空/部分数据，不抛错——调用方（overview 主路径）不许被拖垮。
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger("digital_life.service.overview")


def svc_data_db(service_id: str, filename: str):
    """某服务的运行数据库路径（apps/{id}/data/{filename}）。"""
    from infrastructure.config import resolve_runtime_dir

    return resolve_runtime_dir(service_id) / "data" / filename


def service_stats(service_id: str) -> dict:
    """服务的运行统计：唤醒数/最近唤醒/待处理事件/消息数。"""
    stats = {"wakes": 0, "last_wake_at": None, "open_events": 0, "messages": 0}
    db = svc_data_db(service_id, "runtime_log.db")
    if db.exists():
        try:
            conn = sqlite3.connect(str(db), timeout=3.0)
            stats["wakes"] = conn.execute("SELECT COUNT(*) FROM wake").fetchone()[0]
            row = conn.execute(
                "SELECT started_at FROM wake ORDER BY wake_seq DESC LIMIT 1"
            ).fetchone()
            if row and row[0]:
                from datetime import datetime

                try:
                    stats["last_wake_at"] = datetime.fromtimestamp(float(row[0])).strftime(
                        "%m-%d %H:%M"
                    )
                except (TypeError, ValueError):
                    stats["last_wake_at"] = str(row[0])[:16]
            conn.close()
        except sqlite3.Error:
            pass
    db = svc_data_db(service_id, "state.db")
    if db.exists():
        try:
            conn = sqlite3.connect(str(db), timeout=3.0)
            stats["open_events"] = conn.execute(
                "SELECT COUNT(*) FROM events WHERE consumed_at IS NULL"
                " AND channel LIKE ?",
                (f"instance:{service_id}%",),
            ).fetchone()[0]
            conn.close()
        except sqlite3.Error:
            pass
    db = svc_data_db(service_id, "messages.db")
    if db.exists():
        try:
            conn = sqlite3.connect(str(db), timeout=3.0)
            stats["messages"] = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            conn.close()
        except sqlite3.Error:
            pass
    return stats


def collect_services_overview() -> dict:
    """服务型 agent 总览摘要（供 system overview 聚合）。

    三层结构：def 清单（含服务实例计数）→ active 服务行（含运行统计）
    → 计数汇总。def 目录扫描与 services_registry 均为只读，失败返回
    空摘要而非抛错——不拖垮 overview 主数据。
    """
    from infrastructure.persistence import services_registry

    # def 清单（runtime_kind: definition 的目录）
    def_rows: list[dict] = []
    try:
        import yaml as _yaml

        from infrastructure.config import get_project_root

        apps = get_project_root() / "apps"
        if apps.is_dir():
            for d in sorted(apps.iterdir()):
                cfg = d / "config" / "app.yaml"
                if not cfg.exists():
                    continue
                try:
                    data = _yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
                except Exception:
                    continue
                if str(data.get("runtime_kind") or "") == "definition":
                    def_rows.append(
                        {"def_id": d.name, "display_name": data.get("display_name") or d.name}
                    )
    except Exception:
        logger.exception("services overview: scan defs failed")

    # 服务实例（含运行统计）
    svc_rows: list[dict] = []
    try:
        for svc in services_registry.list_services():
            svc_rows.append(
                {
                    "service_id": svc["service_id"],
                    "agent_def_id": svc["agent_def_id"],
                    "role": svc.get("display_name") or "",
                    "project_id": svc.get("project_id") or "",
                    "project_name": "",
                    "status": svc["status"],
                    **service_stats(svc["service_id"]),
                }
            )
    except Exception:
        logger.exception("services overview: list services failed")

    by_def: dict[str, dict] = {}
    for s in svc_rows:
        g = by_def.setdefault(
            s["agent_def_id"], {"service_count": 0, "active_count": 0}
        )
        g["service_count"] += 1
        if s["status"] == "active":
            g["active_count"] += 1
    for d in def_rows:
        d.update(by_def.get(d["def_id"], {"service_count": 0, "active_count": 0}))

    # 业务 enricher（项目名等）经扩展点补全——基座不知道"项目"是什么
    try:
        from infrastructure import extension

        extension.enrich_service_rows(svc_rows)
    except Exception:
        pass
    active = [s for s in svc_rows if s["status"] == "active"]
    last_wake = max(
        (s["last_wake_at"] for s in svc_rows if s.get("last_wake_at")), default=None
    )
    return {
        "defs": def_rows,
        "def_count": len(def_rows),
        "services": active,
        "service_count": len(active),
        "open_events_total": sum(s.get("open_events") or 0 for s in active),
        "last_activity": last_wake,
    }
