"""health_state auto-recover 恢复逻辑测试。

BUG 背景(2026-06-29 修复)：原 `_read_instance_runtime_state` 的 health 判定
只看"DB 里是否有 severity=error 的 flow_event"，导致实例一旦撞过 3 次失败
写了 critical event、即便后来已恢复正常产出 assistant turn，health 也永久
stuck 在 error（前端红灯钉死）。

修复后语义（与 system_routes.py docstring :218-219 一致）：任意一次"最近
成功 assistant turn 比 critical event 更新"= 模型已恢复 → ok。

覆盖 4 个核心场景：
  a) critical event + 之后有更晚的成功 assistant turn → ok（已恢复）【关键】
  b) critical event + 再无成功 assistant turn → error（zero 现状）
  c) critical event + 之后只有失败的 assistant turn → error
  d) 无 critical event + 最近 turn 无 error → ok

测试不依赖真实例 DB，用 tmp 目录 + monkeypatch get_project_root 隔离。
"""
from __future__ import annotations

import datetime as _dt
import sqlite3

import pytest

from application.api import system_routes
from application.api.system_routes import _parse_iso_to_epoch, _read_instance_runtime_state
from infrastructure import config as infra_config

# epoch 参考值（动态算：critical event 的真实 epoch + 前/后偏移）
# 不硬编码常数以免常数算错（教训见上一版测试）。
CRITICAL_AT_EPOCH = _dt.datetime.fromisoformat("2026-06-27T14:57:01+00:00").timestamp()
EVT_0627_1457 = "2026-06-27T14:57:01+00:00"          # 对应 CRITICAL_AT_EPOCH
TS_BEFORE = CRITICAL_AT_EPOCH - 600.0                # critical 前 10 分钟
TS_AFTER = CRITICAL_AT_EPOCH + 600.0                 # critical 后 10 分钟


# ── _parse_iso_to_epoch helper 单元测试 ────────────────────────────────────


def test_parse_iso_to_epoch_valid_with_offset():
    """带时区偏移的 ISO → 正确 epoch。"""
    # 用动态计算而非硬编码常数（避免常数算错导致假绿）
    from datetime import datetime

    expected = datetime.fromisoformat("2026-06-27T14:57:01+00:00").timestamp()
    assert _parse_iso_to_epoch("2026-06-27T14:57:01+00:00") == pytest.approx(expected)


def test_parse_iso_to_epoch_valid_with_z():
    """Z 后缀（UTC）→ 正确 epoch，与 +00:00 一致。"""
    assert _parse_iso_to_epoch("2026-06-27T14:57:01Z") == pytest.approx(
        _parse_iso_to_epoch("2026-06-27T14:57:01+00:00")
    )


def test_parse_iso_to_epoch_naive_assumes_utc():
    """无时区的 naive ISO → 当 UTC 处理（与 +00:00 等价，不报错）。"""
    assert _parse_iso_to_epoch("2026-06-27T14:57:01") == pytest.approx(
        _parse_iso_to_epoch("2026-06-27T14:57:01+00:00")
    )


def test_parse_iso_to_epoch_empty_returns_neg_inf():
    """空串 → -inf（让"无法确定 critical 时间"保守走未恢复）。"""
    assert _parse_iso_to_epoch("") == float("-inf")
    assert _parse_iso_to_epoch("   ") == float("-inf")


def test_parse_iso_to_epoch_garbage_returns_neg_inf():
    """非法格式 → -inf（fail-safe）。"""
    assert _parse_iso_to_epoch("not-a-date") == float("-inf")
    assert _parse_iso_to_epoch("garbage-123") == float("-inf")


# ── _read_instance_runtime_state 恢复判定集成测试 ──────────────────────────


