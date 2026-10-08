"""服务运行体状态推导：working / resting / idle（+积压告警）。

数据源即审计真相（该服务私有库），不另建状态机：
- 最近一次 wake 未结束 → working（附唤醒原因与已耗时）
- 存在未到期的 timer 事件（rest 设的闹钟）→ resting（附醒来的时间）
- 否则 → idle（待命，等待新事件唤醒）
- 附加告警：未消费事件积压 / 指派待办未动 → detail 里标注
"""

import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from infrastructure.config import resolve_runtime_dir


def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def service_runtime_status(service_id: str) -> dict:
    """服务实时状态。异常一律回 idle（状态展示不抛错）。"""
    try:
        return _status(service_id) or {"state": "idle", "detail": ""}
    except Exception:
        return {"state": "idle", "detail": ""}


def _status(service_id: str) -> Optional[dict]:
    d = resolve_runtime_dir(service_id) / "data"
    now = _now_ts()

    # 1) 最近一次 wake 是否仍在进行 → 结合最后 turn 还原"正在干什么"
    rdb = d / "runtime_log.db"
    if rdb.exists():
        conn = sqlite3.connect(str(rdb), timeout=3.0)
        try:
            row = conn.execute(
                "SELECT started_at, ended_at, meta_json FROM wake"
                " ORDER BY wake_seq DESC LIMIT 1"
            ).fetchone()
            turn = conn.execute(
                "SELECT role, tool_name, tool_calls, timestamp FROM turn"
                " ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        if row and not row[1]:  # wake 进行中
            action, act_at = "处理中", None
            if turn:
                role, tool_name, tool_calls, ts = turn
                act_at = ts
                if role == "assistant":
                    try:
                        calls = json.loads(tool_calls or "[]")
                        nm = calls[0].get("function", {}).get("name", "") if calls else ""
                        action = f"调用 {nm}" if nm else "组织回复"
                    except (ValueError, IndexError):
                        action = "组织回复"
                elif role == "tool":
                    action = f"消化 {tool_name} 的结果、思考中" if tool_name else "思考中"
                else:
                    action = "思考中"
            try:
                elapsed = max(0, now - (act_at or float(row[0])))
            except (TypeError, ValueError):
                elapsed = 0
            mm, ss = int(elapsed // 60), int(elapsed % 60)
            if elapsed > 120:  # 单环节卡住超 2 分钟 → 阻塞
                return {"state": "stuck",
                        "detail": f"{action} 已 {mm}:{ss:02d}，疑似卡住"}
            return {"state": "working",
                        "detail": f"{action} · {mm}:{ss:02d}"}

    # 2) 未到期的 timer（rest 闹钟）→ 休息中
    sdb = d / "state.db"
    if sdb.exists():
        conn = sqlite3.connect(str(sdb), timeout=3.0)
        try:
            rows = conn.execute(
                "SELECT fire_at, payload FROM events"
                " WHERE kind = 'timer' AND consumed_at IS NULL AND fire_at IS NOT NULL"
                " ORDER BY fire_at LIMIT 1"
            ).fetchall()
            open_ev = conn.execute(
                "SELECT COUNT(*) FROM events WHERE consumed_at IS NULL"
            ).fetchone()[0]
        finally:
            conn.close()
        if rows:
            try:
                fire = datetime.fromisoformat(rows[0][0])
                if fire.tzinfo is None:
                    fire = fire.replace(tzinfo=timezone.utc)
                if fire.timestamp() > now:
                    until = fire.astimezone(timezone(offset=__import__("datetime").timedelta(hours=8)))
                    note = ""
                    try:
                        note = (json.loads(rows[0][1] or "{}").get("reason") or "")[:24]
                    except ValueError:
                        pass
                    return {"state": "resting",
                            "detail": f"休息至 {until.strftime('%H:%M')}{'· ' + note if note else ''}"}
            except ValueError:
                pass
        if open_ev:
            return {"state": "idle", "detail": f"待命（{open_ev} 个事件排队）"}

    return {"state": "idle", "detail": "待命"}
