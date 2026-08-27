"""管理员认证（JWT + bcrypt）。

借鉴 st-api app/services/auth.py 的精简版：
- 仅保留登录、verify token、change password、ensure_default_admin。
"""
from __future__ import annotations

from app.time_utils import utcnow_naive

import os
import uuid
from datetime import timedelta
from typing import Any, Dict, Optional, Tuple

import jwt
from sqlalchemy import select

from app.env import PLACEHOLDER_ADMIN_PASSWORDS
from app.models.database import Admin, get_session_factory
from app.services.crypto import CryptoService


class AuthError(Exception):
    pass


class InvalidCredentialsError(AuthError):
    pass


class TokenExpiredError(AuthError):
    pass


class InvalidTokenError(AuthError):
    pass


class AuthService:
    def __init__(self) -> None:
        self._jwt_secret = (os.getenv("JWT_SECRET_KEY") or "").strip()
        self._jwt_expire_hours = int(os.getenv("JWT_EXPIRE_HOURS", "24"))
        self._default_username = (os.getenv("ADMIN_USERNAME") or "").strip() or "admin"
        self._default_password = os.getenv("ADMIN_PASSWORD") or ""

    # -------- token --------
    def _require_jwt_secret(self) -> str:
        if not self._jwt_secret:
            raise RuntimeError("JWT_SECRET_KEY 未设置")
        return self._jwt_secret

    def _has_safe_default_password(self) -> bool:
        password = self._default_password
        return bool(password) and password not in PLACEHOLDER_ADMIN_PASSWORDS

    def generate_token(self, admin_id: str, username: str, *, token_version: int) -> str:
        now = utcnow_naive()
        payload = {
            "sub": admin_id,
            "username": username,
            "ver": max(0, int(token_version)),
            "iat": now,
            "exp": now + timedelta(hours=self._jwt_expire_hours),
        }
        return jwt.encode(payload, self._require_jwt_secret(), algorithm="HS256")

    def decode_token(self, token: str) -> Dict[str, Any]:
        try:
            return jwt.decode(token, self._require_jwt_secret(), algorithms=["HS256"])
        except jwt.ExpiredSignatureError as exc:
            raise TokenExpiredError("Token 已过期") from exc
        except jwt.InvalidTokenError as exc:
            raise InvalidTokenError(f"Token 无效: {exc}") from exc

    async def verify_token(self, token: str) -> Dict[str, Any]:
        payload = self.decode_token(token)
        admin_id = str(payload.get("sub") or "").strip()
        if not admin_id:
            raise InvalidTokenError("Token 缺少管理员标识")

        factory = get_session_factory()
        async with factory() as session:
            row = await session.execute(select(Admin).where(Admin.id == admin_id))
            admin = row.scalar_one_or_none()
        if admin is None:
            raise InvalidTokenError("管理员不存在")

        token_version = int(payload.get("ver") or 0)
        current_version = max(0, int(admin.token_version or 0))
        if token_version != current_version:
            raise InvalidTokenError("Token 已失效，请重新登录")

        payload["username"] = admin.username
        payload["ver"] = current_version
        return payload

    # -------- 登录 --------
    async def authenticate(self, username: str, password: str) -> Tuple[str, Dict[str, Any]]:
        factory = get_session_factory()
        async with factory() as session:
            row = await session.execute(select(Admin).where(Admin.username == username))
            admin = row.scalar_one_or_none()
            if admin is None or not CryptoService.verify_password(password, admin.password_hash):
                raise InvalidCredentialsError("用户名或密码错误")
            token = self.generate_token(
                admin.id,
                admin.username,
                token_version=max(0, int(admin.token_version or 0)),
            )
            return token, {
                "id": admin.id,
                "username": admin.username,
                "created_at": admin.created_at.isoformat() if admin.created_at else None,
            }

    # -------- 改密 --------
    async def change_password(
        self, admin_id: str, old_password: str, new_password: str
    ) -> bool:
        if len(new_password or "") < 12 or len((new_password or "").encode("utf-8")) > 72:
            raise ValueError("新密码必须为 12 至 72 字节")
        factory = get_session_factory()
        async with factory() as session:
            row = await session.execute(select(Admin).where(Admin.id == admin_id))
            admin = row.scalar_one_or_none()
            if admin is None or not CryptoService.verify_password(old_password, admin.password_hash):
                return False
            admin.password_hash = CryptoService.hash_password(new_password)
            admin.token_version = max(0, int(admin.token_version or 0)) + 1
            await session.commit()
            return True

    # -------- 初始化默认管理员 --------
    async def ensure_default_admin(self) -> None:
        factory = get_session_factory()
        async with factory() as session:
            row = await session.execute(select(Admin))
            if row.scalar_one_or_none() is not None:
                return
            if not self._default_password:
                raise RuntimeError("ADMIN_PASSWORD 未设置，无法初始化默认管理员")
            if not self._has_safe_default_password():
                raise RuntimeError("ADMIN_PASSWORD 仍是默认/占位值，无法初始化默认管理员")
            admin = Admin(
                id=str(uuid.uuid4()),
                username=self._default_username,
                password_hash=CryptoService.hash_password(self._default_password),
                token_version=0,
                created_at=utcnow_naive(),
            )
            session.add(admin)
            await session.commit()


_auth_service: Optional[AuthService] = None


def get_auth_service() -> AuthService:
    global _auth_service
    if _auth_service is None:
        _auth_service = AuthService()
    return _auth_service
