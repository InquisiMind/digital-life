"""账号级 LLM 429 熔断器（按 api_key 分区，跨进程共享）。

为什么需要它（独立于 budget_gate / token_tracker / per-instance 健康计数）：

  GLM 的 429 是**账号级**（按 API key 维度的 QPM/日配额）。多实例共用同一把
  key 时，账号额度被所有进程瓜分；但现有四套保护机制全部 per-instance：
    - budget_gate（token 用量闸门）→ 写在 apps/<id>/data/state.db，跨实例不可见
    - token_tracker（token 累加器）→ 同上
    - scheduler 健康失败计数（_consecutive_wake_failures）→ 进程内内存字典
    - agent._chat 的 429 单次退避（5/10/20s）→ 只对"这一次调用"有效

  后果：实例 A 撞 429 退避时，实例 B/C 毫无感知继续往同一把 key 上打，
  429 持续；真人消息走 events._wake_or_inject 即时唤醒路径又**完全不经
  budget_gate**，限流期间还在反复触发 _chat 撞 429 三次。账号级熔断就是
  补这个跨实例协调缺口：任一实例撞 429 → 同一把 key 的所有实例一起暂停。

设计（与用户 2026-06-26 对齐）：
  1. **按 api_key 分区**：共用同一把 key 的实例共吃一个熔断状态；不同 key
     互不影响。明文 key 不落库，只存 sha256 指纹（前 16 位足够区分，且避免
     在 dump/日志里泄露凭据）。
  2. **真人消息也挡**：熔断闸口优先级高于 budget_gate 的 HIGH_PRIORITY_KINDS
     穿透语义——429 说明账号额度真炸了，真人消息打进来也是 429。
  3. **尊重 Retry-After**：触发时从 429 响应头解析 Retry-After（秒 / HTTP-date），
     clamp 到合理区间作熔断时长。无头/非法用兜底。
  4. **自动过期恢复**：expires_at <= now 即视为恢复，读时自动删行清状态，
     不需要人工介入。

存储：根目录 data/circuit_breaker.db（跨实例共享，跟 global_todos.db 同级；
不能放 apps/*/ 里）。WAL 模式，单语句 upsert/读删，天然跨进程安全。
"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from infrastructure.persistence import sqlite as _sqlite

logger = logging.getLogger(__name__)


# ── 兜底退避区间（可被 env 覆盖）──────────────────────────────────────────
# 无 Retry-After 头时的默认熔断时长。5 分钟：够 GLM 账号级 QPM 窗口恢复，
# 又不至于让一次正常唤醒等太久。
DEFAULT_RETRY_AFTER_SEC = float(os.getenv("DIGITAL_LIFE_CB_DEFAULT_RETRY_AFTER", "300"))
# 下限：太短没意义（刚 trip 就恢复，又去撞 429），防恶意/异常的头。
MIN_RETRY_AFTER_SEC = float(os.getenv("DIGITAL_LIFE_CB_MIN_RETRY_AFTER", "10"))
# 上限：1 小时封顶，防止 GLM 返回离谱值（或 HTTP-date 解析出错）把系统卡死。
MAX_RETRY_AFTER_SEC = float(os.getenv("DIGITAL_LIFE_CB_MAX_RETRY_AFTER", "3600"))

# 指纹长度。sha256 前 16 位 hex = 64 bit，区分同一部署里几把 key 绰绰有余，
# 且明文不出本进程（只在内存里 sha256 一次，指纹落库）。
_FINGERPRINT_LEN = 16


# ── GLM 429 错误码分类（docs.bigmodel.cn/cn/api/api-code）──────────────────────
# 429 状态码本身无法区分"限流"与"欠费"，必须解析响应体 ``error.code``。
# 分两类，决定熔断时长策略：
#
#   1. 硬故障（充值前重试必败）：余额不足 / 套餐失效。等几分钟没用，必须人工
#      充值/续费。熔断时间拉到 MAX（默认 1h），避免到期重试又撞 429 反复刷新
#      expires_at（曾观察到的现象：expires_at 从 03:14:13 一路涨到 03:14:44）。
#   2. 软限流（窗口过后自愈）：QPM/并发超限、平台过载、周期用量上限。Retry-After
#      头通常已给合理窗口，保持现有 resolve_retry_after 逻辑。
#
# 以 code 数字为唯一判据——message 文案会变（实测 "余额不足或无可用资源包"
# 与官方现文案 "您的账户已欠费" 不一致），不能靠字符串匹配。

# 硬故障：等也没用，熔断拉满（充值/续费后到期前 clear() 手动解，或等 MAX 自然过期）。
HARD_FAILURE_CODES: frozenset[str] = frozenset({
    "1113",  # 账户欠费 / 余额不足 / 无可用资源包
    "1314",  # 企业套餐已失效
    "1311",  # 当前订阅套餐未开放该模型权限（非临时，需升级套餐）
})

# 软限流：保持 Retry-After / 默认窗口。这里不穷举——1302/1305/1308/1310/1313
# 以及 1316-1321（用量上限+余额不足的复合码）都按"软"处理，因为即便复合了
# 余额不足，超长熔断也无意义（用户充值前不会停）。复合码会在 trip reason
# 里留痕，方便运维看。
SOFT_LIMIT_CODES: frozenset[str] = frozenset({
    "1302",  # 账户已达到速率限制（QPM/并发）
    "1305",  # 该模型当前访问量过大（平台过载）
    "1308",  # 达到 number unit 使用上限
    "1310",  # 达到每周/每月使用上限
    "1313",  # 违反公平使用规则
})


def _repo_root() -> Path:
    """仓库根目录。本文件是 <root>/infrastructure/budget/circuit_breaker.py。"""
    return Path(__file__).resolve().parents[2]


def _data_dir() -> Path:
    d = _repo_root() / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _db_path() -> Path:
    return _data_dir() / "circuit_breaker.db"


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS circuit_breaker (
    api_key_fingerprint TEXT PRIMARY KEY,   -- sha256(api_key)[:16]，明文不落库
    tripped_at          TEXT NOT NULL,      -- ISO 时间（UTC）
    expires_at          TEXT NOT NULL,      -- ISO 时间（UTC），读时判过期
    retry_after_sec     REAL NOT NULL,      -- 实际生效的 Retry-After（留痕）
    reason              TEXT DEFAULT '',
    tripped_by          TEXT DEFAULT ''     -- instance_id（谁触发的）
);
"""


