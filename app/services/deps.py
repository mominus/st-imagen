"""通用依赖：管理员 / 普通用户身份校验。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import User, get_session
from app.services.auth import InvalidTokenError, TokenExpiredError, get_auth_service
from app.services.user_auth import get_user_auth_service, is_user_expired


@dataclass
class GenerationPrincipal:
    kind: str  # user / admin
    user: Optional[User] = None
    admin_payload: Optional[dict] = None

    @property
    def username(self) -> str:
        if self.user is not None:
            return self.user.username
        return str((self.admin_payload or {}).get("username") or "admin")

    @property
    def user_id(self) -> Optional[str]:
        return self.user.id if self.user is not None else None


def _extract_bearer_token(authorization: Optional[str]) -> Optional[str]:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    return token or None


async def require_admin(authorization: Optional[str] = Header(None)) -> dict:
    """从 Authorization: Bearer <jwt> 解析出当前管理员。"""
    token = _extract_bearer_token(authorization)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
        )
    auth = get_auth_service()
    try:
        payload = await auth.verify_token(token)
    except TokenExpiredError:
        raise HTTPException(status_code=401, detail="Token expired")
    except InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    return payload


async def get_optional_admin_payload(authorization: Optional[str]) -> Optional[dict]:
    token = _extract_bearer_token(authorization)
    if not token:
        return None
    auth = get_auth_service()
    try:
        return await auth.verify_token(token)
    except (TokenExpiredError, InvalidTokenError):
        return None


async def get_optional_user(
    request: Request,
    session: AsyncSession = Depends(get_session),
    authorization: Optional[str] = Header(None),
) -> Optional[User]:
    authz_token = _extract_bearer_token(authorization)
    cookie_name = get_user_auth_service().session_cookie_name
    cookie_token = request.cookies.get(cookie_name)
    tokens = []
    if authz_token:
        tokens.append(authz_token)
    if cookie_token and cookie_token not in tokens:
        tokens.append(cookie_token)

    for raw_token in tokens:
        pair = await get_user_auth_service().get_user_by_session_token(session, raw_token)
        if pair is None:
            continue
        user, _sess = pair
        if user.status != "active" or is_user_expired(user):
            return None
        return user
    return None


async def require_user(
    request: Request,
    session: AsyncSession = Depends(get_session),
    authorization: Optional[str] = Header(None),
) -> User:
    user = await get_optional_user(request=request, session=session, authorization=authorization)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或会话已失效")
    return user


async def get_optional_generation_principal(
    request: Request,
    session: AsyncSession = Depends(get_session),
    authorization: Optional[str] = Header(None),
) -> Optional[GenerationPrincipal]:
    admin_payload = await get_optional_admin_payload(authorization)
    if admin_payload is not None:
        return GenerationPrincipal(kind="admin", admin_payload=admin_payload)

    user = await get_optional_user(request=request, session=session, authorization=authorization)
    if user is not None:
        return GenerationPrincipal(kind="user", user=user)
    return None


async def require_generation_principal(
    request: Request,
    session: AsyncSession = Depends(get_session),
    authorization: Optional[str] = Header(None),
) -> GenerationPrincipal:
    principal = await get_optional_generation_principal(
        request=request,
        session=session,
        authorization=authorization,
    )
    if principal is None:
        raise HTTPException(status_code=401, detail="未登录或会话已失效")
    return principal