@pytest.fixture
def fake_instance_db(tmp_path, monkeypatch):
    """构造一个空的临时实例目录，runtime_log.db + state.db 都建好。

    monkeypatch get_project_root → tmp_path，让 _read_instance_runtime_state
    去读 tmp_path/apps/{iid}/data/... 而不是真实例。

    返回一个 helper，测试用它在两个 DB 里注入 turn / flow_event 行。
    """
    iid = "test-inst-health-recovery"
    data_dir = tmp_path / "apps" / iid / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(infra_config, "get_project_root", lambda: tmp_path)
    # system_routes 里是 `from infrastructure.config import get_project_root`，
    # 已经在模块顶部 bind 成本地名；patch 模块属性即可（_read_instance_runtime_state
    # 用的是 system_routes.get_project_root 这个本地引用）。
    monkeypatch.setattr(system_routes, "get_project_root", lambda: tmp_path)

    runtime_db = data_dir / "runtime_log.db"
    state_db = data_dir / "state.db"

    # 建 turn / wake 表（schema 与 runtime_log.py 一致）
    conn = sqlite3.connect(str(runtime_db))
    conn.executescript(
        """
        CREATE TABLE wake (id INTEGER PRIMARY KEY, ended_at REAL);
        CREATE TABLE turn (
            id INTEGER PRIMARY KEY, wake_id INTEGER, wake_seq INTEGER,
            llm_call_seq INTEGER, position_in_call INTEGER DEFAULT 0,
            role TEXT, content TEXT, error TEXT, timestamp REAL NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()

    # 建 flow_event_log_events 表（schema 与生产一致：timestamp TEXT + severity + summary）
    conn = sqlite3.connect(str(state_db))
    conn.executescript(
        """
        CREATE TABLE flow_event_log_events (
            id INTEGER PRIMARY KEY, timestamp TEXT, severity TEXT, summary TEXT
        );
        CREATE TABLE vitals (energy REAL);
        CREATE TABLE affairs (status TEXT, updated_at TEXT);
        """
    )
    conn.commit()
    conn.close()

    class _Helper:
        def add_turn(self, role: str, error: str | None, ts: float) -> None:
            conn = sqlite3.connect(str(runtime_db))
            conn.execute(
                "INSERT INTO turn (role, error, timestamp) VALUES (?, ?, ?)",
                (role, error, ts),
            )
            conn.commit()
            conn.close()

        def add_critical_event(self, iso_ts: str, summary: str = "GLM 持续失败") -> None:
            conn = sqlite3.connect(str(state_db))
            conn.execute(
                "INSERT INTO flow_event_log_events (timestamp, severity, summary) "
                "VALUES (?, 'error', ?)",
                (iso_ts, summary),
            )
            conn.commit()
            conn.close()

        def read_health(self) -> tuple[str, str]:
            # _read_instance_runtime_state 返回 (energy, runtime, process, health, reason)
            _e, _r, _p, health, reason = _read_instance_runtime_state(iid, active=True)
            return health, reason

    return _Helper()


def test_recovered_after_critical_plus_successful_assistant(fake_instance_db):
    """场景 a【核心】：critical event + 之后有更晚的成功 assistant turn → ok。

    这是 alpha/贝塔的真实情况：今天有成功 turn 覆盖了 06-27 的 critical 故障。
    """
    fake_instance_db.add_critical_event(EVT_0627_1457, summary="429 quota")
    # critical 之后产出了成功的 assistant turn → 应恢复
    fake_instance_db.add_turn(role="assistant", error=None, ts=TS_AFTER)
    health, reason = fake_instance_db.read_health()
    assert health == "ok", f"应有成功 turn 覆盖 critical → ok，实际 {health} / {reason}"
    assert "已恢复" in reason


def test_not_recovered_critical_without_successful_assistant(fake_instance_db):
    """场景 b：critical event + 再无成功 assistant turn → error（zero 现状）。"""
    fake_instance_db.add_critical_event(EVT_0627_1457, summary="429 quota")
    # critical 之前有过成功 turn（属历史），之后再无 → 仍 error
    fake_instance_db.add_turn(role="assistant", error=None, ts=TS_BEFORE)
    health, reason = fake_instance_db.read_health()
    assert health == "error", f"故障未被覆盖 → error，实际 {health} / {reason}"
    assert "事件流严重错误" in reason


def test_not_recovered_critical_then_failed_assistant(fake_instance_db):
    """场景 c：critical event + 之后只有失败的 assistant turn → error。"""
    fake_instance_db.add_critical_event(EVT_0627_1457)
    # critical 之后的 assistant turn 是失败的（有 error）→ 不算恢复
    fake_instance_db.add_turn(role="assistant", error="429 Too Many Requests", ts=TS_AFTER)
    health, _reason = fake_instance_db.read_health()
    assert health == "error", f"最近 assistant 是失败的 → error，实际 {health}"


def test_ok_without_critical_event(fake_instance_db):
    """场景 d：无 critical event + 最近 turn 无 error → ok（健康基线）。"""
    fake_instance_db.add_turn(role="assistant", error=None, ts=TS_AFTER)
    health, reason = fake_instance_db.read_health()
    assert health == "ok", f"无故障 → ok，实际 {health} / {reason}"


def test_critical_with_only_user_turns_after_not_recovered(fake_instance_db):
    """边界：critical 之后只有 user turn（非 assistant）成功 → 仍 error。

    user turn 成功不代表"模型能工作"（user turn 是输入，不是产出），
    只有 assistant turn 成功才算恢复。这是恢复判定的严谨性。
    """
    fake_instance_db.add_critical_event(EVT_0627_1457)
    fake_instance_db.add_turn(role="user", error=None, ts=TS_AFTER)
    health, _reason = fake_instance_db.read_health()
    assert health == "error", "仅 user turn 成功不应算恢复，需 assistant turn"
