"""普通用户鉴权：邀请码激活 + 本地账号 + 服务端会话。"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from fastapi import Response
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import InviteCode, User, UserSession
from app.services.crypto import CryptoService


_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")
logger = logging.getLogger(__name__)


class UserAuthError(Exception):
    pass


class InvalidInviteCodeError(UserAuthError):
    pass


class InviteCodeExhaustedError(UserAuthError):
    pass


class InviteCodeRevokedError(UserAuthError):
    pass


class UsernameTakenError(UserAuthError):
    pass


class InvalidUserCredentialsError(UserAuthError):
    pass


class UserDisabledError(UserAuthError):
    pass


class UserExpiredError(UserAuthError):
    pass


class UserQuotaExceededError(UserAuthError):
    pass


class UserConcurrencyExceededError(UserAuthError):
    pass


class InvalidUsernameError(UserAuthError):
    pass


class InvalidPasswordError(UserAuthError):
    pass


def build_user_usage_snapshot(user: User, *, now: Optional[datetime] = None) -> dict:
    current = now or datetime.utcnow()
    same_day = bool(user.last_used_at and user.last_used_at.date() == current.date())
    daily_used = max(0, int(user.daily_used or 0)) if same_day else 0
    in_flight = max(0, int(user.in_flight or 0))
    daily_quota = max(0, int(user.daily_quota or 0))
    quota_remaining = None if daily_quota <= 0 else max(0, daily_quota - daily_used - in_flight)
    return {
        "daily_used": daily_used,
        "in_flight": in_flight,
        "quota_remaining": quota_remaining,
    }


def is_user_expired(user: Optional[User], *, now: Optional[datetime] = None) -> bool:
    if user is None:
        return False
    expires_at = getattr(user, "expires_at", None)
    if expires_at is None:
        return False
    return expires_at <= (now or datetime.utcnow())


def get_effective_user_status(user: User, *, now: Optional[datetime] = None) -> str:
    if user.status != "active":
        return "disabled"
    if is_user_expired(user, now=now):
        return "expired"
    return "active"


class UserAuthService:
    _activation_lock: asyncio.Lock = asyncio.Lock()
    _usage_lock: asyncio.Lock = asyncio.Lock()
    _schema_lock: asyncio.Lock = asyncio.Lock()

    def __init__(self) -> None:
        self._session_cookie_name = os.getenv("USER_SESSION_COOKIE_NAME", "st_imagen_session")
        self._session_days = max(1, int(os.getenv("USER_SESSION_DAYS", "30")))
        self._session_secure = os.getenv("USER_SESSION_SECURE", "true").lower() in {
            "1",
            "true",
            "yes",
        }
        self._session_samesite = (os.getenv("USER_SESSION_SAMESITE", "lax") or "lax").lower()
        self._session_domain = (os.getenv("USER_SESSION_DOMAIN") or "").strip() or None
        self._default_user_daily_quota = max(
            0,
            int(os.getenv("DEFAULT_USER_DAILY_QUOTA", "0")),
        )
        self._default_user_max_inflight = max(
            1,
            int(os.getenv("DEFAULT_USER_MAX_INFLIGHT", "2")),
        )
        self._user_schema_checked = False

    @property
    def session_cookie_name(self) -> str:
        return self._session_cookie_name

    @property
    def session_max_age(self) -> int:
        return self._session_days * 24 * 60 * 60

    @staticmethod
    def normalize_username(username: str) -> str:
        return (username or "").strip().lower()

    @staticmethod
    def _require_valid_password(password: str) -> None:
        raw = password or ""
        if len(raw) < 8:
            raise InvalidPasswordError("密码至少 8 位")
        if len(raw) > 128:
            raise InvalidPasswordError("密码不能超过 128 位")

    def _require_valid_username(self, username: str) -> str:
        normalized = self.normalize_username(username)
        if not _USERNAME_RE.fullmatch(normalized):
            raise InvalidUsernameError(
                "用户名需为 3-32 位小写字母、数字、点、下划线或中划线"
            )
        return normalized

    @staticmethod
    def _generate_batch_username(existing_usernames: set[str]) -> str:
        for _ in range(128):
            candidate = f"user-{secrets.token_hex(4)}"
            if candidate not in existing_usernames:
                return candidate
        raise RuntimeError("自动生成用户名失败，请重试")

    @staticmethod
    def _generate_batch_password(length: int = 12) -> str:
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
        return "".join(secrets.choice(alphabet) for _ in range(max(8, length)))

    @staticmethod
    def _generate_invite_guest_username() -> str:
        # 仅作为数据库内部主键替代值，绝不展示或返回给访客。
        return f"guest-{secrets.token_hex(12)}"

    @staticmethod
    def _make_invite_code() -> str:
        return "sti_" + secrets.token_urlsafe(18)

    def _invite_defaults(
        self,
        *,
        daily_quota: Optional[int],
        max_inflight: Optional[int],
    ) -> Tuple[int, int]:
        quota = self._default_user_daily_quota if daily_quota is None else max(0, int(daily_quota))
        inflight = (
            self._default_user_max_inflight
            if max_inflight is None
            else max(1, int(max_inflight))
        )
        return quota, inflight

    @staticmethod
    def _normalize_user_status(status: Optional[str]) -> str:
        return "disabled" if status == "disabled" else "active"

    @staticmethod
    def _ensure_user_can_access(user: User, *, now: Optional[datetime] = None) -> None:
        current = now or datetime.utcnow()
        if user.status != "active":
            raise UserDisabledError("账号已停用")
        if is_user_expired(user, now=current):
            raise UserExpiredError("账号已过期")

    async def _create_user_unlocked(
        self,
        session: AsyncSession,
        *,
        username: str,
        password: str,
        status: str = "active",
        daily_quota: Optional[int] = None,
        max_inflight: Optional[int] = None,
        expires_at: Optional[datetime] = None,
        existing_usernames: Optional[set[str]] = None,
    ) -> User:
        normalized_username = self._require_valid_username(username)
        self._require_valid_password(password)
        normalized_status = self._normalize_user_status(status)
        quota, inflight = self._invite_defaults(
            daily_quota=daily_quota,
            max_inflight=max_inflight,
        )

        if existing_usernames is None:
            existing = await session.execute(
                select(User).where(User.username == normalized_username)
            )
            if existing.scalar_one_or_none() is not None:
                raise UsernameTakenError("用户名已存在")
        elif normalized_username in existing_usernames:
            raise UsernameTakenError("用户名已存在")

        user = User(
            id=str(uuid.uuid4()),
            username=normalized_username,
            password_hash=CryptoService.hash_password(password),
            status=normalized_status,
            invite_code_id=None,
            daily_quota=quota,
            daily_used=0,
            total_requests=0,
            last_used_at=None,
            last_login_at=None,
            expires_at=expires_at,
            in_flight=0,
            max_inflight=inflight,
        )
        session.add(user)
        try:
            await session.flush()
        except IntegrityError as exc:
            if "users.username" in str(exc).lower() or "unique constraint failed: users.username" in str(exc).lower():
                raise UsernameTakenError("用户名已存在") from exc
            raise

        if existing_usernames is not None:
            existing_usernames.add(normalized_username)
        return user

    async def ensure_user_schema(self, session: AsyncSession) -> None:
        if self._user_schema_checked:
            return

        async with self._schema_lock:
            if self._user_schema_checked:
                return

            res = await session.execute(text("PRAGMA table_info(users)"))
            cols = {row[1] for row in res.fetchall()}
            if "expires_at" not in cols:
                await session.execute(text("ALTER TABLE users ADD COLUMN expires_at DATETIME"))
                logger.warning("schema auto-migrated on demand: users.expires_at added")

            self._user_schema_checked = True

    def set_session_cookie(self, response: Response, raw_token: str) -> None:
        response.set_cookie(
            key=self._session_cookie_name,
            value=raw_token,
            max_age=self.session_max_age,
            httponly=True,
            secure=self._session_secure,
            samesite=self._session_samesite,
            path="/",
            domain=self._session_domain,
        )

    def clear_session_cookie(self, response: Response) -> None:
        response.delete_cookie(
            key=self._session_cookie_name,
            path="/",
            domain=self._session_domain,
        )

    async def create_invite_codes(
        self,
        session: AsyncSession,
        *,
        count: int,
        max_uses: int = 1,
        note: Optional[str] = None,
        expires_at: Optional[datetime] = None,
        daily_quota: Optional[int] = None,
        max_inflight: Optional[int] = None,
    ) -> List[Tuple[InviteCode, str]]:
        count = max(1, min(200, int(count)))
        max_uses = max(1, min(1000, int(max_uses)))
        quota, inflight = self._invite_defaults(
            daily_quota=daily_quota,
            max_inflight=max_inflight,
        )

        created: List[Tuple[InviteCode, str]] = []
        for _ in range(count):
            raw_code = self._make_invite_code()
            invite = InviteCode(
                id=str(uuid.uuid4()),
                code_hash=CryptoService.hash_api_key(raw_code),
                code_prefix=raw_code[:12],
                code_suffix=raw_code[-4:],
                note=(note or "").strip() or None,
                max_uses=max_uses,
                used_count=0,
                daily_quota=quota,
                max_inflight=inflight,
                expires_at=expires_at,
            )
            session.add(invite)
            created.append((invite, raw_code))

        await session.flush()
        return created

    async def list_invite_codes(self, session: AsyncSession) -> List[InviteCode]:
        result = await session.execute(select(InviteCode).order_by(InviteCode.created_at.desc()))
        return list(result.scalars().all())

    async def get_invite_code(self, session: AsyncSession, invite_id: str) -> Optional[InviteCode]:
        result = await session.execute(select(InviteCode).where(InviteCode.id == invite_id))
        return result.scalar_one_or_none()

    async def revoke_invite_code(self, session: AsyncSession, invite_id: str) -> Optional[InviteCode]:
        invite = await self.get_invite_code(session, invite_id)
        if invite is None:
            return None
        invite.revoked_at = datetime.utcnow()
        invite.updated_at = datetime.utcnow()
        await session.flush()
        return invite

    async def delete_invite_code(self, session: AsyncSession, invite_id: str) -> bool:
        invite = await self.get_invite_code(session, invite_id)
        if invite is None:
            return False

        now = datetime.utcnow()
        await session.execute(
            update(User)
            .where(User.invite_code_id == invite.id)
            .values(invite_code_id=None, updated_at=now)
        )
        await session.delete(invite)
        await session.flush()
        return True

    async def delete_all_invite_codes(self, session: AsyncSession) -> int:
        now = datetime.utcnow()
        await session.execute(
            update(User)
            .where(User.invite_code_id.is_not(None))
            .values(invite_code_id=None, updated_at=now)
        )
        result = await session.execute(delete(InviteCode))
        await session.flush()
        return int(result.rowcount or 0)

    async def list_users(self, session: AsyncSession) -> List[User]:
        await self.ensure_user_schema(session)
        result = await session.execute(select(User).order_by(User.created_at.desc()))
        return list(result.scalars().all())

    async def get_user(self, session: AsyncSession, user_id: str) -> Optional[User]:
        await self.ensure_user_schema(session)
        result = await session.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()

    async def create_user(
        self,
        session: AsyncSession,
        *,
        username: str,
        password: str,
        status: str = "active",
        daily_quota: Optional[int] = None,
        max_inflight: Optional[int] = None,
        expires_at: Optional[datetime] = None,
    ) -> User:
        await self.ensure_user_schema(session)
        async with self._activation_lock:
            return await self._create_user_unlocked(
                session,
                username=username,
                password=password,
                status=status,
                daily_quota=daily_quota,
                max_inflight=max_inflight,
                expires_at=expires_at,
            )

    async def create_users_batch(
        self,
        session: AsyncSession,
        *,
        count: int,
        status: str = "active",
        daily_quota: Optional[int] = None,
        max_inflight: Optional[int] = None,
        expires_at: Optional[datetime] = None,
    ) -> List[Tuple[User, str]]:
        await self.ensure_user_schema(session)
        normalized_status = self._normalize_user_status(status)
        quota, inflight = self._invite_defaults(
            daily_quota=daily_quota,
            max_inflight=max_inflight,
        )
        safe_count = max(1, int(count))

        async with self._activation_lock:
            existing_rows = await session.execute(select(User.username))
            existing_usernames = {
                str(row[0]).strip().lower()
                for row in existing_rows.all()
                if str(row[0] or "").strip()
            }

            created: List[Tuple[User, str]] = []
            for _ in range(safe_count):
                for _attempt in range(32):
                    username = self._generate_batch_username(existing_usernames)
                    password = self._generate_batch_password()
                    try:
                        user = await self._create_user_unlocked(
                            session,
                            username=username,
                            password=password,
                            status=normalized_status,
                            daily_quota=quota,
                            max_inflight=inflight,
                            expires_at=expires_at,
                            existing_usernames=existing_usernames,
                        )
                    except UsernameTakenError:
                        continue
                    created.append((user, password))
                    break
                else:
                    raise RuntimeError("自动生成用户名失败，请重试")
            return created

    async def update_user(
        self,
        session: AsyncSession,
        user_id: str,
        *,
        status: Optional[str] = None,
        daily_quota: Optional[int] = None,
        max_inflight: Optional[int] = None,
        expires_at: Optional[datetime] = None,
        expires_at_provided: bool = False,
        new_password: Optional[str] = None,
    ) -> Optional[User]:
        user = await self.get_user(session, user_id)
        if user is None:
            return None

        now = datetime.utcnow()
        should_revoke_sessions = False
        if status in {"active", "disabled"}:
            user.status = status
            if status == "disabled":
                should_revoke_sessions = True
        if daily_quota is not None:
            user.daily_quota = max(0, int(daily_quota))
        if max_inflight is not None:
            user.max_inflight = max(1, int(max_inflight))
        if expires_at_provided:
            user.expires_at = expires_at
            if expires_at is not None and expires_at <= now:
                should_revoke_sessions = True
        if new_password is not None:
            self._require_valid_password(new_password)
            user.password_hash = CryptoService.hash_password(new_password)
            should_revoke_sessions = True
        user.updated_at = now
        await session.flush()

        if should_revoke_sessions:
            await self._revoke_active_sessions_for_user(session, user.id)
        return user

    async def delete_user(self, session: AsyncSession, user_id: str) -> bool:
        user = await self.get_user(session, user_id)
        if user is None:
            return False

        await session.execute(delete(UserSession).where(UserSession.user_id == user.id))
        await session.delete(user)
        await session.flush()
        return True

    async def delete_all_users(self, session: AsyncSession) -> int:
        await session.execute(delete(UserSession))
        result = await session.execute(delete(User))
        await session.flush()
        return int(result.rowcount or 0)

    async def activate_with_invite(
        self,
        session: AsyncSession,
        *,
        invite_code: str,
        username: str,
        password: str,
        ip_address: Optional[str],
        user_agent: Optional[str],
    ) -> Tuple[User, str]:
        await self.ensure_user_schema(session)
        normalized_username = self._require_valid_username(username)
        self._require_valid_password(password)
        now = datetime.utcnow()
        code_hash = CryptoService.hash_api_key((invite_code or "").strip())

        async with self._activation_lock:
            existing = await session.execute(
                select(User).where(User.username == normalized_username)
            )
            if existing.scalar_one_or_none() is not None:
                raise UsernameTakenError("用户名已存在")

            row = await session.execute(
                select(InviteCode).where(InviteCode.code_hash == code_hash)
            )
            invite = row.scalar_one_or_none()
            if invite is None:
                raise InvalidInviteCodeError("邀请码无效")
            if invite.revoked_at is not None:
                raise InviteCodeRevokedError("邀请码已失效")
            if invite.expires_at and invite.expires_at < now:
                raise InvalidInviteCodeError("邀请码已过期")
            if invite.used_count >= invite.max_uses:
                raise InviteCodeExhaustedError("邀请码已达到使用上限")

            user = User(
                id=str(uuid.uuid4()),
                username=normalized_username,
                password_hash=CryptoService.hash_password(password),
                status="active",
                auth_kind="password",
                invite_code_id=invite.id,
                daily_quota=max(0, int(invite.daily_quota or 0)),
                daily_used=0,
                total_requests=0,
                last_login_at=now,
                in_flight=0,
                max_inflight=max(1, int(invite.max_inflight or self._default_user_max_inflight)),
            )
            invite.used_count += 1
            invite.updated_at = now
            session.add(user)
            raw_token = await self._create_session(
                session,
                user=user,
                ip_address=ip_address,
                user_agent=user_agent,
            )
            await session.flush()
            return user, raw_token

    async def login_with_invite(
        self,
        session: AsyncSession,
        *,
        invite_code: str,
        ip_address: Optional[str],
        user_agent: Optional[str],
    ) -> Tuple[User, str]:
        """使用邀请码创建无用户名密码的访客会话。"""
        await self.ensure_user_schema(session)
        now = datetime.utcnow()
        code_hash = CryptoService.hash_api_key((invite_code or "").strip())

        async with self._activation_lock:
            row = await session.execute(
                select(InviteCode).where(InviteCode.code_hash == code_hash)
            )
            invite = row.scalar_one_or_none()
            if invite is None:
                raise InvalidInviteCodeError("邀请码无效")
            if invite.revoked_at is not None:
                raise InviteCodeRevokedError("邀请码已失效")
            if invite.expires_at and invite.expires_at < now:
                raise InvalidInviteCodeError("邀请码已过期")
            if invite.used_count >= invite.max_uses:
                raise InviteCodeExhaustedError("邀请码已达到使用上限")

            user = User(
                id=str(uuid.uuid4()),
                username=self._generate_invite_guest_username(),
                password_hash=CryptoService.hash_password(secrets.token_urlsafe(32)),
                status="active",
                auth_kind="invite_guest",
                invite_code_id=invite.id,
                daily_quota=max(0, int(invite.daily_quota or 0)),
                daily_used=0,
                total_requests=0,
                last_login_at=now,
                in_flight=0,
                max_inflight=max(1, int(invite.max_inflight or self._default_user_max_inflight)),
            )
            invite.used_count += 1
            invite.updated_at = now
            session.add(user)
            raw_token = await self._create_session(
                session,
                user=user,
                ip_address=ip_address,
                user_agent=user_agent,
            )
            await session.flush()
            return user, raw_token

    async def authenticate(
        self,
        session: AsyncSession,
        *,
        username: str,
        password: str,
        ip_address: Optional[str],
        user_agent: Optional[str],
    ) -> Tuple[User, str]:
        await self.ensure_user_schema(session)
        normalized_username = self.normalize_username(username)
        row = await session.execute(select(User).where(User.username == normalized_username))
        user = row.scalar_one_or_none()
        if user is None or not CryptoService.verify_password(password, user.password_hash):
            raise InvalidUserCredentialsError("用户名或密码错误")
        now = datetime.utcnow()
        self._ensure_user_can_access(user, now=now)
        user.last_login_at = now
        user.updated_at = now
        raw_token = await self._create_session(
            session,
            user=user,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await session.flush()
        return user, raw_token

    async def _create_session(
        self,
        session: AsyncSession,
        *,
        user: User,
        ip_address: Optional[str],
        user_agent: Optional[str],
    ) -> str:
        now = datetime.utcnow()
        raw_token = secrets.token_urlsafe(32)
        sess = UserSession(
            id=str(uuid.uuid4()),
            user_id=user.id,
            token_hash=CryptoService.hash_api_key(raw_token),
            ip_address=(ip_address or "").strip() or None,
            user_agent=(user_agent or "").strip() or None,
            created_at=now,
            expires_at=now + timedelta(days=self._session_days),
            last_seen_at=now,
            revoked_at=None,
        )
        session.add(sess)
        await session.flush()
        return raw_token

    async def get_user_by_session_token(
        self,
        session: AsyncSession,
        raw_token: str,
    ) -> Optional[Tuple[User, UserSession]]:
        """按 session token 取用户，保持请求路径只读。

        高并发下，鉴权依赖会出现在 `/api/auth/status`、`/api/recent-images`、
        `/api/reference-url/validate` 等本来不应持有写锁的路径上。
        这里不再顺手更新 `last_seen_at/revoked_at`，避免认证请求也开启写事务。
        """
        token = (raw_token or "").strip()
        if not token:
            return None

        await self.ensure_user_schema(session)
        token_hash = CryptoService.hash_api_key(token)
        row = await session.execute(
            select(UserSession, User)
            .join(User, User.id == UserSession.user_id)
            .where(UserSession.token_hash == token_hash)
        )
        pair = row.first()
        if pair is None:
            return None

        sess, user = pair
        now = datetime.utcnow()
        if sess.revoked_at is not None or sess.expires_at <= now:
            return None
        if user.status != "active" or is_user_expired(user, now=now):
            return None

        return user, sess

    async def revoke_session_token(self, session: AsyncSession, raw_token: str) -> bool:
        token = (raw_token or "").strip()
        if not token:
            return False

        token_hash = CryptoService.hash_api_key(token)
        row = await session.execute(
            select(UserSession).where(UserSession.token_hash == token_hash)
        )
        sess = row.scalar_one_or_none()
        if sess is None:
            return False
        if sess.revoked_at is None:
            sess.revoked_at = datetime.utcnow()
            await session.flush()
        return True

    async def _revoke_active_sessions_for_user(self, session: AsyncSession, user_id: str) -> None:
        await session.execute(
            update(UserSession)
            .where(UserSession.user_id == user_id)
            .where(UserSession.revoked_at.is_(None))
            .values(revoked_at=datetime.utcnow())
        )
        await session.flush()

    async def acquire_generation_slot(self, session: AsyncSession, user_id: str) -> User:
        del session
        return await self._acquire_generation_slot_once(user_id=user_id)

    async def _acquire_generation_slot_once(self, *, user_id: str) -> User:
        from app.models.database import get_session_factory  # 延迟导入避免循环

        now = datetime.utcnow()
        today = now.date()
        factory = get_session_factory()
        async with self._usage_lock:
            async with factory() as inner:
                user = await self.get_user(inner, user_id)
                if user is None:
                    raise UserDisabledError("用户不存在")
                self._ensure_user_can_access(user, now=now)

                same_day = bool(user.last_used_at and user.last_used_at.date() == today)
                current_daily_used = user.daily_used if same_day else 0
                current_in_flight = max(0, int(user.in_flight or 0))

                if current_in_flight >= max(1, int(user.max_inflight or 1)):
                    raise UserConcurrencyExceededError("当前账号并发已达上限，请稍后重试")
                if user.daily_quota > 0 and current_daily_used + current_in_flight >= user.daily_quota:
                    raise UserQuotaExceededError("今日生成额度已用尽")

                if not same_day:
                    user.daily_used = 0
                user.in_flight = current_in_flight + 1
                user.updated_at = now
                await inner.commit()
                await inner.refresh(user)
                return user

    async def release_generation_slot(
        self,
        session: AsyncSession,
        user_id: str,
        *,
        count_usage: bool,
    ) -> None:
        """释放用户在途名额，并在需要时累计一次使用。

        流式生成的 finally 可能在请求已被 cancel 后执行。若继续依赖请求级 session，
        await 会直接被 CancelledError 打断，导致 users.in_flight 泄漏。
        这里改成：
        - 独立 session，脱离请求生命周期
        - asyncio.shield，确保外层 task 被 cancel 时内部释放仍能完成
        """
        del session
        try:
            await asyncio.shield(
                self._release_generation_slot_once(
                    user_id=user_id,
                    count_usage=count_usage,
                )
            )
        except asyncio.CancelledError:
            # shield 内部任务会继续完成；这里吞掉 cancel，避免 finally 清理被中断。
            pass
        except Exception as exc:
            logger.warning("release_generation_slot(%s) outer failed: %s", user_id, exc)

    async def _release_generation_slot_once(
        self,
        *,
        user_id: str,
        count_usage: bool,
    ) -> None:
        from app.models.database import get_session_factory  # 延迟导入避免循环

        now = datetime.utcnow()
        today = now.date()
        factory = get_session_factory()
        async with self._usage_lock:
            async with factory() as inner:
                try:
                    user = await self.get_user(inner, user_id)
                    if user is None:
                        return

                    same_day = bool(user.last_used_at and user.last_used_at.date() == today)
                    if not same_day:
                        user.daily_used = 0
                    if user.in_flight > 0:
                        user.in_flight -= 1
                    if count_usage:
                        user.daily_used += 1
                        user.total_requests += 1
                        user.last_used_at = now
                    user.updated_at = now
                    await inner.commit()
                except Exception as exc:
                    try:
                        await inner.rollback()
                    except Exception:
                        logger.warning("release_generation_slot(%s) rollback failed", user_id)
                    logger.warning("release_generation_slot(%s) commit failed: %s", user_id, exc)


_user_auth_service: Optional[UserAuthService] = None


def get_user_auth_service() -> UserAuthService:
    global _user_auth_service
    if _user_auth_service is None:
        _user_auth_service = UserAuthService()
    return _user_auth_service
