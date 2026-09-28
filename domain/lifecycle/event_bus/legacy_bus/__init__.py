"""LegacyEventBus — 基于 SQLite 的事件持久化存储层。

这是整个事件系统的最底层——所有事件的 CRUD 操作最终都通过这里操作 events 表。

设计原则：
  - 存储、时钟、预测器通过依赖注入传入，与 Hermes 运行时解耦
  - events 表是 Hermes 时代的遗产表结构，通过 ensure_columns() 扩展了 L4 需要的列
  - 多实例隔离通过 channel 列 + LIKE 前缀匹配实现

events 表结构（L4 扩展后）：
  event_id              INTEGER PRIMARY KEY  — 事件唯一 ID
  channel               TEXT                  — 实例隔离通道（instance:{uuid} 或 instance:{uuid}/gateway:lark:user）
  kind                  TEXT                  — 事件类型标识（message、timer 等）
  payload               TEXT (JSON)           — 事件携带数据
  created_at            TEXT (ISO8601)        — 创建时间
  fire_at               TEXT (ISO8601)        — 定时触发时间（NULL = 立即可触发）
  consumed_at           TEXT (ISO8601)        — 消费时间（NULL = 未消费）
  consumed_by_session_id TEXT                 — 消费该事件的会话 ID（用于日历聚合展示）
  target_affair_id      TEXT                  — 目标事务 ID

关键 SQL 查询模式：
  pop_due_events:  WHERE consumed_at IS NULL AND (fire_at IS NULL OR fire_at <= now) AND channel LIKE 'instance:X%'
  consume_event:   UPDATE events SET consumed_at=now, consumed_by_session_id=sid WHERE event_id=? AND consumed_at IS NULL
  list_recent:     WHERE created_at >= since AND channel LIKE ... ORDER BY event_id DESC
"""

from __future__ import annotations

import json
from infrastructure.persistence import sqlite
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Set


ConnectionFactory = Callable[[], object]
InitDatabase = Callable[[], None]
NowIso = Callable[[], str]
NowDateTime = Callable[[], datetime]
ThresholdPredictor = Callable[..., List[Dict]]


def _channel_clause(prefix: str | None) -> tuple[str, list]:
    """构建 channel 前缀过滤的 SQL 子句和参数。

    多实例隔离的核心机制：
      事件通过 channel 列区分所属实例。channel 格式为 instance:{uuid} 或 instance:{uuid}/subchannel。
      用 LIKE 'instance:{uuid}%' 匹配该实例的所有事件。
      同时兼容没有 channel 的旧数据（channel IS NULL）。

    示例：
      prefix = "instance:abc123" → (channel LIKE 'instance:abc123%' OR channel IS NULL)
      prefix = None → (1=1) 不过滤
    """
    if prefix:
        return "(channel LIKE ? OR channel IS NULL)", [f"{prefix}%"]
    return "(1=1)", []


