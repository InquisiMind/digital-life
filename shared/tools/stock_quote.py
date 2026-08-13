"""Auto-registered tool: stock_quote (scope=shared)."""

from typing import Any, Dict


def handler(args: Dict[str, Any], **kwargs: Any) -> str:
    code = args.get("code", "")
    import requests, json

    if not code:
        return json.dumps({"error": "code is required (e.g. 'sz000025', 'sh600629')"}, ensure_ascii=False)

    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        url = f"http://qt.gtimg.cn/q={code}"
        resp = requests.get(url, headers=headers, timeout=10)
        resp.encoding = 'gbk'

        if '"' not in resp.text:
            return json.dumps({"error": "无法获取数据，可能代码错误"}, ensure_ascii=False)

        content = resp.text.split('"')[1]
        fields = content.split('~')

        if len(fields) < 10:
            return json.dumps({"error": "数据解析失败"}, ensure_ascii=False)

        result = {
            "name": fields[1].strip(),
            "code": code,
            "current_price": float(fields[3]) if fields[3] else 0,
            "yesterday_close": float(fields[4]) if fields[4] else 0,
            "open": float(fields[5]) if fields[5] else 0,
            "volume": fields[36] if len(fields) > 36 else "N/A",
            "amount": fields[37] if len(fields) > 37 else "N/A",
            "turnover_rate": fields[38] + "%" if len(fields) > 38 else "N/A",
            "pe_ratio": fields[39] if len(fields) > 39 else "N/A",
            "high": float(fields[33]) if len(fields) > 33 and fields[33] else 0,
            "low": float(fields[34]) if len(fields) > 34 and fields[34] else 0,
            "change_pct": fields[32] + "%" if len(fields) > 32 else "N/A",
            "total_market_cap": fields[45] + "亿" if len(fields) > 45 else "N/A",
            "circulating_market_cap": fields[44] + "亿" if len(fields) > 44 else "N/A",
        }

        return json.dumps(result, ensure_ascii=False, indent=2)

    except requests.exceptions.Timeout:
        return json.dumps({"error": "请求超时"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"获取失败: {str(e)}"}, ensure_ascii=False)
