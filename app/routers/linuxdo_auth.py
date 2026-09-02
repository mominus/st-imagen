"""LINUX DO Connect OAuth 登录路由：start 发起授权 + callback 完成登录。

回调失败统一 303 到 /?auth_error=<消息>，由前端登录弹窗展示并清理地址栏；
成功则种上用户会话 cookie 后回首页。
"""
from __future__ import annotations

import logging
import secrets
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_session
from app.services import linuxdo_auth
from app.services.user_auth import (
    InvalidInviteCodeError,
    InviteCodeExhaustedError,
    InviteCodeRevokedError,
    UserAuthError,
    UserDisabledError,
    UserExpiredError,
    get_user_auth_service,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth/linuxdo", tags=["user-auth"])


class LinuxDOStartRequest(BaseModel):
    # 首次绑定时消耗；已绑定用户可留空
    invite_code: Optional[str] = Field(default=None, max_length=255)


def _client_ip(request: Request) -> Optional[str]:
    forwarded_for = (request.headers.get("x-forwarded-for") or "").split(",", 1)[0].strip()
    if forwarded_for:
        return forwarded_for
    if request.client and request.client.host:
        return request.client.host
    return None


def _error_redirect(message: str) -> RedirectResponse:
    resp = RedirectResponse(f"/?auth_error={quote(message)}", status_code=303)
    linuxdo_auth.clear_state_cookie(resp)
    return resp


@router.post("/start")
async def start(req: LinuxDOStartRequest, request: Request):
    if not await linuxdo_auth.is_enabled():
        return JSONResponse({"detail": "LINUX DO 登录未启用"}, status_code=404)
    if not linuxdo_auth.is_configured():
        return JSONResponse(
            {"detail": "LINUX DO 登录未配置客户端凭据，请联系管理员"}, status_code=503
        )

    state = secrets.token_urlsafe(24)
    verifier, challenge = linuxdo_auth.make_pkce_pair()
    redirect_uri = linuxdo_auth.resolve_redirect_uri(request)
    try:
        authorize_url = await linuxdo_auth.build_authorize_url(
            state=state, code_challenge=challenge, redirect_uri=redirect_uri
        )
    except linuxdo_auth.LinuxDOOAuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=503)

    resp = JSONResponse({"authorize_url": authorize_url})
    linuxdo_auth.set_state_cookie(
        resp,
        linuxdo_auth.encode_state_payload(
            state=state, verifier=verifier, invite_code=(req.invite_code or "").strip()
        ),
    )
    return resp


@router.get("/callback")
async def callback(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    parsed = linuxdo_auth.decode_state_payload(
        request.cookies.get(linuxdo_auth.STATE_COOKIE_NAME)
    )
    query_state = (request.query_params.get("state") or "").strip()
    code = (request.query_params.get("code") or "").strip()
    if parsed is None or not query_state or not secrets.compare_digest(
        parsed["state"], query_state
    ):
        return _error_redirect("登录状态校验失败，请重新发起 LINUX DO 登录")
    if not code:
        return _error_redirect("LINUX DO 授权未完成，请重试")

    redirect_uri = linuxdo_auth.resolve_redirect_uri(request)
    try:
        access_token = await linuxdo_auth.exchange_code(
            code=code, redirect_uri=redirect_uri, code_verifier=parsed["verifier"]
        )
        profile = await linuxdo_auth.fetch_profile(access_token)
    except linuxdo_auth.LinuxDOOAuthError as exc:
        logger.info("linuxdo oauth callback failed: %s", exc)
        return _error_redirect(str(exc))

    min_level = linuxdo_auth.min_trust_level()
    trust_level = profile.get("trust_level")
    if min_level > 0 and (trust_level is None or trust_level < min_level):
        return _error_redirect("LINUX DO 信任等级不足，无法登录")

    auth = get_user_auth_service()
    try:
        user, raw_token = await auth.login_with_linuxdo(
            session,
            profile=profile,
            invite_code=parsed.get("invite_code") or "",
            ip_address=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
    except (
        InvalidInviteCodeError,
        InviteCodeExhaustedError,
        InviteCodeRevokedError,
        UserDisabledError,
        UserExpiredError,
        UserAuthError,
    ) as exc:
        await session.rollback()
        return _error_redirect(str(exc))
    except Exception:
        logger.exception("linuxdo login failed unexpectedly")
        await session.rollback()
        return _error_redirect("LINUX DO 登录失败，请稍后重试")

    await session.commit()
    resp = RedirectResponse("/", status_code=303)
    auth.set_session_cookie(resp, raw_token)
    linuxdo_auth.clear_state_cookie(resp)
    logger.info(
        "linuxdo login success: user=%s linuxdo_id=%s", user.id[:8], str(profile.get("id"))
    )
    return resp