def get_circuit_breaker_db() -> _sqlite.Connection:
    """返回 circuit_breaker.db 的连接（WAL + 幂等建表）。

    每用每开（同 token_tracker / global_todos 范式），用完由调用方 close。
    WAL 模式保证多进程读写不互斥。
    """
    db = _sqlite.connect(str(_db_path()))
    db.row_factory = _sqlite.Row
    # durability: WAL + FULL synchronous 防 WAL 半写损坏。
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("PRAGMA busy_timeout=5000")
    db.executescript(_SCHEMA_SQL)
    db.commit()
    return db


def _fingerprint(api_key: str) -> str:
    """api_key → 16 位 hex 指纹。明文 key 只活在调用栈内存里。"""
    if not api_key:
        return "_no_key"
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:_FINGERPRINT_LEN]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(s: str) -> datetime:
    """ISO 字符串 → aware datetime（UTC）。fromisoformat 在 3.11 支持 Z 但保守起见
    用 replace 兜底 naive 情况。"""
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def resolve_retry_after(header_value: str | None) -> float:
    """合规解析 Retry-After 响应头，返回熔断秒数（clamp 到 [MIN, MAX]）。

    Retry-After 两种合规格式（RFC 7231）：
      - 整数秒：``Retry-After: 120``
      - HTTP-date：``Retry-After: Fri, 31 Dec 2023 23:59:59 GMT``

    无头 / None / 非法 → DEFAULT_RETRY_AFTER_SEC。
    负数 / 0 → MIN（防恶意头把熔断时长设成 0）。
    超大 → MAX（防 HTTP-date 解析出错或离谱值卡死系统）。
    """
    if not header_value:
        return DEFAULT_RETRY_AFTER_SEC

    raw = header_value.strip()

    # 先试整数秒（最常见）
    try:
        secs = float(int(raw))
    except (ValueError, TypeError):
        secs = None
    else:
        return max(MIN_RETRY_AFTER_SEC, min(secs, MAX_RETRY_AFTER_SEC))

    # 再试 HTTP-date（parsedate_to_datetime 不认整数）
    if secs is None:
        try:
            target = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            target = None
        if target is not None:
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            secs = (target - datetime.now(timezone.utc)).total_seconds()
            # 已过期的 HTTP-date（target 在过去）→ 用兜底而不是 0
            if secs <= 0:
                return DEFAULT_RETRY_AFTER_SEC
            return max(MIN_RETRY_AFTER_SEC, min(secs, MAX_RETRY_AFTER_SEC))

    # 非法格式 → 兜底
    return DEFAULT_RETRY_AFTER_SEC


