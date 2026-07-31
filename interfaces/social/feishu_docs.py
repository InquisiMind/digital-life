"""飞书文档 API 模块 — token 管理 + 读取封装 + 通用请求。

设计:
  - 独立于 social takeover daemon, 不依赖 daemon 内存状态。
  - 复用 social.env 里的 refresh_token, 自己刷新 user_access_token。
  - token 对模型不可见: feishu_call 工具内部调用 _ensure_user_token, 不外泄。

三层:
  1. token 管理: _ensure_user_token / _refresh_token / _persist_refresh
  2. 通用请求:   _api_request(method, path, params, body) — 返回原始 JSON (含 code/msg)
  3. 读取封装:   read_feishu_url(url) — 渲染成文本 (兼容旧 sense 调用, 可选)

权限域 (OAuth scope):
  - wiki:wiki:readonly / docx:document:readonly / sheets:spreadsheet:readonly (读)
  - sheets:spreadsheet (写) / bitable:app (多维表格读写)
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
    """从环境变量 → 实例 secrets.env → 实例 app.yaml 读飞书 app_id + app_secret。

    ContextVar instance_id 必须已设置。读顺序:
      1. 进程 env (master 设置的全局默认)
      2. apps/{iid}/config/secrets.env (secret 类字段)
      3. apps/{iid}/config/app.yaml (app_id 等非敏感字段)
    """
    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")

    if not app_id or not app_secret:
        # fallback 1: 从实例 secrets.env 读
        try:
            from pathlib import Path
            from infrastructure.config import get_instance_dir, get_app_instance_id
            iid = ""
            try:
                iid = get_app_instance_id() or ""
            except Exception:
                pass
            if iid:
                secrets = get_instance_dir(iid) / "config" / "secrets.env"
                if secrets.exists():
                    for line in secrets.read_text(encoding="utf-8").splitlines():
                        line = line.strip()
                        if line.startswith("FEISHU_APP_ID=") and not app_id:
                            app_id = line.split("=", 1)[1].strip()
                        elif line.startswith("FEISHU_APP_SECRET=") and not app_secret:
                            app_secret = line.split("=", 1)[1].strip()
        except Exception:
            pass

    if not app_id or not app_secret:
        # fallback 2: 从实例 app.yaml 读 app_id (app_secret 通常在 secrets.env)
        try:
            import yaml
            from infrastructure.config import get_instance_app_config_path
            cfg_path = get_instance_app_config_path()
            if cfg_path.exists():
                raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
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
    """拿有效的 user_access_token — 缓存 + 自动刷新。返回空串=失败。

    token 对调用方透明: 刷新后 _persist_refresh 把轮换的 refresh_token 写回 social.env。
    feishu_call 工具用这个, 模型永远拿不到 token 值。
    """
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


# ════════════════════════════════════════════════════════════════
# 通用请求 — feishu_call 工具的底层。返回原始 JSON (含 code/msg), 不吞错误。
# ════════════════════════════════════════════════════════════════

def _api_request(
    method: str,
    path: str,
    params: dict | None = None,
    body: dict | None = None,
) -> dict[str, Any]:
    """以 user 身份发任意飞书 API 请求。返回原始 JSON {code, msg, data}。

    与旧 _api_get/_api_post 的区别:
      - 返回完整响应 (含 code/msg), 不在 code!=0 时返回 None
      - 让调用方 (feishu_call 工具) 看到真实错误, 避免误判
    """
    token = _ensure_user_token()
    if not token:
        return {"code": -1, "msg": "user_access_token 获取失败 (social.env 无 refresh_token 或刷新失败)"}
    method = method.upper()
    try:
        kwargs: dict[str, Any] = {
            "headers": {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            "timeout": 20,
        }
        if params:
            kwargs["params"] = params
        if body and method in {"POST", "PUT", "PATCH", "DELETE"}:
            kwargs["json"] = body
        resp = httpx.request(method, f"{FEISHU_BASE}{path}", **kwargs)
        return resp.json()
    except Exception as exc:
        return {"code": -2, "msg": f"{type(exc).__name__}: {exc}"}


# 旧的只读便捷函数 (内部用, read_feishu_url 依赖)
def _api_get(path: str, params: dict | None = None) -> dict | None:
    """以 user 身份 GET 飞书 API。code != 0 返回 None (只读封装用)。"""
    data = _api_request("GET", path, params=params)
    if data.get("code") != 0:
        logger.debug("feishu_docs GET %s code=%s msg=%s",
                     path, data.get("code"), data.get("msg", ""))
        return None
    return data.get("data") or {}


def _resolve_wiki_obj(node_token: str) -> dict[str, str] | None:
    """wiki 节点 → {obj_token, obj_type, title}。读取和 feishu_call 解析都用。

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
    resolved = _resolve_wiki_obj(node_token)
    if not resolved:
        return {"ok": False, "reason": "wiki get_node 失败 (可能权限不足或链接错误)"}
    obj_token = resolved["obj_token"]
    obj_type = resolved["obj_type"]
    title = resolved["title"]
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


# ── 对外读入口 (兼容旧 sense 调用, 可选保留) ─────────────────────

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


# ── 授权检测 (feishu_call 工具的 check_fn 用) ────────────────────

def is_feishu_authorized() -> bool:
    """当前实例是否已 OAuth 授权 (social.env 有 refresh_token)。

    用于 feishu_call 工具的 per-tool gating: 没授权时工具不暴露给模型。
    只读文件, 不发网络请求, 不刷 token — 轻量检测。
    """
    try:
        from infrastructure.config import get_instance_dir, get_app_instance_id
        iid = get_app_instance_id() or ""
        if not iid:
            return False
        env = get_instance_dir(iid) / "config" / "social.env"
        if not env.exists():
            return False
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("FEISHU_USER_REFRESH_TOKEN="):
                return bool(line.split("=", 1)[1].strip())
        return False
    except Exception:
        return False


__all__ = [
    "read_feishu_url",
    "parse_feishu_url",
    "_api_request",
    "_resolve_wiki_obj",
    "is_feishu_authorized",
]
