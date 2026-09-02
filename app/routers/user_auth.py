"""普通用户鉴权路由：邀请码激活 / 登录 / 登出 / 会话状态。"""
from __future__ import annotations

from app.time_utils import utcnow_naive

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
    is_temporary_user,
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
    current = utcnow_naive()
    usage = build_user_usage_snapshot(user)
    auth_kind = str(getattr(user, "auth_kind", "password") or "password")
    linuxdo_id = str(getattr(user, "linuxdo_id", None) or "").strip() or None
    linuxdo_username = getattr(user, "linuxdo_username", None)
    # 兼容认证类型尚未回填、但已经存在 LINUX DO 绑定信息的历史用户。
    is_linuxdo = auth_kind == "linuxdo" or bool(linuxdo_id)
    is_invite_guest = auth_kind == "invite_guest" and not is_linuxdo
    if is_invite_guest:
        display_name = "邀请码访客"
    elif is_linuxdo:
        display_id = str(linuxdo_username or linuxdo_id or user.username).strip()
        display_name = f"@{display_id.lstrip('@')}"
    else:
        display_name = f"@{user.username}"
    return {
        "id": user.id,
        # 邀请码本身是 bearer credential；访客用户名用于后台关联，但不回显给浏览器。
        "username": None if is_invite_guest else user.username,
        "display_name": display_name,
        "auth_kind": auth_kind,
        "linuxdo": (
            {
                "id": linuxdo_id,
                "username": linuxdo_username,
                "trust_level": getattr(user, "linuxdo_trust_level", None),
            }
            if is_linuxdo
            else None
        ),
        "quota_type": "one_time" if is_temporary_user(user) else "daily",
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
    from app.services import linuxdo_auth

    return {
        "authenticated": current_user is not None,
        "user": _user_to_dict(current_user) if current_user else None,
        "linuxdo_enabled": await linuxdo_auth.is_enabled(),
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
    from app.services import linuxdo_auth

    if await linuxdo_auth.is_enabled():
        return JSONResponse(
            {"success": False, "message": "请使用 LINUX DO 登录完成注册"},
            status_code=403,
        )

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
    except (UserDisabledError, UserExpiredError) as exc:
        return JSONResponse({"success": False, "message": str(exc)}, status_code=401)

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