class LegacyEventBus:
    """事件总线 — 兼容旧 Hermes lifecycle schema 的 SQLite 事件存储。

    所有 CRUD 操作通过注入的 connection_factory、init_database、now_iso、now_dt 实现。
    events 事件表在 Hermes 运行时中，L4 通过 ensure_columns() 扩展必要列。

    核心操作流程：
      emit_event  → INSERT INTO events (channel, payload, created_at, kind, fire_at)
      pop_due_events → SELECT * WHERE consumed_at IS NULL AND fire_at <= now
      consume_event  → UPDATE events SET consumed_at=now WHERE event_id=?
      list_recent    → SELECT * WHERE created_at >= since ORDER BY event_id DESC
      cancel_pending → UPDATE events SET consumed_at=now WHERE fire_at IS NOT NULL AND fire_at > now
    """

    def __init__(
        self,
        *,
        connection_factory: ConnectionFactory,
        init_database: InitDatabase,
        now_iso: NowIso,
        now_dt: NowDateTime,
    ) -> None:
        self._connection_factory = connection_factory
        self._init_database = init_database
        self._now_iso = now_iso
        self._now_dt = now_dt

    def ensure_columns(self) -> None:
        """幂等扩展 events 表，添加 L4 需要的列。

        新增列：
          fire_at — 定时触发时间（支持延迟事件）
          kind — 事件类型标识（与 channel 分离）
          consumed_by_session_id — 消费会话 ID（用于日历展示）
        """
        self._init_database()
        with self._connection_factory() as connection:
            cols = {row["name"] for row in connection.execute("PRAGMA table_info(events)").fetchall()}
            if "fire_at" not in cols:
                connection.execute("ALTER TABLE events ADD COLUMN fire_at TEXT")
            if "kind" not in cols:
                connection.execute("ALTER TABLE events ADD COLUMN kind TEXT")
            if "consumed_by_session_id" not in cols:
                connection.execute("ALTER TABLE events ADD COLUMN consumed_by_session_id TEXT")
            if "resurrect_count" not in cols:
                connection.execute(
                    "ALTER TABLE events ADD COLUMN resurrect_count INTEGER DEFAULT 0"
                )

    def emit_event(
        self,
        kind: str,
        payload: Optional[Dict] = None,
        fire_at: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> int:
        """发布或调度一个事件到 events 表。

        Args:
            kind: 事件类型标识（如 message、timer）
            payload: 事件携带数据（JSON 序列化后存入 payload 列）
            fire_at: 定时触发时间（ISO8601），NULL 表示立即可触发
            channel: 实例通道（如 instance:{uuid}/gateway:lark:user）

        Returns:
            新插入事件的 event_id

        事件在 DB 中的初始状态：consumed_at=NULL（未消费），cron tick 的
        pop_due_events() 会将其捞出。
        """
        self.ensure_columns()
        payload = payload or {}
        with self._connection_factory() as connection:
            cursor = connection.execute(
                "INSERT INTO events (channel, payload, created_at, kind, fire_at) VALUES (?, ?, ?, ?, ?)",
                (channel or kind, json.dumps(payload, ensure_ascii=False), self._now_iso(), kind, fire_at),
            )
            return cursor.lastrowid

    def pop_due_events(self, limit: int = 50, channel_prefix: str | None = None) -> List[Dict]:
        """获取到期未消费的事件（只读不消费）。

        这是事件消费流程的核心查询——cron tick 用它检查是否有事件需要唤醒，
        sense_event_queue 用它展示事件列表。

        查询条件：
          1. consumed_at IS NULL（未消费）
          2. fire_at IS NULL OR fire_at <= now（已到期）
          3. channel LIKE prefix%（实例隔离）
          4. ORDER BY event_id ASC LIMIT N（按插入顺序）

        注意：pop 这个词有误导，这里只是 SELECT 不会 UPDATE。
        实际的消费操作需要单独调用 consume_event()。
        """
        self.ensure_columns()
        now = self._now_iso()
        clause, params = _channel_clause(channel_prefix)
        with self._connection_factory() as connection:
            rows = connection.execute(
                f"SELECT * FROM events WHERE consumed_at IS NULL "
                f"AND (fire_at IS NULL OR fire_at <= ?) "
                f"AND {clause} "
                f"ORDER BY event_id ASC LIMIT ?",
                [now] + params + [limit],
            ).fetchall()
        return [self.row_to_dict(row) for row in rows]

    def peek_unconsumed_event(self, event_id: int) -> Optional[Dict]:
        """只读查询单条事件，仅当未消费时返回（信号池幽灵过滤用）。

        与 pop_due_events 不同：不检查 fire_at / channel——调用方已持有
        事件本体，只缺一个"它是否仍待处理"的真值判断。走 bus 自己的
        connection_factory（与 emit/consume 同源），不引入第二套路径解析。
        """
        with self._connection_factory() as connection:
            row = connection.execute(
                "SELECT * FROM events WHERE event_id = ? AND consumed_at IS NULL",
                (event_id,),
            ).fetchone()
        return self.row_to_dict(row) if row is not None else None

    def list_recent_events(
        self,
        hours: float = 6.0,
        kinds: Optional[Set[str]] = None,
        include_consumed: bool = True,
        limit: int = 100,
        channel_prefix: str | None = None,
    ) -> List[Dict]:
        """列出近期事件（含已消费），用于上下文/sense 工具。"""
        self.ensure_columns()
        since = (self._now_dt() - timedelta(hours=hours)).isoformat(timespec="seconds")
        clause, params = _channel_clause(channel_prefix)
        query = f"SELECT * FROM events WHERE created_at >= ?"
        query_params: list = [since]
        if not include_consumed:
            query += " AND consumed_at IS NULL"
        query += f" AND {clause} ORDER BY event_id DESC LIMIT ?"
        query_params += params + [limit]
        with self._connection_factory() as connection:
            rows = connection.execute(query, query_params).fetchall()
        events = [self.row_to_dict(row) for row in rows]
        if kinds:
            events = [event for event in events if event["kind"] in kinds]
        return events

    def consume_event(self, event_id: int, target_affair_id: Optional[str] = None, session_id: Optional[str] = None) -> bool:
        """标记事件为已消费 — 事件生命周期的终点（幂等，首消费记录不可覆盖）。

        写入 consumed_at（当前时间）和 consumed_by_session_id（消费会话）。
        consumed_by_session_id 被 calendar() 用于聚合展示过往会话。

        UPDATE 带 ``consumed_at IS NULL`` 守卫：已消费的事件不被重复消费覆盖
        （0922 zero #1064 双投：发送关卡 check_before_send 补消费时把 17:43 的
        真实首消费记录覆盖成 18:26/另一会话，calendar 聚合错位 + 排障被误导）。
        与 consume_events_by_kind 的守卫对齐。重新激活仍走 unconsume_events。

        Returns:
            True = 本次写入消费记录；False = 事件已消费（no-op，记录保持首消费值）。
        """
        with self._connection_factory() as connection:
            cursor = connection.execute(
                "UPDATE events SET consumed_at = ?, target_affair_id = ?, consumed_by_session_id = ? "
                "WHERE event_id = ? AND consumed_at IS NULL",
                (self._now_iso(), target_affair_id, session_id, event_id),
            )
            return bool(getattr(cursor, "rowcount", 1))

    def consume_events_by_kind(self, kind: str, session_id: Optional[str] = None, channel_prefix: str | None = None) -> int:
        """按类型批量消费到期未消费事件。"""
        self.ensure_columns()
        now = self._now_iso()
        clause, params = _channel_clause(channel_prefix)
        query = f"""UPDATE events SET consumed_at = ?, consumed_by_session_id = ?
                   WHERE consumed_at IS NULL AND kind = ?
                   AND (fire_at IS NULL OR fire_at <= ?)
                   AND {clause}"""
        with self._connection_factory() as connection:
            cursor = connection.execute(query, [now, session_id, kind, now] + params)
            return cursor.rowcount

    def unconsume_events(self, event_ids: list[int], channel_prefix: str | None = None) -> int:
        """回退事件消费——将已消费事件恢复为未消费状态。"""
        if not event_ids:
            return 0
        clause, params = _channel_clause(channel_prefix)
        placeholders = ",".join("?" for _ in event_ids)
        query = f"UPDATE events SET consumed_at = NULL, consumed_by_session_id = NULL WHERE event_id IN ({placeholders}) AND {clause}"
        with self._connection_factory() as connection:
            cursor = connection.execute(query, event_ids + params)
            return cursor.rowcount

    def delay_pending_events(
        self,
        event_ids: list[int],
        minutes: float,
        *,
        max_minutes: float = 60.0,
        channel_prefix: str | None = None,
    ) -> int:
        """事件消费失败后的「自我指数退避」。

        设计意图：失败重试不是闹钟的事，是事件自己的事。闹钟只负责「未来某个
        真实时间点要做某件具体的事」（作息、回复等待、deadline、agent 主动
        rest(until=...)），永远不为 failure recovery 设闹钟。事件被消费失败 →
        自己推迟下次露面时间，靠 pop_due_events 的 `fire_at <= now` 守卫让自
        己在退避窗口内对 cron 不可见。

        行为：
          - 将给定事件的 fire_at 推到 now + minutes
          - resurrect_count 自增 1（首次失败 1，再次 2，...）
          - 下次推荐的退避分钟数 = min(max_minutes, base * 2^resurrect_count)
            （调用方自己算 base，通常 base=2 → 2/4/8/16/32/60/60…）

        minutes 为本次应施加的退避分钟数，由调用方依据每个事件的
        resurrect_count 算出后传入，本函数不重复计算（保持纯 setter 语义）。
        """
        if not event_ids:
            return 0
        self.ensure_columns()
        now = self._now_iso()
        from datetime import timedelta
        fire_at = (self._now_dt() + timedelta(minutes=minutes)).isoformat(timespec="seconds")
        clause, params = _channel_clause(channel_prefix)
        placeholders = ",".join("?" for _ in event_ids)
        query = (
            f"UPDATE events SET consumed_at = NULL, consumed_by_session_id = NULL, "
            f"fire_at = ?, resurrect_count = resurrect_count + 1 "
            f"WHERE event_id IN ({placeholders}) AND {clause}"
        )
        with self._connection_factory() as connection:
            cursor = connection.execute(query, [fire_at] + event_ids + params)
            return cursor.rowcount

    def pop_events_by_kind(self, kind: str, limit: int = 10, session_id: Optional[str] = None, channel_prefix: str | None = None) -> List[Dict]:
        """按类型取出并消费到期事件——先 SELECT 再批量 UPDATE consumed_at。"""
        self.ensure_columns()
        now = self._now_iso()
        clause, params = _channel_clause(channel_prefix)
        with self._connection_factory() as connection:
            rows = connection.execute(
                f"SELECT * FROM events WHERE consumed_at IS NULL AND kind = ? "
                f"AND (fire_at IS NULL OR fire_at <= ?) "
                f"AND {clause} "
                f"ORDER BY event_id ASC LIMIT ?",
                [kind, now] + params + [limit],
            ).fetchall()
            events = [self.row_to_dict(row) for row in rows]
            if events:
                event_ids = [event["event_id"] for event in events]
                placeholders = ",".join("?" * len(event_ids))
                connection.execute(
                    f"UPDATE events SET consumed_at = ?, consumed_by_session_id = ? WHERE event_id IN ({placeholders})",
                    [now, session_id] + event_ids,
                )
            return events

    def count_pending_by_payload_key(
        self,
        kind: str,
        payload_key: str,
        payload_value: str,
        *,
        channel_prefix: str | None = None,
    ) -> int:
        """统计未消费的同源事件数。

        用于"emit 前去重"：如果队列里已经有一个同 kind + 同 payload_key=value
        的未消费事件，就不该再 emit。例如 task_reminder 按 task_id 作 key
        去重——同一待办的提醒只要队列里有 1 个没处理完，就不该再发第 2 个。

        payload 用 json_extract 操作（payload 列 JSON 文本）：
          json_extract(payload, '$.task_id') = ?
        """
        self.ensure_columns()
        clause, params = _channel_clause(channel_prefix)
        with self._connection_factory() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) as n FROM events "
                f"WHERE consumed_at IS NULL AND kind = ? "
                f"AND json_extract(payload, ?) = ? "
                f"AND {clause}",
                [kind, f"$.{payload_key}", payload_value] + params,
            ).fetchone()
            return int(row["n"]) if row else 0

    def cancel_pending_events(self, kind: Optional[str] = None, payload_filter: Optional[Dict] = None, channel_prefix: str | None = None) -> int:
        """取消未来的定时事件，可选地按 payload_filter 恢复不匹配的行。"""
        self.ensure_columns()
        now = self._now_iso()
        clause, extra_params = _channel_clause(channel_prefix)
        query = (
            "UPDATE events SET consumed_at = ? "
            "WHERE consumed_at IS NULL AND fire_at IS NOT NULL AND fire_at > ?"
        )
        params: list = [now, now]
        if kind:
            query += " AND kind = ?"
            params.append(kind)
        query += f" AND {clause}"
        params += extra_params

        with self._connection_factory() as connection:
            cursor = connection.execute(query, params)
            count = cursor.rowcount
            if count > 0 and payload_filter is not None and kind:
                rows = connection.execute(
                    "SELECT event_id, payload FROM events WHERE consumed_at IS NOT NULL "
                    "AND kind = ? ORDER BY event_id DESC LIMIT ?",
                    (kind, count),
                ).fetchall()
                for row in rows:
                    try:
                        payload = json.loads(row["payload"])
                        matched = all(payload.get(key) == value for key, value in payload_filter.items())
                    except Exception:
                        matched = False
                    if not matched:
                        connection.execute("UPDATE events SET consumed_at = NULL WHERE event_id = ?", (row["event_id"],))
            return count

    def schedule_vital_threshold_events(
        self,
        predictor: ThresholdPredictor,
        *,
        clear_previous: bool = True,
        horizon_hours: float = 72.0,
        segment_filter: Optional[Set[str]] = None,
    ) -> List[Dict]:
        """调度精力阈值穿越为未来事件。

        segment_filter: 可选的要监听的段位名称集合。为 None 时不筛选（调度所有预测穿越）。
        adapter 层应从 SEGMENTS 推导此集合。
        """
        self.ensure_columns()
        if clear_previous:
            with self._connection_factory() as connection:
                connection.execute("DELETE FROM events WHERE kind = 'vital_threshold'")

        crossings = predictor(horizon_hours=horizon_hours)
        scheduled = []
        for crossing in crossings:
            if segment_filter and crossing["to_seg"] not in segment_filter:
                continue
            event_id = self.emit_event(
                kind="vital_threshold",
                payload=crossing,
                fire_at=crossing["fire_at"],
            )
            crossing["event_id"] = event_id
            scheduled.append(crossing)
        return scheduled

    @staticmethod
    def row_to_dict(row: sqlite.Row) -> Dict:
        try:
            payload = json.loads(row["payload"]) if row["payload"] else {}
        except Exception:
            payload = {"raw": row["payload"]}
        keys = row.keys()
        return {
            "event_id": row["event_id"],
            "channel": row["channel"],
            "kind": row["kind"] if "kind" in keys else row["channel"],
            "payload": payload,
            "created_at": row["created_at"],
            "fire_at": row["fire_at"] if "fire_at" in keys else None,
            "consumed_at": row["consumed_at"],
            "target_affair_id": row["target_affair_id"],
            # resurrect_count 让失败回退路径能算出每事件独立指数退避（2^n min）
            "resurrect_count": row["resurrect_count"] if "resurrect_count" in keys else 0,
        }


__all__ = ["LegacyEventBus"]
