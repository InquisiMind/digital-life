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


def _get_app_creds() -> tuple[str, str]:
    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    return app_id, app_secret


async def handle_feishu_oauth_callback(request: web.Request) -> web.Response:
    """GET /oauth/feishu/callback?code=xxx&state={instance_id}"""
    code = request.query.get("code", "")
    instance_id = request.query.get("state", "")

    if not code:
        return web.Response(text="缺少 code 参数", status=400)
    if not instance_id:
        return web.Response(text="缺少 state(instance_id) 参数", status=400)

    app_id, app_secret = _get_app_creds()
    if not app_id or not app_secret:
        return web.Response(text="飞书应用未配置 app_id/app_secret", status=500)

    try:
        # 用 code 换 user_access_token
        resp = httpx.post(
            f"{FEISHU_BASE}/authen/v1/access_token",
            headers={"Content-Type": "application/json"},
            json={
                "grant_type": "authorization_code",
                "code": code,
                "app_id": app_id,
                "app_secret": app_secret,
            },
            timeout=10,
        )
        data = resp.json()
        if data.get("code") != 0:
            error_msg = data.get("msg", "unknown error")
            logger.warning("OAuth token exchange failed: %s", error_msg)
            return web.Response(
                text=f"授权失败: {error_msg}<br><a href='/'>返回</a>",
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
    """生成飞书 OAuth 授权 URL。"""
    app_id, _ = _get_app_creds()
    from urllib.parse import urlencode
    params = urlencode({
        "app_id": app_id,
        "redirect_uri": redirect_uri,
        "state": instance_id,
    })
    return f"https://open.feishu.cn/open-apis/authen/v1/index?{params}"


def register_oauth_routes(app: web.Application, prefix: str = "") -> None:
    """注册 OAuth 路由到 aiohttp app。"""
    app.router.add_get(f"{prefix}/oauth/feishu/callback", handle_feishu_oauth_callback)
    logger.info("OAuth routes registered: /oauth/feishu/callback")
