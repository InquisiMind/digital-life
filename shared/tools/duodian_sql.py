"""Auto-registered tool: duodian_sql (scope=shared)."""

from typing import Any, Dict


def handler(args: Dict[str, Any], **kwargs: Any) -> str:
    import base64, json, urllib.request, os

    # 凭据文件：优先 shared 层（跨实例协作），回退 zero 工作区
    CONF_CANDIDATES = [
        "/Users/zhanghaopu/Documents/探索项目/工作区/shared/duodian_adapter.json",
        os.path.expanduser("/Users/zhanghaopu/Documents/探索项目/工作区/zero/duodian_adapter.json"),
    ]
    conf = None
    for p in CONF_CANDIDATES:
        if os.path.exists(p):
            with open(p) as f:
                conf = json.load(f)
            break
    if conf is None:
        raise FileNotFoundError("duodian_adapter.json 未找到于 shared/ 或 zero/ 工作区")
    BASE = conf["base_url"]
    AUTH = base64.b64encode(f'{conf["username"]}:{conf["password"]}'.encode()).decode()

    sql = (args.get("sql") or "").strip()
    if not sql:
        return "错误：sql 参数为空"
    up = sql.upper().lstrip()
    if not up.startswith("SELECT"):
        return "错误：仅接受 SELECT 查询（该通道为只读）"
    if ";" in sql or "--" in sql or "/*" in sql:
        return "错误：仅接受单条 SELECT（禁止分号/注释）"

    EMPTY_TABLES = {"ol_order", "purchase", "sale_cost"}
    tables_mentioned = [t for t in EMPTY_TABLES if t.lower() in sql.lower()]

    r = urllib.request.Request(BASE + "/api/erp/sql", method="POST",
        headers={"Authorization": "Basic " + AUTH, "Content-Type": "application/json"},
        data=json.dumps({"sql": sql}).encode())
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            d = json.loads(resp.read().decode())
        out = {"cols": d.get("cols"), "col_cn": d.get("col_cn"), "n": d.get("n"),
               "rows": d.get("rows", [])[:50], "truncated": d.get("truncated"),
               "server_sql": d.get("sql")}
        if tables_mentioned and d.get("n") == 0:
            out["hint"] = f"注意：{tables_mentioned} 在此库中无数据（空表），返回空行属正常，非查询错误"
        return json.dumps(out, ensure_ascii=False)
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}: {e.read(200).decode('utf-8','ignore')}"
    except Exception as e:
        return f"请求失败: {e}"
