"""pnkx 统一登录（OIDC）接入。

单用户家庭中枢：仅允许 ARIA_SSO_ALLOWED_SUB 指定的 pnkx 用户（OIDC sub，
即 pnkx userId）登录，映射到本地唯一 chat 用户。启用条件（环境变量）：
  ARIA_SSO_ISSUER / ARIA_SSO_CLIENT_ID / ARIA_SSO_CLIENT_SECRET / ARIA_SSO_ALLOWED_SUB
四项齐备才启用；本地密码登录始终保留，作为离线降级通道。

流程：/api/v1/auth/sso/login → 302 pnkx 授权页 → 回调换令牌取 userinfo →
校验 sub 白名单 → 建立本地会话 → 返回落地页（写 localStorage 后回首页）。
"""

from __future__ import annotations

import html
import secrets
import time
from dataclasses import dataclass
from typing import Annotated

import httpx
from fastapi import APIRouter, Request, HTTPException, Query, Response
from starlette.responses import HTMLResponse

from app.auth.service import AuthService
from app.db import AppUserRecord

from sqlalchemy import select

_TOKEN_KEY = "ariaChatToken"
_STATE_TTL_SECONDS = 300
# 单实例部署，state 保存在进程内存即可（重启丢未完成跳转，用户重试即可）
_pending_states: dict[str, float] = {}


@dataclass(frozen=True, slots=True)
class SsoSettings:
    issuer: str
    client_id: str
    client_secret: str
    allowed_sub: str

    @property
    def enabled(self) -> bool:
        return all(
            (
                self.issuer,
                self.client_id,
                self.client_secret,
                self.allowed_sub,
            )
        )


def load_sso_settings() -> SsoSettings:
    import os

    return SsoSettings(
        issuer=os.getenv("ARIA_SSO_ISSUER", "").rstrip("/"),
        client_id=os.getenv("ARIA_SSO_CLIENT_ID", ""),
        client_secret=os.getenv("ARIA_SSO_CLIENT_SECRET", ""),
        allowed_sub=os.getenv("ARIA_SSO_ALLOWED_SUB", ""),
    )


def _prune_states(now: float) -> None:
    for state in [k for k, ts in _pending_states.items() if now - ts > _STATE_TTL_SECONDS]:
        _pending_states.pop(state, None)


def create_sso_router(
    service: AuthService,
    settings: SsoSettings,
    *,
    base_url: str = "",
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/auth/sso", tags=["auth-sso"])

    @router.get("/status")
    async def sso_status() -> dict[str, bool]:
        return {"enabled": settings.enabled}

    def _origin(request: Request) -> str:
        """回调地址与登录后落地页按发起请求的来源站点推导，
        使 chat / admin 等同域多入口都能各自完成 SSO 回跳。"""
        host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme
        return f"{proto}://{host}" if host else base_url

    @router.get("/login")
    async def sso_login(request: Request, response: Response) -> None:
        if not settings.enabled:
            raise HTTPException(status_code=404, detail="SSO 未启用")
        now = time.monotonic()
        _prune_states(now)
        state = secrets.token_urlsafe(24)
        _pending_states[state] = now
        authorize_url = (
            f"{settings.issuer}/oauth2/authorize"
            f"?response_type=code&client_id={settings.client_id}"
            f"&redirect_uri={_origin(request)}/api/v1/auth/sso/callback"
            f"&scope=openid%20profile&state={state}"
        )
        response.status_code = 302
        response.headers["Location"] = authorize_url

    @router.get("/callback")
    async def sso_callback(
        request: Request,
        code: Annotated[str | None, Query()] = None,
        state: Annotated[str | None, Query()] = None,
        error: Annotated[str | None, Query()] = None,
    ) -> HTMLResponse:
        if not settings.enabled:
            raise HTTPException(status_code=404, detail="SSO 未启用")
        fail_target = f"{base_url}/#sso-error="
        if error:
            return _landing_failure(fail_target, f"pnkx 授权失败：{error}")
        now = time.monotonic()
        _prune_states(now)
        issued = _pending_states.pop(state, None) if state else None
        if not code or issued is None or now - issued > _STATE_TTL_SECONDS:
            return _landing_failure(fail_target, "SSO 状态校验失败，请重新登录")

        redirect_uri = f"{base_url}/api/v1/auth/sso/callback"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                token_resp = await client.post(
                    f"{settings.issuer}/oauth2/token",
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": redirect_uri,
                    },
                    auth=(settings.client_id, settings.client_secret),
                )
                token_resp.raise_for_status()
                access_token = token_resp.json().get("access_token")
                if not access_token:
                    raise RuntimeError("empty access token")
                userinfo_resp = await client.get(
                    f"{settings.issuer}/userinfo",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                userinfo_resp.raise_for_status()
                userinfo = userinfo_resp.json()
        except Exception as exc:  # noqa: BLE001
            return _landing_failure(fail_target, f"SSO 令牌交换失败：{exc}")

        sub = str(userinfo.get("sub") or "")
        if not sub or sub != settings.allowed_sub:
            return _landing_failure(fail_target, "该 pnkx 账号无权登录本系统")

        async with service._database.sessions() as session:  # noqa: SLF001
            user = (
                await session.execute(
                    select(AppUserRecord).where(AppUserRecord.status == "active")
                )
            ).scalars().first()
        if user is None:
            return _landing_failure(fail_target, "本地账号尚未初始化，请先用密码完成首次设置")

        auth_session = await service.login_sso(user_id=user.id)
        token = auth_session.access_token
        safe_token = html.escape(token, quote=True)
        return HTMLResponse(
            f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>登录成功</title>
<script>
try {{ localStorage.setItem('{_TOKEN_KEY}', '{safe_token}');
     sessionStorage.setItem('{_TOKEN_KEY}', '{safe_token}'); }} catch (e) {{}}
location.replace('{_origin(request) or "/"}/');
</script></head>
<body style="font-family:sans-serif;text-align:center;padding-top:20vh;color:#555">
正在进入 Companion Hub……
</body></html>"""
        )

    def _landing_failure(target: str, message: str) -> HTMLResponse:
        from urllib.parse import quote

        return HTMLResponse(
            f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>登录失败</title>
<script>location.replace('{target}{quote(message)}');</script></head>
<body style="font-family:sans-serif;text-align:center;padding-top:20vh;color:#a33">{html.escape(message)}</body></html>"""
        )

    return router
