"""端点 reasoning_effort 值域学习库。

OpenAI 兼容端点没有能力自省——值域只能由端点自己的 400 告知（如 newapi
渠道只认 low/high/max，家族默认 medium 撞墙）。首次撞墙后按端点记下邻档
映射并持久化到 var/llm_effort_domains.json：之后该端点上所有 agent、所有
唤醒装配时直接用对档，不再付学费。
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_CACHE: dict[str, dict[str, str]] | None = None

# 邻档映射：值域窄的端点接受语义最近的档
NEAR: dict[str, str] = {"minimal": "low", "medium": "high", "xhigh": "max"}


def _store_path() -> Path:
    from infrastructure.config import get_project_root

    return get_project_root() / "var" / "llm_effort_domains.json"


def _load() -> dict[str, dict[str, str]]:
    global _CACHE
    if _CACHE is None:
        try:
            _CACHE = json.loads(_store_path().read_text(encoding="utf-8"))
        except Exception:
            _CACHE = {}
    return _CACHE


def endpoint_key(url: str) -> str:
    """按 scheme://host 归一端点（路径无关）。"""
    try:
        p = urlsplit(url)
        return f"{p.scheme}://{p.netloc}"
    except Exception:
        return url


def get_fix(url: str, effort: str) -> str | None:
    """该端点已学得的档位映射；未学得返回 None。"""
    return _load().get(endpoint_key(url), {}).get(effort)


def record_fix(url: str, effort: str, mapped: str) -> None:
    key = endpoint_key(url)
    with _LOCK:
        store = _load()
        store.setdefault(key, {})[effort] = mapped
        try:
            path = _store_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(store, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(path)
        except Exception:  # noqa: BLE001 — 持久化失败不阻断当次自愈重试
            logger.debug("effort domain store persist failed", exc_info=True)
