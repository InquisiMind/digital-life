"""web_search — 联网检索工具（博查 Web Search API，provider 可扩展）。

配置（config/local.yaml，机器级密钥不入 git）：

  web_search:
    provider: bocha
    api_key: "<博查 key>"     # 填入后工具自动出现在已配工具面的角色上

返回：清洗后的结果列表（标题/URL/摘要/站点/日期），供模型直接引用。
无 key 或调用失败：工具隐藏/返回 error，不影响其他工具。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("digital_life.web_search")

_API_URL = "https://api.bochaai.com/v1/web-search"
_FRESHNESS = {None, "", "noLimit", "oneDay", "oneWeek", "oneMonth", "oneYear"}


def _ws_cfg() -> dict[str, Any]:
    try:
        import yaml

        from infrastructure.config import get_project_root

        path = get_project_root() / "config" / "local.yaml"
        if not path.exists():
            return {}
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        ws = data.get("web_search") or {}
        if not isinstance(ws, dict):
            return {}
        provider = str(ws.get("provider") or "bocha")
        keys = ws.get("keys") if isinstance(ws.get("keys"), dict) else {}
        api_key = str(ws.get("api_key") or keys.get(provider) or "").strip()
        return {"provider": provider, "api_key": api_key}
    except Exception:  # noqa: BLE001 — 配置缺失等同未配置
        return {}


def _configured() -> bool:
    return bool(_ws_cfg().get("api_key"))


def _bocha_search(query: str, count: int, freshness: str) -> list[dict[str, Any]]:
    import requests

    cfg = _ws_cfg()
    resp = requests.post(
        _API_URL,
        headers={
            "Authorization": f"Bearer {cfg['api_key']}",
            "Content-Type": "application/json",
        },
        json={"query": query, "count": count, "summary": True,
              "freshness": freshness or "noLimit"},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") not in (200, 0):
        raise RuntimeError(f"bocha 返回异常: {str(data.get('msg') or data)[:200]}")
    pages = ((data.get("data") or {}).get("webPages") or {}).get("value") or []
    out = []
    for p in pages:
        out.append({
            "title": p.get("name") or "",
            "url": p.get("url") or "",
            "summary": p.get("summary") or p.get("snippet") or "",
            "site": p.get("siteName") or "",
            "date": (p.get("datePublished") or p.get("dateLastCrawled") or "")[:10],
        })
    return out


def _handle_web_search(args: dict[str, Any], **_) -> str:
    import json

    query = str(args.get("query") or "").strip()
    if not query:
        from interfaces.tools.registry import tool_error

        return tool_error("必须传 query")
    count = max(1, min(int(args.get("count") or 8), 10))
    freshness = str(args.get("freshness") or "noLimit")
    if freshness not in _FRESHNESS:
        freshness = "noLimit"
    try:
        results = _bocha_search(query, count, freshness)
    except Exception as exc:  # noqa: BLE001 — 工具边界：HTTP/配额错误转模型可读的 error
        from interfaces.tools.registry import tool_error

        return tool_error(f"搜索失败：{exc}（可稍后重试、换检索词，或用 execute_code 直接抓目标站点）")
    if not results:
        return json.dumps({"query": query, "results": [],
                           "note": "无结果——可换检索词/切英文/放宽 freshness 后重试"},
                          ensure_ascii=False)
    return json.dumps({"query": query, "count": len(results), "results": results},
                      ensure_ascii=False)


def _schema() -> dict[str, Any]:
    return {
        "name": "web_search",
        "description": (
            "联网搜索 — 检索公开网页（中文为主），返回标题/链接/摘要/站点/日期。"
            "用于：企业现状、行业数据、新闻舆情、政策法规等一切需要最新公开信息的场景。"
            "要点：①专有名词、公司名建议中英文各搜一次；"
            "②时效敏感的加 freshness（如 oneYear）；"
            "③关键数字仍须回原始出处核对（配合 s01 证据分级）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索词（一句话，可含公司名/指标名）"},
                "count": {"type": "integer", "description": "结果条数，默认 8，最多 10"},
                "freshness": {"type": "string", "enum": ["noLimit", "oneDay", "oneWeek",
                                                         "oneMonth", "oneYear"],
                              "description": "时效范围：限定最近发布时间"},
            },
            "required": ["query"],
        },
    }


def register(registry=None) -> None:
    (registry or _registry()).register(
        name="web_search",
        toolset="research",
        schema=_schema(),
        handler=_handle_web_search,
        check_fn=_configured,
        requires_env=(),
        description="联网搜索（博查）",
        emoji="🔍",
        max_result_size_chars=16_000,
    )


def _registry():
    from interfaces.tools.registry import registry

    return registry


register()  # import 副作用：随模块装载自动注册（与其他工具模块一致）