def extract_glm_error_code(body: Any) -> str | None:
    """从 GLM 429 响应体提取 ``error.code``。

    body 形如 ``{"error":{"code":"1113","message":"..."}}``。解析失败/结构
    不符返回 None（调用方按"未知 code"处理，保守沿用 Retry-After 逻辑）。
    """
    if not body:
        return None
    if isinstance(body, (bytes, bytearray)):
        import json as _json

        try:
            body = _json.loads(body)
        except Exception:
            return None
    if isinstance(body, str):
        import json as _json

        try:
            body = _json.loads(body)
        except Exception:
            return None
    if not isinstance(body, dict):
        return None
    err = body.get("error")
    if not isinstance(err, dict):
        return None
    code = err.get("code")
    return str(code) if code is not None else None


def classify_429(error_code: str | None) -> str:
    """把 GLM 429 的 error.code 归为三类：hard / soft / unknown。

    - hard：硬故障（充值前重试必败），应拉长熔断 → 返回 ``"hard"``
    - soft：软限流（窗口后自愈），沿用 Retry-After → 返回 ``"soft"``
    - unknown：未识别 code，保守按 soft 处理 → 返回 ``"unknown"``
    """
    if error_code is None:
        return "unknown"
    if error_code in HARD_FAILURE_CODES:
        return "hard"
    if error_code in SOFT_LIMIT_CODES:
        return "soft"
    return "unknown"


def resolve_retry_after_for_429(
    *,
    retry_after_header: str | None,
    response_body: Any = None,
) -> tuple[float, str]:
    """综合 Retry-After 头 + response body 的 error.code 决定熔断时长。

    返回 ``(retry_after_sec, reason)``。reason 形如 ``"429:hard:1113"`` 或
    ``"429:soft:1302"`` 或 ``"429:unknown"``，落库留痕便于运维诊断。

    策略：
      - error.code 命中 HARD_FAILURE_CODES → MAX_RETRY_AFTER_SEC（等也没用，
        拉满避免反复刷 expires_at；充值后运维 clear() 或等自然过期）。
      - 否则 → resolve_retry_after(retry_after_header) 维持原逻辑（软限流
        尊重 Retry-After 头，未知 code 保守用默认窗口）。
    """
    code = extract_glm_error_code(response_body)
    kind = classify_429(code)
    reason = f"429:{kind}" + (f":{code}" if code else "")
    if kind == "hard":
        return (MAX_RETRY_AFTER_SEC, reason)
    return (resolve_retry_after(retry_after_header), reason)


def trip(
    api_key: str,
    *,
    retry_after_sec: float,
    instance_id: str = "",
    reason: str = "429",
) -> None:
    """撞 429 时调用：把这把 key 标记为熔断，到 expires_at 自动恢复。

    upsert 语义：**只有在新的 expires_at 比现有更晚时才覆盖**。避免短退避
    的实例把长退避（更晚恢复）的状态覆盖掉。例如 A 实例 trip 了 60s，5s 后
    B 实例又 trip 了 10s——不能让 B 把系统提前 50s 恢复。

    expires_at = now + retry_after_sec。retry_after_sec 由调用方按
    resolve_retry_after() 解析好传进来。
    """
    fp = _fingerprint(api_key)
    now = datetime.now(timezone.utc)
    retry_after_sec = max(MIN_RETRY_AFTER_SEC, min(retry_after_sec, MAX_RETRY_AFTER_SEC))
    new_expires = now.timestamp() + retry_after_sec
    expires_iso = datetime.fromtimestamp(new_expires, tz=timezone.utc).isoformat(timespec="seconds")

    db = None
    try:
        db = get_circuit_breaker_db()
        existing = db.execute(
            "SELECT expires_at FROM circuit_breaker WHERE api_key_fingerprint = ?",
            (fp,),
        ).fetchone()
        if existing is not None:
            # 只有更晚才覆盖（保护长退避）。
            try:
                cur_expires_ts = _parse_iso(existing["expires_at"]).timestamp()
            except (ValueError, TypeError):
                cur_expires_ts = 0.0
            if new_expires <= cur_expires_ts:
                logger.info(
                    "circuit breaker already tripped with later expires_at "
                    "(fp=%s cur=%s > new=%s) — not overwriting",
                    fp, existing["expires_at"], expires_iso,
                )
                return
        db.execute(
            """
            INSERT INTO circuit_breaker
                (api_key_fingerprint, tripped_at, expires_at, retry_after_sec, reason, tripped_by)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(api_key_fingerprint) DO UPDATE SET
                tripped_at      = excluded.tripped_at,
                expires_at      = excluded.expires_at,
                retry_after_sec = excluded.retry_after_sec,
                reason          = excluded.reason,
                tripped_by      = excluded.tripped_by
            """,
            (fp, now.isoformat(timespec="seconds"), expires_iso, retry_after_sec,
             reason, instance_id),
        )
        db.commit()
        logger.warning(
            "circuit breaker TRIPPED (fp=%s retry_after=%.0fs expires_at=%s by=%s reason=%s)",
            fp, retry_after_sec, expires_iso, instance_id or "?", reason,
        )
    except Exception as exc:
        # 熔断是保护机制，自身故障不能阻断 LLM 流程（同 _record_token_usage 的 swallow 策略）。
        logger.debug("circuit breaker trip failed: %s", exc)
    finally:
        if db is not None:
            db.close()


