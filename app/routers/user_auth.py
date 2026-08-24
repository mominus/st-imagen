"""普通用户鉴权路由：邀请码激活 / 登录 / 登出 / 会话状态。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import User, get_session
from app.services.deps import get_optional_user, require_user
from app.services.user_auth import (
    InvalidInviteCodeError,
    InvalidPasswordError,
    InvalidUserCredentialsError,
    InvalidUsernameError,
    InviteCodeExhaustedError,
    InviteCodeRevokedError,
    UserDisabledError,
    UserExpiredError,
    UsernameTakenError,
    build_user_usage_snapshot,
    get_effective_user_status,
    get_user_auth_service,
    is_user_expired,
)


router = APIRouter(prefix="/api/auth", tags=["user-auth"])


class ActivateRequest(BaseModel):
    invite_code: str = Field(min_length=1, max_length=255)
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=128)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class InviteLoginRequest(BaseModel):
    invite_code: str = Field(min_length=1, max_length=255)


def _client_ip(request: Request) -> Optional[str]:
    forwarded_for = (request.headers.get("x-forwarded-for") or "").split(",", 1)[0].strip()
    if forwarded_for:
        return forwarded_for
    if request.client and request.client.host:
        return request.client.host
    return None


def _user_to_dict(user: User) -> dict:
    current = datetime.utcnow()
    usage = build_user_usage_snapshot(user)
    auth_kind = str(getattr(user, "auth_kind", "password") or "password")
    is_invite_guest = auth_kind == "invite_guest"
    return {
        "id": user.id,
        # 邀请码本身是 bearer credential；访客用户名用于后台关联，但不回显给浏览器。
        "username": None if is_invite_guest else user.username,
        "display_name": "邀请码访客" if is_invite_guest else f"@{user.username}",
        "auth_kind": auth_kind,
        "status": user.status,
        "effective_status": get_effective_user_status(user, now=current),
        "is_expired": is_user_expired(user, now=current),
        "daily_quota": user.daily_quota,
        "daily_used": usage["daily_used"],
        "total_requests": user.total_requests,
        "in_flight": usage["in_flight"],
        "quota_remaining": usage["quota_remaining"],
        "max_inflight": user.max_inflight,
        "last_used_at": user.last_used_at.isoformat() if user.last_used_at else None,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        "expires_at": user.expires_at.isoformat() if user.expires_at else None,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


@router.get("/status")
async def status(current_user: Optional[User] = Depends(get_optional_user)):
    return {
        "authenticated": current_user is not None,
        "user": _user_to_dict(current_user) if current_user else None,
    }


@router.get("/me")
async def me(current_user: User = Depends(require_user)):
    return _user_to_dict(current_user)


@router.post("/activate")
async def activate(
    req: ActivateRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    auth = get_user_auth_service()
    try:
        user, raw_token = await auth.activate_with_invite(
            session,
            invite_code=req.invite_code,
            username=req.username,
            password=req.password,
            ip_address=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
    except (InvalidInviteCodeError, InviteCodeExhaustedError, InviteCodeRevokedError) as exc:
        return JSONResponse({"success": False, "message": str(exc)}, status_code=400)
    except (InvalidUsernameError, InvalidPasswordError, UsernameTakenError) as exc:
        return JSONResponse({"success": False, "message": str(exc)}, status_code=400)

    await session.commit()
    resp = JSONResponse({"success": True, "user": _user_to_dict(user)})
    auth.set_session_cookie(resp, raw_token)
    return resp


@router.post("/invite-login")
async def invite_login(
    req: InviteLoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """仅凭邀请码创建访客会话，不要求用户提供账号密码。"""
    auth = get_user_auth_service()
    try:
        user, raw_token = await auth.login_with_invite(
            session,
            invite_code=req.invite_code,
            ip_address=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
    except (InvalidInviteCodeError, InviteCodeExhaustedError, InviteCodeRevokedError) as exc:
        return JSONResponse({"success": False, "message": str(exc)}, status_code=400)

    await session.commit()
    resp = JSONResponse({"success": True, "user": _user_to_dict(user)})
    auth.set_session_cookie(resp, raw_token)
    return resp


@router.post("/login")
async def login(
    req: LoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    auth = get_user_auth_service()
    try:
        user, raw_token = await auth.authenticate(
            session,
            username=req.username,
            password=req.password,
            ip_address=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
    except (InvalidUserCredentialsError, UserDisabledError, UserExpiredError) as exc:
        return JSONResponse({"success": False, "message": str(exc)}, status_code=401)

    await session.commit()
    resp = JSONResponse({"success": True, "user": _user_to_dict(user)})
    auth.set_session_cookie(resp, raw_token)
    return resp


@router.post("/logout")
async def logout(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    auth = get_user_auth_service()
    raw_token = request.cookies.get(auth.session_cookie_name)
    if raw_token:
        await auth.revoke_session_token(session, raw_token)
        await session.commit()
    resp = JSONResponse({"success": True})
    auth.clear_session_cookie(resp)
    return resp
