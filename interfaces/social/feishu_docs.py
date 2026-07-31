"""飞书文档读取模块 — 以 user_access_token 身份读 wiki/docx/sheet。

设计:
  - 独立于 social takeover daemon, 不依赖 daemon 内存状态。
  - 复用 social.env 里的 refresh_token, 自己刷新 user_access_token。
  - 支持 4 类链接: wiki/ docx/ sheets/ base/(bitable)。
  - wiki 是"外壳": 先 get_node 拿 obj_token+obj_type, 再按类型读内容。

调用入口:
  read_feishu_url(url) -> { ok, type, title, content, ... }

权限域 (OAuth scope):
  - wiki:wiki:readonly       读 wiki 节点
  - docx:document:readonly   读 docx 文档内容
  - sheets:spreadsheet:readonly 读电子表格
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

FEISHU_BASE = "https://open.feishu.cn/open-apis"

# ── token 缓存 (进程内, 避免每次调用都刷新) ──────────────────────
_token_cache: dict[str, dict] = {}  # instance_id -> {access, refresh, expires}
_TOKEN_REFRESH_LOCK = {}  # instance_id -> threading.Lock


def _get_app_creds() -> tuple[str, str]:
    """读飞书 app_id + app_secret (与 feishu_takeover 同源逻辑)。"""
    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    if app_id and app_secret:
        return app_id, app_secret
    try:
        from pathlib import Path
        from infrastructure.config import get_instance_dir, get_app_instance_id
        iid = get_app_instance_id() or ""
        if iid:
            secrets = get_instance_dir(iid) / "config" / "secrets.env"
            if secrets.exists():
                for line in secrets.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("FEISHU_APP_ID=") and not app_id:
                        app_id = line.split("=", 1)[1].strip()
                    elif line.startswith("FEISHU_APP_SECRET=") and not app_secret:
                        app_secret = line.split("=", 1)[1].strip()
            if not app_id:
                import yaml
                from infrastructure.config import get_instance_app_config_path
                cfg = get_instance_app_config_path()
                if cfg.exists():
                    raw = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
                    feishu = (raw.get("channels") or {}).get("feishu") or {}
                    app_id = app_id or feishu.get("app_id", "")
    except Exception:
        pass
    return app_id, app_secret


def _get_tenant_token() -> str:
    """app_id/secret 换 tenant_access_token。"""
    app_id, app_secret = _get_app_creds()
    if not app_id or not app_secret:
        return ""
    try:
        resp = httpx.post(
            f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
            headers={"Content-Type": "application/json"},
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=10,
        )
        return resp.json().get("tenant_access_token", "")
    except Exception:
        return ""


def _load_social_env() -> tuple[str, str, str]:
    """从 apps/{iid}/config/social.env 读 user access/refresh token + instance_id。"""
    try:
        from pathlib import Path
        from infrastructure.config import get_instance_dir, get_app_instance_id
        iid = get_app_instance_id() or ""
        if not iid:
            return "", "", ""
        env = get_instance_dir(iid) / "config" / "social.env"
        if not env.exists():
            return "", "", iid
        access = refresh = ""
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("FEISHU_USER_ACCESS_TOKEN="):
                access = line.split("=", 1)[1].strip()
            elif line.startswith("FEISHU_USER_REFRESH_TOKEN="):
                refresh = line.split("=", 1)[1].strip()
        return access, refresh, iid
    except Exception:
        return "", "", ""


def _ensure_user_token() -> str:
    """拿有效的 user_access_token — 缓存 + 自动刷新。返回空串=失败。"""
    import threading

    access, refresh, iid = _load_social_env()
    if not iid:
        return ""
    if iid not in _TOKEN_REFRESH_LOCK:
        _TOKEN_REFRESH_LOCK[iid] = threading.Lock()

    with _TOKEN_REFRESH_LOCK[iid]:
        cache = _token_cache.get(iid)
        if cache and cache.get("access") and time.time() < cache.get("expires", 0):
            return cache["access"]

        # 先试现有 access (可能仍是有效的)
        if access and _test_token(access):
            _token_cache[iid] = {"access": access, "refresh": refresh, "expires": time.time() + 600}
            return access

        # 刷新
        if not refresh:
            return ""
        new_access = _refresh_token(refresh)
        if new_access:
            _token_cache[iid] = {"access": new_access, "refresh": refresh, "expires": time.time() + 7000}
            return new_access
        return ""


def _test_token(access: str) -> bool:
    """快速探活 user_access_token (用 user_info 接口)。"""
    try:
        resp = httpx.get(
            f"{FEISHU_BASE}/authen/v1/user_info",
            headers={"Authorization": f"Bearer {access}"},
            timeout=8,
        )
        return resp.json().get("code") == 0
    except Exception:
        return False


def _refresh_token(refresh: str) -> str:
    """refresh_token 换新 access_token。"""
    tenant_tok = _get_tenant_token()
    if not tenant_tok:
        return ""
    try:
        resp = httpx.post(
            f"{FEISHU_BASE}/authen/v1/oidc/refresh_access_token",
            headers={
                "Authorization": f"Bearer {tenant_tok}",
                "Content-Type": "application/json",
            },
            json={"grant_type": "refresh_token", "refresh_token": refresh},
            timeout=10,
        )
        data = resp.json()
        if data.get("code") != 0:
            logger.warning("feishu_docs: refresh failed: %s", data.get("msg", ""))
            return ""
        token_data = data.get("data") or {}
        new_access = token_data.get("access_token", "")
        new_refresh = token_data.get("refresh_token", "")
        # 回写 social.env (refresh_token 会轮换, 必须持久化新的)
        if new_refresh:
            _persist_refresh(new_refresh)
        return new_access
    except Exception as exc:
        logger.warning("feishu_docs: refresh exception: %s", exc)
        return ""


def _persist_refresh(refresh: str) -> None:
    """把轮换后的 refresh_token 写回 social.env。"""
    try:
        from pathlib import Path
        from infrastructure.config import get_instance_dir, get_app_instance_id
        iid = get_app_instance_id() or ""
        if not iid:
            return
        env = get_instance_dir(iid) / "config" / "social.env"
        if not env.exists():
            return
        lines = env.read_text(encoding="utf-8").splitlines()
        out = []
        found = False
        for line in lines:
            if line.startswith("FEISHU_USER_REFRESH_TOKEN="):
                out.append(f"FEISHU_USER_REFRESH_TOKEN={refresh}")
                found = True
            else:
                out.append(line)
        if not found:
            out.append(f"FEISHU_USER_REFRESH_TOKEN={refresh}")
        env.write_text("\n".join(out) + "\n", encoding="utf-8")
    except Exception:
        pass


def _api_get(path: str, params: dict | None = None) -> dict | None:
    """以 user 身份 GET 飞书 API。code != 0 返回 None。"""
    token = _ensure_user_token()
    if not token:
        return None
    try:
        resp = httpx.get(
            f"{FEISHU_BASE}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params or {},
            timeout=15,
        )
        data = resp.json()
        if data.get("code") != 0:
            logger.debug("feishu_docs API %s code=%s msg=%s",
                         path, data.get("code"), data.get("msg", ""))
            return None
        return data.get("data") or {}
    except Exception as exc:
        logger.debug("feishu_docs API %s exception: %s", path, exc)
        return None


def _api_post(path: str, json_body: dict | None = None, params: dict | None = None) -> dict | None:
    """以 user 身份 POST 飞书 API。code != 0 返回 None。

    镜像 _api_get, 用于写入操作 (append_sheet/append_docx/export_task create)。
    """
    token = _ensure_user_token()
    if not token:
        return None
    try:
        resp = httpx.post(
            f"{FEISHU_BASE}{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            params=params or {},
            json=json_body or {},
            timeout=20,
        )
        data = resp.json()
        if data.get("code") != 0:
            logger.debug("feishu_docs POST %s code=%s msg=%s",
                         path, data.get("code"), data.get("msg", ""))
            return None
        return data.get("data") or {}
    except Exception as exc:
        logger.debug("feishu_docs POST %s exception: %s", path, exc)
        return None


def _resolve_wiki_obj(node_token: str) -> dict[str, str] | None:
    """wiki 节点 → {obj_token, obj_type, title}。读取和写入都用。

    失败返回 None (调用方自行决定报错文案)。
    """
    node = _api_get(
        "/wiki/v2/spaces/get_node",
        params={"token": node_token},
    )
    if not node or not node.get("node"):
        return None
    info = node["node"]
    obj_token = info.get("obj_token", "")
    obj_type = info.get("obj_type", "")
    title = info.get("title", "")
    if not obj_token:
        return None
    return {"obj_token": obj_token, "obj_type": obj_type, "title": title}


# ── URL 解析 ─────────────────────────────────────────────────────

def parse_feishu_url(url: str) -> dict[str, str]:
    """解析飞书文档链接 → {type, token}。

    支持的前缀 (以 https://*.feishu.cn/ 后):
      /wiki/{node_id}      → wiki (需二次解析 obj_token)
      /docx/{doc_token}    → docx
      /sheets/{sht_token}  → sheet
      /base/{app_token}    → bitable

    URL 里 ?from=from_copylink 等查询参数忽略。
    带子路径的也兼容 (/docx/XxX/edit?uid=... → docx)
    """
    url = (url or "").strip()
    if not url:
        return {"type": "", "token": ""}

    # 提取 path (忽略 query/fragment)
    try:
        parsed = urlparse(url)
        path = parsed.path or ""
        host = parsed.netloc or ""
    except Exception:
        return {"type": "", "token": ""}

    # 确认是飞书域名
    if "feishu.cn" not in host and "larksuite" not in host and "bytedance" not in host:
        return {"type": "", "token": ""}

    # 逐前缀匹配
    patterns = [
        (r"/wiki/([A-Za-z0-9]+)", "wiki"),
        (r"/docx/([A-Za-z0-9]+)", "docx"),
        (r"/sheets/([A-Za-z0-9]+)", "sheet"),
        (r"/base/([A-Za-z0-9]+)", "bitable"),
    ]
    for pat, kind in patterns:
        m = re.search(pat, path)
        if m:
            return {"type": kind, "token": m.group(1)}
    return {"type": "", "token": ""}


# ── 各类型内容读取 ────────────────────────────────────────────────

def _read_wiki(node_token: str) -> dict[str, Any]:
    """wiki 是外壳: get_node 拿 obj_token+obj_type, 再按类型读内容。"""
    node = _api_get(
        "/wiki/v2/spaces/get_node",
        params={"token": node_token},
    )
    if not node or not node.get("node"):
        return {"ok": False, "reason": "wiki get_node 失败 (可能权限不足或链接错误)"}
    info = node["node"]
    obj_token = info.get("obj_token", "")
    obj_type = info.get("obj_type", "")  # docx / sheet / bitable / ...
    title = info.get("title", "")
    if not obj_token:
        return {"ok": False, "reason": f"wiki 节点 obj_token 空, obj_type={obj_type}"}

    # 递归读实际内容
    if obj_type == "docx":
        body = _read_docx(obj_token)
        return {**body, "title": title or body.get("title", ""), "wiki_type": "docx"}
    if obj_type == "sheet":
        body = _read_sheet(obj_token)
        return {**body, "title": title or body.get("title", ""), "wiki_type": "sheet"}
    if obj_type == "bitable":
        body = _read_bitable(obj_token)
        return {**body, "title": title or body.get("title", ""), "wiki_type": "bitable"}
    # 未知类型至少返标题和元信息
    return {
        "ok": True,
        "type": "wiki",
        "obj_type": obj_type,
        "title": title,
        "content": f"[wiki 节点类型 {obj_type} 暂不支持自动读取内容, 请用对应直链]",
        "node_url": obj_token,
    }


def _read_docx(doc_token: str) -> dict[str, Any]:
    """读 docx 文档。优先 raw_content (纯文本), 失败再试 blocks。"""
    data = _api_get(f"/docx/v1/documents/{doc_token}/raw_content")
    if data is None:
        return {"ok": False, "reason": "docx raw_content 读取失败 (权限不足或文档不存在)"}
    content = data.get("content", "")
    # 再拿标题
    title = ""
    meta = _api_get(f"/docx/v1/documents/{doc_token}")
    if meta and meta.get("document"):
        title = meta["document"].get("title", "")
    return {
        "ok": True,
        "type": "docx",
        "title": title,
        "content": content,
        "length": len(content),
    }


def _read_sheet(sheet_token: str) -> dict[str, Any]:
    """读电子表格 — 先查 sheet 列表, 再读每个 sheet 的值。

    限制: 多 sheet 只读前 3 个, 每 sheet 最多 50 行, 避免超长。

    注意 v3 的 /spreadsheets/{token} 只返 spreadsheet 元信息(无 sheets 列表),
    sheet 列表必须单独查 /spreadsheets/{token}/sheets/query。
    """
    # 1. spreadsheet 元信息 (标题)
    meta = _api_get(f"/sheets/v3/spreadsheets/{sheet_token}")
    if not meta:
        return {"ok": False, "reason": "sheet 元信息读取失败 (权限不足或链接错误)"}
    sp = meta.get("spreadsheet") or {}
    title = sp.get("title", "")

    # 2. sheet 列表 (独立接口)
    sheets_resp = _api_get(f"/sheets/v3/spreadsheets/{sheet_token}/sheets/query")
    sheets = (sheets_resp or {}).get("sheets") or []
    if not sheets:
        return {"ok": False, "reason": f"spreadsheet '{title}' 没有 sheet 或无权读", "title": title}

    # 3. 逐 sheet 读值 (限制前 3 个)
    parts: list[str] = []
    for sh in sheets[:3]:
        sid = sh.get("sheet_id", "")
        sname = sh.get("title", sid)
        if not sid:
            continue
        # v2 values API: range=sheetId!A1:Z50
        # 注意: dateTimeRenderOption 的值飞书不认(Formatted 无效), 只传 valueRenderOption
        rng = f"{sid}!A1:Z50"
        vals = _api_get(
            f"/sheets/v2/spreadsheets/{sheet_token}/values/{rng}",
            params={"valueRenderOption": "ToString"},
        )
        if not vals:
            continue
        rows = vals.get("valueRange", {}).get("values") or []
        parts.append(f"### Sheet: {sname}\n" + _render_table(rows))
    if not parts:
        return {"ok": False, "reason": "所有 sheet 读值失败"}
    return {
        "ok": True,
        "type": "sheet",
        "title": title,
        "content": "\n\n".join(parts),
        "sheet_count": len(sheets),
    }


def _read_bitable(app_token: str, table_id: str = "") -> dict[str, Any]:
    """读多维表格。不传 table_id → 先列表, 取第一个。"""
    if not table_id:
        tl = _api_get(f"/bitable/v1/apps/{app_token}/tables")
        if not tl or not tl.get("items"):
            return {"ok": False, "reason": "bitable 没有表 (权限不足或 app_token 错误)"}
        table_id = tl["items"][0].get("table_id", "")
    if not table_id:
        return {"ok": False, "reason": "table_id 解析失败"}
    fields = _api_get(f"/bitable/v1/apps/{app_token}/tables/{table_id}/fields")
    records = _api_get(
        f"/bitable/v1/apps/{app_token}/tables/{table_id}/records",
        params={"page_size": 50},
    )
    if records is None:
        return {"ok": False, "reason": "bitable records 读取失败"}
    rows = records.get("items") or []
    parts: list[str] = []
    if fields:
        flist = fields.get("items") or []
        parts.append("## 字段定义\n" + "\n".join(
            f"- {f.get('field_name', '')} ({f.get('type_name', '')})" for f in flist
        ))
    parts.append(f"## 记录 ({len(rows)} 条)\n" + _render_records(rows))
    return {
        "ok": True,
        "type": "bitable",
        "title": app_token,
        "content": "\n\n".join(parts),
        "record_count": len(rows),
    }


# ── 渲染辅助 ─────────────────────────────────────────────────────

def _render_table(rows: list[list]) -> str:
    """二维数组 → markdown 表格 (前 50 行)。"""
    if not rows:
        return "(空表)"
    rows = rows[:50]
    out = []
    for i, row in enumerate(rows):
        cells = [str(c) if c is not None else "" for c in row]
        out.append("| " + " | ".join(cells) + " |")
        if i == 0:
            out.append("| " + " | ".join("---" for _ in cells) + " |")
    return "\n".join(out)


def _render_records(rows: list[dict]) -> str:
    """bitable records → 文本。每条记录一行 JSON-ish。"""
    out = []
    for i, rec in enumerate(rows[:50], 1):
        fields = rec.get("fields") or {}
        cells = []
        for k, v in fields.items():
            if isinstance(v, list):
                v = "/".join(str(x.get("text", x) if isinstance(x, dict) else x) for x in v)
            cells.append(f"{k}={v}")
        out.append(f"{i}. {' | '.join(cells)}")
    return "\n".join(out) if out else "(无记录)"


# ── 对外总入口 ───────────────────────────────────────────────────

def read_feishu_url(url: str) -> dict[str, Any]:
    """读任意飞书文档链接 (统一入口)。

    返回:
      { ok: bool, type, title, content, length?, ... }
      失败时 ok=False, reason=说明
    """
    parsed = parse_feishu_url(url)
    kind = parsed.get("type", "")
    token = parsed.get("token", "")
    if not kind:
        return {
            "ok": False,
            "reason": f"无法识别飞书链接 (期望 /wiki/ /docx/ /sheets/ /base/): {url}",
        }
    if kind == "wiki":
        return _read_wiki(node_token=token)
    if kind == "docx":
        return _read_docx(doc_token=token)
    if kind == "sheet":
        return _read_sheet(sheet_token=token)
    if kind == "bitable":
        return _read_bitable(app_token=token)
    return {"ok": False, "reason": f"未知类型 {kind}"}


# ════════════════════════════════════════════════════════════════
# 写入能力 — 两步确认 (preview → confirm)
# ════════════════════════════════════════════════════════════════

# 最大行数限制 (防超长)
MAX_WRITE_ROWS = 50
# 导出任务轮询配置
_EXPORT_POLL_INTERVAL = 3.0
_EXPORT_POLL_MAX = 20  # 60s 总超时
# 文档类型 → 导出格式映射
_EXPORT_TYPE_FMT = {
    "sheet": "xlsx",
    "docx": "pdf",
    "bitable": "pdf",
}


def _write_sheet(sheet_token: str, sheet_id: str, rows: list[list]) -> dict[str, Any]:
    """往 sheet 追加行。用 values_append (OVERWRITE 模式)。

    飞书 API: POST /sheets/v2/spreadsheets/{token}/values_append
    body: { valueRange: { range: "{sid}!A1:{endCol}1", values: [[...]] } }

    注意 range 必须是完整范围 (如 A1:Z1), 不能是单格 (A1 会报 90202 wrong range)。
    append 会自动跳到表末尾追加, range 只用于声明列范围。
    """
    if not rows:
        return {"ok": False, "reason": "rows 为空"}
    if len(rows) > MAX_WRITE_ROWS:
        return {"ok": False, "reason": f"单次最多追加 {MAX_WRITE_ROWS} 行, 收到 {len(rows)} 行"}
    # 列数取所有行的最大值 (补齐空格)
    max_cols = max(len(r) for r in rows)
    norm_rows = [list(r) + [""] * (max_cols - len(r)) for r in rows]
    # range 用 A1:<endcol>1 完整范围。列号转换: 1→A, 26→Z, 27→AA
    end_col = _col_letter(max_cols)

    body = {
        "valueRange": {
            "range": f"{sheet_id}!A1:{end_col}1",
            "values": norm_rows,
        }
    }
    result = _api_post(
        f"/sheets/v2/spreadsheets/{sheet_token}/values_append",
        json_body=body,
        params={"insertDataOption": "OVERWRITE"},
    )
    if result is None:
        return {"ok": False, "reason": "values_append 失败 (sheet_id 错误或权限不足, 查 feishu_docs debug 日志)"}
    updated_range = result.get("updatedRange") or result.get("tableRange") or ""
    return {
        "ok": True,
        "action": "append_sheet",
        "appended": len(rows),
        "range": updated_range,
        "cols": max_cols,
    }


def _col_letter(n: int) -> str:
    """列序号 → Excel 列字母 (1→A, 26→Z, 27→AA)。range 范围用。"""
    result = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result = chr(65 + rem) + result
    return result


def _append_docx(doc_token: str, text: str) -> dict[str, Any]:
    """往 docx 文档末尾追加纯文本段落。

    飞书 API:
      1. GET /docx/v1/documents/{token} → 拿 document_id (根 block)
      2. POST /docx/v1/documents/{token}/blocks/{block_id}/children
         body: { children: [{block_type:2, text:{elements:[{text_run:{content:"..."}}]}}] }

    block_type=2 是文本块 (Text)。文档根 block 的 block_id 等于 document_id。
    """
    if not text or not text.strip():
        return {"ok": False, "reason": "text 为空"}
    text = text[:4000]  # 单段限 4000 字

    # 1. 拿根 block_id
    meta = _api_get(f"/docx/v1/documents/{doc_token}")
    if not meta or not meta.get("document"):
        return {"ok": False, "reason": "无法读 docx 元信息 (权限不足或文档不存在)"}
    doc = meta["document"]
    root_block_id = doc.get("document_id", "")
    title = doc.get("title", "")
    if not root_block_id:
        return {"ok": False, "reason": "document_id 解析失败"}

    # 2. 追加文本 block
    body = {
        "children": [
            {
                "block_type": 2,  # Text block
                "text": {
                    "elements": [
                        {"text_run": {"content": text}}
                    ]
                },
            }
        ],
        "index": -1,  # 末尾
    }
    result = _api_post(
        f"/docx/v1/documents/{doc_token}/blocks/{root_block_id}/children",
        json_body=body,
    )
    if result is None:
        return {"ok": False, "reason": "docx children POST 失败 (权限不足或 block_id 错误)"}
    added = (result.get("children") or [])
    return {
        "ok": True,
        "action": "append_docx",
        "title": title,
        "added_blocks": len(added),
        "text_len": len(text),
    }


def _export_doc(obj_token: str, obj_type: str, fmt: str = "") -> dict[str, Any]:
    """导出文档为 PDF/xlsx 本地文件。2 步异步 API。

    飞书 API:
      1. POST /drive/v1/export_tasks  → task_id
      2. GET /drive/v1/export_tasks/{task_id}  → 轮询到 result.file_token
      3. GET /drive/v1/download/{file_token}/download  → 下载二进制

    文件落地: apps/{iid}/data/exports/{token8}.{ext}
    """
    if not fmt:
        fmt = _EXPORT_TYPE_FMT.get(obj_type, "pdf")
    if fmt not in {"pdf", "xlsx", "docx"}:
        return {"ok": False, "reason": f"不支持的导出格式: {fmt}"}

    # 1. 创建导出任务
    create_body = {
        "file_extension": fmt,
        "token": obj_token,
        "type": _obj_type_to_export_type(obj_type),
    }
    task = _api_post("/drive/v1/export_tasks", json_body=create_body)
    if not task or not task.get("ticket"):
        return {"ok": False, "reason": "export_tasks 创建失败 (权限不足或 token 错误)"}
    task_id = task["ticket"]

    # 2. 轮询任务状态
    file_token = ""
    for _ in range(_EXPORT_POLL_MAX):
        time.sleep(_EXPORT_POLL_INTERVAL)
        status = _api_get(f"/drive/v1/export_tasks/{task_id}")
        if not status:
            continue
        jr = status.get("result") or {}
        if status.get("job_status") == 0 and jr.get("file_token"):
            file_token = jr["file_token"]
            break
        if status.get("job_status", 0) < 0:
            return {"ok": False, "reason": f"导出任务失败: code={status.get('job_status')}"}
    if not file_token:
        return {"ok": False, "reason": f"导出任务超时 ({_EXPORT_POLL_MAX * _EXPORT_POLL_INTERVAL:.0f}s)"}

    # 3. 下载文件
    return _download_export(file_token, obj_token, fmt, task_id)


def _obj_type_to_export_type(obj_type: str) -> str:
    """obj_type → 飞书 export type 参数。"""
    return {
        "docx": "docx",
        "sheet": "sheet",
        "bitable": "bitable",
    }.get(obj_type, obj_type)


def _download_export(file_token: str, obj_token: str, fmt: str, task_id: str) -> dict[str, Any]:
    """下载导出文件并落地到 apps/{iid}/data/exports/。"""
    try:
        from infrastructure.config import get_instance_data_dir, get_app_instance_id
        iid = get_app_instance_id() or ""
        if not iid:
            return {"ok": False, "reason": "无法确定 instance_id, 文件无法落地"}
        exports_dir = get_instance_data_dir(iid) / "exports"
        exports_dir.mkdir(parents=True, exist_ok=True)
        fname = f"{obj_token[:8]}_{task_id[:8]}.{fmt}"
        fpath = exports_dir / fname

        token = _ensure_user_token()
        if not token:
            return {"ok": False, "reason": "token 获取失败"}
        resp = httpx.get(
            f"{FEISHU_BASE}/drive/v1/download/{file_token}/download",
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
        )
        if resp.status_code != 200 or not resp.content:
            return {"ok": False, "reason": f"下载失败 status={resp.status_code}"}
        fpath.write_bytes(resp.content)

        # 入 attachments 库 (让 sense_image/register_attachment 可见)
        try:
            from infrastructure.persistence.instance.attachments import register_attachment
            register_attachment(
                instance_id=iid,
                source="feishu_export",
                source_key=obj_token,
                mime={"pdf": "application/pdf", "xlsx": "application/vnd.ms-excel"}.get(fmt, "application/octet-stream"),
                local_path=str(fpath),
                size_bytes=len(resp.content),
            )
        except Exception:
            pass  # 入库失败不影响导出成功

        return {
            "ok": True,
            "action": "export",
            "format": fmt,
            "local_path": str(fpath),
            "size_bytes": len(resp.content),
        }
    except Exception as exc:
        return {"ok": False, "reason": f"下载异常: {type(exc).__name__}: {exc}"}


def _preview_sheet_write(sheet_token: str, sheet_id: str, rows: list[list]) -> dict[str, Any]:
    """preview: 读 sheet 元信息 + 显示将要写入的内容, 不执行写入。"""
    # 拿 sheet 列表确认 sheet_id 有效
    sheets_resp = _api_get(f"/sheets/v3/spreadsheets/{sheet_token}/sheets/query")
    sheets = (sheets_resp or {}).get("sheets") or []
    target = None
    for sh in sheets:
        if sh.get("sheet_id") == sheet_id:
            target = sh
            break
    sp_meta = _api_get(f"/sheets/v3/spreadsheets/{sheet_token}")
    sp_title = (sp_meta or {}).get("spreadsheet", {}).get("title", "")

    if target is None:
        # sheet_id 没指定 → 列出所有供选
        sheet_list = [{"sheet_id": s.get("sheet_id"), "title": s.get("title")} for s in sheets[:5]]
        return {
            "ok": False,
            "preview": True,
            "reason": f"sheet_id '{sheet_id}' 不存在或未指定",
            "spreadsheet_title": sp_title,
            "available_sheets": sheet_list,
        }
    max_cols = max((len(r) for r in rows), default=0)
    return {
        "ok": True,
        "preview": True,
        "action": "append_sheet",
        "target": f"{sp_title} / {target.get('title', sheet_id)}",
        "sheet_id": sheet_id,
        "will_append": f"{len(rows)} 行 × {max_cols} 列",
        "sample_first_row": (rows[0] if rows else []),
        "hint": "确认无误后, 加 confirm=true 再次调用以执行写入",
    }


def _preview_docx_write(doc_token: str, text: str) -> dict[str, Any]:
    """preview: 读 docx 标题 + 显示将追加的文本, 不执行写入。"""
    meta = _api_get(f"/docx/v1/documents/{doc_token}")
    if not meta or not meta.get("document"):
        return {"ok": False, "reason": "无法读 docx 元信息 (权限不足或文档不存在)"}
    title = meta["document"].get("title", "")
    return {
        "ok": True,
        "preview": True,
        "action": "append_docx",
        "target": title,
        "will_append": f"{len(text)} 字 (纯文本段落)",
        "text_preview": text[:200] + ("..." if len(text) > 200 else ""),
        "hint": "确认无误后, 加 confirm=true 再次调用以执行写入",
    }


def _preview_export(obj_token: str, obj_type: str, fmt: str) -> dict[str, Any]:
    """preview: 显示将导出的格式和目标路径, 不执行。"""
    final_fmt = fmt or _EXPORT_TYPE_FMT.get(obj_type, "pdf")
    return {
        "ok": True,
        "preview": True,
        "action": "export",
        "target": f"{obj_token[:16]}... ({obj_type})",
        "format": final_fmt,
        "hint": "确认无误后, 加 confirm=true 再次调用以执行导出",
    }


# ── 写入总入口 ───────────────────────────────────────────────────

def write_feishu_url(
    url: str,
    action: str,
    *,
    confirm: bool = False,
    sheet_id: str = "",
    rows: list[list] | None = None,
    text: str = "",
    fmt: str = "",
) -> dict[str, Any]:
    """写飞书文档 (统一入口)。两步确认: confirm=false 只 preview, confirm=true 才执行。

    action:
      append_sheet — 往 sheet 追加行 (需 sheet_id + rows)
      append_docx  — 往 docx 追加文本段落 (需 text)
      export       — 导出为 PDF/xlsx (需 fmt?, 默认按类型)
    """
    parsed = parse_feishu_url(url)
    kind = parsed.get("type", "")
    token = parsed.get("token", "")
    if not kind:
        return {"ok": False, "reason": f"无法识别飞书链接: {url}"}

    # wiki → 二次解析到 obj
    obj_type = kind
    obj_token = token
    wiki_title = ""
    if kind == "wiki":
        resolved = _resolve_wiki_obj(token)
        if not resolved:
            return {"ok": False, "reason": "wiki 节点解析失败 (权限不足或链接错误)"}
        obj_token = resolved["obj_token"]
        obj_type = resolved["obj_type"]
        wiki_title = resolved.get("title", "")
        if obj_type not in {"sheet", "docx", "bitable"}:
            return {"ok": False, "reason": f"wiki 节点类型 {obj_type} 不支持写入"}

    # ── action 分发 ──
    if action not in {"append_sheet", "append_docx", "export"}:
        return {"ok": False, "reason": f"未知 action: {action} (支持 append_sheet/append_docx/export)"}

    # ── preview 阶段 (confirm=false) ──
    if not confirm:
        if action == "append_sheet":
            if obj_type != "sheet":
                return {"ok": False, "reason": f"目标不是 sheet (obj_type={obj_type}), 无法追加行"}
            if not sheet_id:
                return _preview_sheet_write(obj_token, "", rows or [])
            return _preview_sheet_write(obj_token, sheet_id, rows or [])
        if action == "append_docx":
            if obj_type != "docx":
                return {"ok": False, "reason": f"目标不是 docx (obj_type={obj_type}), 无法追加段落"}
            if not text.strip():
                return {"ok": False, "reason": "text 不能为空"}
            return _preview_docx_write(obj_token, text)
        if action == "export":
            return _preview_export(obj_token, obj_type, fmt)

    # ── confirm=true 执行阶段 ──
    if action == "append_sheet":
        if obj_type != "sheet":
            return {"ok": False, "reason": f"目标不是 sheet (obj_type={obj_type})"}
        if not sheet_id:
            return {"ok": False, "reason": "confirm 阶段必须指定 sheet_id"}
        return _write_sheet(obj_token, sheet_id, rows or [])
    if action == "append_docx":
        if obj_type != "docx":
            return {"ok": False, "reason": f"目标不是 docx (obj_type={obj_type})"}
        if not text.strip():
            return {"ok": False, "reason": "text 不能为空"}
        return _append_docx(obj_token, text)
    if action == "export":
        return _export_doc(obj_token, obj_type, fmt)

    return {"ok": False, "reason": "未处理的分支"}


__all__ = ["read_feishu_url", "parse_feishu_url", "write_feishu_url"]