def is_tripped(api_key: str) -> tuple[bool, dict[str, Any]]:
    """这把 key 当前是否处于熔断中。

    读时判过期：``expires_at <= now`` → 自动删行并返回 ``(False, {})``，
    实现自动过期恢复。否则返回 ``(True, {expires_at, retry_after_sec,
    reason, tripped_by})``。

    任何 DB 异常都 fail-open 返回 ``(False, {})``：熔断故障宁可漏（多打一次
    API）也不能误杀（让所有 wake 永久卡死）。
    """
    fp = _fingerprint(api_key)
    now_ts = datetime.now(timezone.utc).timestamp()
    db = None
    try:
        db = get_circuit_breaker_db()
        row = db.execute(
            "SELECT expires_at, retry_after_sec, reason, tripped_by "
            "FROM circuit_breaker WHERE api_key_fingerprint = ?",
            (fp,),
        ).fetchone()
        if row is None:
            return (False, {})
        try:
            expires_ts = _parse_iso(row["expires_at"]).timestamp()
        except (ValueError, TypeError):
            expires_ts = 0.0
        if expires_ts <= now_ts:
            # 自动过期恢复：删行清状态。
            db.execute(
                "DELETE FROM circuit_breaker WHERE api_key_fingerprint = ?",
                (fp,),
            )
            db.commit()
            logger.info(
                "circuit breaker auto-recovered (fp=%s expired_at=%s) — resuming",
                fp, row["expires_at"],
            )
            return (False, {})
        return (True, {
            "expires_at": row["expires_at"],
            "retry_after_sec": row["retry_after_sec"],
            "reason": row["reason"],
            "tripped_by": row["tripped_by"],
        })
    except Exception as exc:
        # fail-open：DB 故障宁可漏熔断（多打一次 API）也不误杀所有实例。
        logger.debug("circuit breaker is_tripped lookup failed: %s", exc)
        return (False, {})
    finally:
        if db is not None:
            db.close()


def clear(api_key: str) -> bool:
    """手动清除某把 key 的熔断状态（运维 / 测试用）。返回是否实际清了一行。"""
    fp = _fingerprint(api_key)
    db = None
    try:
        db = get_circuit_breaker_db()
        cur = db.execute(
            "DELETE FROM circuit_breaker WHERE api_key_fingerprint = ?",
            (fp,),
        )
        db.commit()
        deleted = cur.rowcount > 0
        if deleted:
            logger.info("circuit breaker manually cleared (fp=%s)", fp)
        return deleted
    except Exception as exc:
        logger.debug("circuit breaker clear failed: %s", exc)
        return False
    finally:
        if db is not None:
            db.close()


def circuit_breaker_db_path() -> Path:
    """暴露 DB 路径，便于 backup / 测试。"""
    return _db_path()


# 专用异常：in-flight 闸口（_chat 入口）检测到熔断已生效时抛出，语义清晰，
# 不与 httpx 异常混淆。
class CircuitBreakerOpen(Exception):
    """_chat 入口检测到熔断已生效，拒绝实际打 LLM API。"""

    def __init__(self, api_key_fingerprint: str, expires_at: str) -> None:
        self.api_key_fingerprint = api_key_fingerprint
        self.expires_at = expires_at
        super().__init__(
            f"circuit breaker open (fp={api_key_fingerprint} until {expires_at})"
        )
