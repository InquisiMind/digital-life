"""Auto-registered tool: duodian_data (scope=shared)."""

from typing import Any, Dict


def handler(args: Dict[str, Any], **kwargs: Any) -> str:
    import base64, json, urllib.request, os

    CONF = os.path.expanduser("/Users/zhanghaopu/Documents/探索项目/工作区/zero/duodian_adapter.json")
    with open(CONF) as f:
        conf = json.load(f)
    BASE = conf["base_url"]
    AUTH = base64.b64encode(f'{conf["username"]}:{conf["password"]}'.encode()).decode()

    ep = {"overview": "/api/erp/overview", "tables": "/api/erp/tables", "agents": "/api/agents", "days": "/api/chat/days"}
    name = (args.get("endpoint") or "overview").lower()
    if name not in ep:
        return f"错误：endpoint 仅支持 {list(ep)}"
    url = BASE + ep[name]
    if name == "days" and args.get("day"):
        url += "?day=" + str(args["day"])
    r = urllib.request.Request(url, headers={"Authorization": "Basic " + AUTH})
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            d = json.loads(resp.read().decode())
        s = json.dumps(d, ensure_ascii=False)
        return s if len(s) <= 6000 else s[:6000] + "…(截断)"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}: {e.read(200).decode('utf-8','ignore')}"
    except Exception as e:
        return f"请求失败: {e}"
