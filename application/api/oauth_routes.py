"""飞书 OAuth 回调 — 接收授权码, 换取 user_access_token + refresh_token。

流程:
  1. zhp 访问飞书授权页(浏览器)
  2. 飞书 302 → /oauth/feishu/callback?code=xxx&state={instance_id}
  3. 本 handler 用 code 换 token
  4. 持久化到 apps/{iid}/config/social.env
  5. 返回"授权成功"页面
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx
from aiohttp import web

logger = logging.getLogger(__name__)

FEISHU_BASE = "https://open.feishu.cn/open-apis"


def _get_app_creds(instance_id: str = "") -> tuple[str, str]:
    """从实例 config 读飞书 app_id + app_secret。

    Master 进程没有实例 context(env 没加载), 所以需要 explicit iid 参数。
    """
    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")

    # fallback: 从实例的 secrets.env / app.yaml 读
    if (not app_id or not app_secret) and instance_id:
        from pathlib import Path
        secrets = Path("apps") / instance_id / "config" / "secrets.env"
        if secrets.exists():
            for line in secrets.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("FEISHU_APP_ID=") and not app_id:
                    app_id = line.split("=", 1)[1].strip()
                elif line.startswith("FEISHU_APP_SECRET=") and not app_secret:
                    app_secret = line.split("=", 1)[1].strip()
        if not app_id:
            try:
                import yaml
                cfg_path = Path("apps") / instance_id / "config" / "app.yaml"
                if cfg_path.exists():
                    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
                    feishu = (raw.get("channels") or {}).get("feishu") or {}
                    app_id = app_id or feishu.get("app_id", "")
            except Exception:
                pass

    return app_id, app_secret


async def handle_feishu_oauth_callback(request: web.Request) -> web.Response:
    """GET /oauth/feishu/callback?code=xxx&state={instance_id}"""
    code = request.query.get("code", "")
    instance_id = request.query.get("state", "")

    if not code:
        return web.Response(text="缺少 code 参数", status=400)
    if not instance_id:
        return web.Response(text="缺少 state(instance_id) 参数", status=400)

    app_id, app_secret = _get_app_creds(instance_id)
    if not app_id or not app_secret:
        return web.Response(text="飞书应用未配置 app_id/app_secret (检查 secrets.env)", status=500)

    try:
        # 用 code 换 user_access_token (Feishu OIDC)
        # 步骤: 先拿 tenant_access_token → 用它换 user tokens
        tenant_resp = httpx.post(
            f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
            headers={"Content-Type": "application/json"},
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=10,
        )
        tenant_tok = tenant_resp.json().get("tenant_access_token", "")
        if not tenant_tok:
            return web.Response(
                text=f"获取 tenant_access_token 失败: {tenant_resp.text}<br><a href='/'>返回</a>",
                content_type="text/html",
                status=500,
            )

        resp = httpx.post(
            f"{FEISHU_BASE}/authen/v1/oidc/access_token",
            headers={
                "Authorization": f"Bearer {tenant_tok}",
                "Content-Type": "application/json",
            },
            json={
                "grant_type": "authorization_code",
                "code": code,
            },
            timeout=10,
        )
        data = resp.json()
        if data.get("code") != 0:
            err = data.get("msg", "unknown error")
            logger.warning("OIDC token exchange failed: %s", err)
            return web.Response(
                text=f"授权失败: {err}<br><a href='/'>返回</a>",
                content_type="text/html",
                status=500,
            )

        token_data = data.get("data") or {}
        access_token = token_data.get("access_token", "")
        refresh_token = token_data.get("refresh_token", "")

        if not access_token or not refresh_token:
            return web.Response(text="返回的 token 为空", status=500)

        # 持久化
        social_env = Path("apps") / instance_id / "config" / "social.env"
        social_env.parent.mkdir(parents=True, exist_ok=True)
        social_env.write_text(
            f"FEISHU_USER_ACCESS_TOKEN={access_token}\n"
            f"FEISHU_USER_REFRESH_TOKEN={refresh_token}\n",
            encoding="utf-8",
        )

        logger.info("OAuth success: tokens saved for instance %s", instance_id[:8])

        # 接管激活: 自动创建 personal_assistant 项目 + 日常待办
        try:
            from domain.lifecycle.schema import activate_personal_assistant
            activate_personal_assistant(instance_id)
        except Exception as exc:
            logger.warning("activate_personal_assistant failed: %s", exc)

        return web.Response(
            text=(
                "<html><body style='font-family:sans-serif;padding:40px;'>"
                "<h2>✅ 飞书社交接管授权成功</h2>"
                "<p>Token 已保存。重启后社交接管模块将自动启动。</p>"
                f"<p>实例: {instance_id[:8]}...</p>"
                "<p><a href='/'>返回控制台</a></p>"
                "</body></html>"
            ),
            content_type="text/html",
        )
    except Exception as exc:
        logger.exception("OAuth callback exception: %s", exc)
        return web.Response(
            text=f"授权过程异常: {exc}<br><a href='/'>返回</a>",
            content_type="text/html",
            status=500,
        )


def get_oauth_url(instance_id: str, redirect_uri: str = "http://localhost:8642/oauth/feishu/callback") -> str:
    """生成飞书 OAuth 授权 URL (v1 authorize + OIDC scope)。

    scope 必须显式声明, 否则 OIDC 返的 user_access_token 只有基础 scope,
    拉 /im/v1/messages 会 99991679 (权限不足)。

    需要 scope:
      im:chat:readonly    - 读群列表
      im:message          - 读消息 + 收发消息
      im:message.group_at_msg:readonly - 群里 @ 消息读
      im:resource         - 附件/图片
      contact:user.base:readonly - 用户信息(查 sender name)
    """
    app_id, _ = _get_app_creds(instance_id)
    if not app_id:
        logger.warning("get_oauth_url: app_id empty for instance %s", instance_id[:8])
    from urllib.parse import urlencode
    params = urlencode({
        "app_id": app_id,
        "redirect_uri": redirect_uri,
        "state": instance_id,
        # scope 用空格分隔(飞书 OIDC 标准)
        # IM 类: 读群/消息/资源/联系人
        # V6 全接管: 加 group_msg/p2p_msg get_as_user (以用户身份读全量消息)
        #         + search:message (搜索消息, 发现私聊对象)
        # 文档类: 以用户身份读 wiki/docx/sheet (sense_feishu_doc 工具用)
        "scope": (
            "im:chat:readonly "
            "im:message "
            "im:message.group_at_msg:readonly "
            "im:message.group_msg:get_as_user "    # 全量群消息 (不需@bot)
            "im:message.p2p_msg:get_as_user "      # 全量私聊消息
            "im:resource "
            "contact:user.base:readonly "
            "bitable:app "
            "wiki:wiki:readonly "                  # 读 wiki 节点
            "docx:document:readonly "              # 读 docx 文档
            "sheets:spreadsheet:readonly"          # 读电子表格
        ),
    })
    return f"https://open.feishu.cn/open-apis/authen/v1/authorize?{params}"


def register_oauth_routes(app: web.Application, prefix: str = "") -> None:
    """注册 OAuth 路由到 aiohttp app。"""
    app.router.add_get(f"{prefix}/oauth/feishu/callback", handle_feishu_oauth_callback)
    logger.info("OAuth routes registered: /oauth/feishu/callback")
