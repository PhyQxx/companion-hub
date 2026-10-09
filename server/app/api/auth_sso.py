"""pnkx 统一登录（OIDC）接入，多用户白名单开户。

允许 ARIA_SSO_ALLOWED_SUBS（逗号/空白分隔的 pnkx userId 列表，兼容旧单值
ARIA_SSO_ALLOWED_SUB）内的账号登录；OIDC sub 固定映射到 app_user.sso_sub，
名单内首次登录自动开户（ARIA_SSO_OWNER_SUB 或名单首个为业主，其余为成员），
存量未绑定业主由业主 sub 首次登录回填。启用条件（环境变量）：
  ARIA_SSO_ISSUER / ARIA_SSO_CLIENT_ID / ARIA_SSO_CLIENT_SECRET / 名单非空
四项齐备才启用；本地密码登录始终保留，作为业主离线降级通道。

流程：/api/v1/auth/sso/login → 302 pnkx 授权页 → 回调换令牌取 userinfo →
校验 sub 白名单 → 映射/开户本地用户并建立会话 → 返回落地页（写 localStorage
后回首页）。
"""

from __future__ import annotations

import html
import secrets
import time
from dataclasses import dataclass
from typing import Annotated

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, Response
from starlette.responses import HTMLResponse

from app.auth.service import AuthService

_TOKEN_KEY = "ariaChatToken"
_STATE_TTL_SECONDS = 300
# 单实例部署，state 保存在进程内存即可（重启丢未完成跳转，用户重试即可）
_pending_states: dict[str, tuple[float, str]] = {}


@dataclass(frozen=True, slots=True)
class SsoSettings:
    issuer: str
    client_id: str
    client_secret: str
    allowed_subs: tuple[str, ...]
    owner_sub: str

    @property
    def enabled(self) -> bool:
        return bool(
            self.issuer
            and self.client_id
            and self.client_secret
            and self.allowed_subs
        )


def load_sso_settings() -> SsoSettings:
    import os

    raw_subs = os.getenv("ARIA_SSO_ALLOWED_SUBS", "") or os.getenv("ARIA_SSO_ALLOWED_SUB", "")
    allowed_subs = tuple(sub for sub in raw_subs.replace(",", " ").split() if sub)
    owner_sub = os.getenv("ARIA_SSO_OWNER_SUB", "") or (allowed_subs[0] if allowed_subs else "")
    return SsoSettings(
        issuer=os.getenv("ARIA_SSO_ISSUER", "").rstrip("/"),
        client_id=os.getenv("ARIA_SSO_CLIENT_ID", ""),
        client_secret=os.getenv("ARIA_SSO_CLIENT_SECRET", ""),
        allowed_subs=allowed_subs,
        owner_sub=owner_sub,
    )


def _prune_states(now: float) -> None:
    for state in [k for k, entry in _pending_states.items() if now - entry[0] > _STATE_TTL_SECONDS]:
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
        bound_redirect_uri = f"{_origin(request)}/api/v1/auth/sso/callback"
        _pending_states[state] = (now, bound_redirect_uri)
        authorize_url = (
            f"{settings.issuer}/oauth2/authorize"
            f"?response_type=code&client_id={settings.client_id}"
            f"&redirect_uri={bound_redirect_uri}"
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
        fail_target = f"{_origin(request)}/chat/#sso-error="
        if error:
            return _landing_failure(fail_target, f"pnkx 授权失败：{error}")
        now = time.monotonic()
        _prune_states(now)
        entry = _pending_states.pop(state, None) if state else None
        if not code or entry is None or now - entry[0] > _STATE_TTL_SECONDS:
            return _landing_failure(fail_target, "SSO 状态校验失败，请重新登录")
        redirect_uri = entry[1]
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
                if token_resp.status_code >= 400:
                    body = token_resp.text[:200]
                    raise RuntimeError(
                        f"pnkx 返回 {token_resp.status_code}: {body}"
                        f" (redirect_uri={redirect_uri})"
                    )
                access_token = token_resp.json().get("access_token")
                if not access_token:
                    raise RuntimeError("empty access token")
                userinfo_resp = await client.get(
                    f"{settings.issuer}/userinfo",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                userinfo_resp.raise_for_status()
                userinfo = userinfo_resp.json()
        except Exception as exc:
            return _landing_failure(fail_target, f"SSO 令牌交换失败：{exc}")

        sub = str(userinfo.get("sub") or "")
        if not sub or sub not in settings.allowed_subs:
            return _landing_failure(fail_target, "该 pnkx 账号无权登录本系统")

        try:
            auth_session = await service.login_sso_provisioned(
                sub=sub,
                display_name=str(userinfo.get("name") or ""),
                owner_sub=settings.owner_sub,
            )
        except Exception:
            return _landing_failure(fail_target, "该账号已被停用或无法建立会话")
        token = auth_session.access_token
        safe_token = html.escape(token, quote=True)
        return HTMLResponse(
            f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>登录成功</title>
<script>
try {{ localStorage.setItem('{_TOKEN_KEY}', '{safe_token}');
     sessionStorage.setItem('{_TOKEN_KEY}', '{safe_token}'); }} catch (e) {{}}
location.replace('{_origin(request)}/chat/');
</script></head>
<body style="font-family:sans-serif;text-align:center;padding-top:20vh;color:#555">
正在进入 Companion Hub……
</body></html>"""
        )

    def _landing_failure(target: str, message: str) -> HTMLResponse:
        from urllib.parse import quote

        safe_message = html.escape(message)
        return HTMLResponse(
            f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>登录失败</title>
<script>location.replace('{target}{quote(message)}');</script></head>
<body style="font-family:sans-serif;text-align:center;padding-top:20vh;color:#a33">
{safe_message}</body></html>"""
        )

    return router
