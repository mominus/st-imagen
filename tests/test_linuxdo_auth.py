"""LINUX DO Connect OAuth 登录：服务层与路由层测试。"""
from __future__ import annotations

import os
import tempfile
import unittest
import urllib.parse
from datetime import timedelta
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import database as database_mod
from app.models.database import Base, InviteCode, User, get_session
from app.routers.linuxdo_auth import router as linuxdo_router
from app.services import app_settings, linuxdo_auth
from app.services.user_auth import (
    InvalidInviteCodeError,
    UserAuthService,
    UserDisabledError,
)
from app.time_utils import utcnow_naive

PROFILE = {
    "id": 4242,
    "username": "Neo.User",
    "name": "Neo",
    "trust_level": 2,
    "active": True,
    "silenced": False,
}

BASE_ENV = {
    "LINUXDO_CLIENT_ID": "cid-test",
    "LINUXDO_CLIENT_SECRET": "secret-test",
    "LINUXDO_OAUTH_SCOPE": "user",
    "LINUXDO_MIN_TRUST_LEVEL": "0",
    "LINUXDO_OAUTH_ENABLED": "true",
    "USER_SESSION_SECURE": "false",
    "USER_SESSION_SAMESITE": "lax",
    "USER_SESSION_COOKIE_NAME": "imagen_session",
}


class ServiceTestCase(unittest.IsolatedAsyncioTestCase):
    """服务层测试：内存 SQLite + UserAuthService。"""

    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self._factory = async_sessionmaker(self._engine, expire_on_commit=False)
        self._orig_factory = database_mod._session_factory
        database_mod._session_factory = self._factory
        app_settings.clear_cache()
        self._service = UserAuthService()

    async def asyncTearDown(self) -> None:
        database_mod._session_factory = self._orig_factory
        app_settings.clear_cache()
        await self._engine.dispose()
        self._tmpdir.cleanup()

    async def _make_invite(self, *, max_uses: int = 1, daily_quota: int = 7, max_inflight: int = 3):
        async with self._factory() as session:
            created = await self._service.create_invite_codes(
                session, count=1, max_uses=max_uses, daily_quota=daily_quota, max_inflight=max_inflight
            )
            await session.commit()
        return created[0][1]

    async def test_first_login_with_invite_creates_linked_user(self) -> None:
        code = await self._make_invite(max_uses=3)
        async with self._factory() as session:
            user, token = await self._service.login_with_linuxdo(
                session,
                profile={"id": "4242", "username": "Neo.User", "trust_level": 2},
                invite_code=code,
                ip_address="1.2.3.4",
                user_agent="pytest",
            )
            await session.commit()
            assert token
            assert user.linuxdo_id == "4242"
            assert user.username == "Neo.User"
            assert user.linuxdo_username == "Neo.User"
            assert user.linuxdo_trust_level == 2
            assert user.auth_kind == "linuxdo"
            assert user.daily_quota == self._service._default_user_daily_quota
            assert user.max_inflight == 3
            assert user.expires_at is None
            invite = await session.get(InviteCode, user.invite_code_id)
            assert invite.used_count == 1

    async def test_linuxdo_registration_does_not_inherit_invite_expiry_or_quota(self) -> None:
        code = await self._make_invite(max_uses=1, daily_quota=2)
        async with self._factory() as session:
            row = await session.execute(select(InviteCode))
            invite = row.scalars().one()
            invite.expires_at = utcnow_naive() + timedelta(days=1)
            await session.commit()

        async with self._factory() as session:
            user, _ = await self._service.login_with_linuxdo(
                session,
                profile={"id": "9001", "username": "forum-user", "trust_level": 2},
                invite_code=code,
                ip_address=None,
                user_agent=None,
            )

            assert user.username == "forum-user"
            assert user.auth_kind == "linuxdo"
            assert user.expires_at is None
            assert user.daily_quota == self._service._default_user_daily_quota

    async def test_second_login_reuses_account_without_consuming_invite(self) -> None:
        code = await self._make_invite(max_uses=3)
        async with self._factory() as session:
            first, _ = await self._service.login_with_linuxdo(
                session,
                profile={"id": "4242", "username": "Neo.User", "trust_level": 2},
                invite_code=code,
                ip_address=None,
                user_agent=None,
            )
            await session.commit()
        async with self._factory() as session:
            second, token = await self._service.login_with_linuxdo(
                session,
                profile={"id": "4242", "username": "renamed", "trust_level": 3},
                invite_code=None,
                ip_address=None,
                user_agent=None,
            )
            await session.commit()
            assert second.id == first.id
            assert second.username == "renamed"
            assert second.linuxdo_username == "renamed"
            assert second.linuxdo_trust_level == 3
            invite = await session.get(InviteCode, second.invite_code_id)
            assert invite.used_count == 1

    async def test_first_login_without_invite_rejected(self) -> None:
        with self.assertRaises(InvalidInviteCodeError):
            async with self._factory() as session:
                await self._service.login_with_linuxdo(
                    session,
                    profile={"id": "1", "username": "who", "trust_level": 0},
                    invite_code="",
                    ip_address=None,
                    user_agent=None,
                )

    async def test_username_collision_appends_suffix(self) -> None:
        async with self._factory() as session:
            session.add(
                User(
                    id="occupied",
                    username="Neo.User",
                    password_hash="x",
                    status="active",
                )
            )
            await session.commit()
        code = await self._make_invite()
        async with self._factory() as session:
            user, _ = await self._service.login_with_linuxdo(
                session,
                profile={"id": "4242", "username": "Neo.User", "trust_level": 1},
                invite_code=code,
                ip_address=None,
                user_agent=None,
            )
            await session.commit()
            assert user.username.startswith("Neo.User_")
            assert len(user.username) <= 64

    async def test_disabled_bound_user_cannot_login(self) -> None:
        code = await self._make_invite()
        async with self._factory() as session:
            user, _ = await self._service.login_with_linuxdo(
                session,
                profile={"id": "4242", "username": "Neo.User", "trust_level": 1},
                invite_code=code,
                ip_address=None,
                user_agent=None,
            )
            user.status = "disabled"
            await session.commit()
        async with self._factory() as session:
            with self.assertRaises(UserDisabledError):
                await self._service.login_with_linuxdo(
                    session,
                    profile={"id": "4242", "username": "Neo.User", "trust_level": 1},
                    invite_code=None,
                    ip_address=None,
                    user_agent=None,
                )


class ParseAndCodecTests(unittest.TestCase):
    def test_parse_profile_requires_id(self) -> None:
        profile = linuxdo_auth.parse_profile({"id": 7, "username": "u", "trust_level": "3"})
        assert profile == {"id": "7", "username": "u", "name": "", "trust_level": 3}
        with self.assertRaises(linuxdo_auth.LinuxDOOAuthError):
            linuxdo_auth.parse_profile({"username": "no-id"})
        with self.assertRaises(linuxdo_auth.LinuxDOOAuthError):
            linuxdo_auth.parse_profile("not-a-dict")

    def test_state_payload_roundtrip(self) -> None:
        payload = linuxdo_auth.encode_state_payload(
            state="s1", verifier="v1", invite_code="sti_x"
        )
        decoded = linuxdo_auth.decode_state_payload(payload)
        assert decoded == {"state": "s1", "verifier": "v1", "invite_code": "sti_x"}
        assert linuxdo_auth.decode_state_payload(None) is None
        assert linuxdo_auth.decode_state_payload("garbage") is None
        assert linuxdo_auth.decode_state_payload("AAA=") is None  # 解码后缺少必要字段

    def test_pkce_pair_shape(self) -> None:
        verifier, challenge = linuxdo_auth.make_pkce_pair()
        assert 43 <= len(verifier) <= 128
        assert len(challenge) == 43
        assert "=" not in challenge


class RouteTestCase(unittest.IsolatedAsyncioTestCase):
    """路由层测试：httpx ASGITransport + MockTransport 打桩上游。"""

    def _build_app(self) -> FastAPI:
        app = FastAPI()
        app.include_router(linuxdo_router)

        async def _override_session():
            async with self._factory() as session:
                yield session

        app.dependency_overrides[get_session] = _override_session
        return app

    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self._factory = async_sessionmaker(self._engine, expire_on_commit=False)
        self._orig_factory = database_mod._session_factory
        database_mod._session_factory = self._factory
        app_settings.clear_cache()
        self._env_patch = patch.dict(os.environ, BASE_ENV)
        self._env_patch.start()
        self._profile = dict(PROFILE)
        self._upstream_requests: list[httpx.Request] = []
        self._token_status = 200
        app = self._build_app()
        self._client = AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")

    async def asyncTearDown(self) -> None:
        await self._client.aclose()
        self._env_patch.stop()
        linuxdo_auth.set_http_client_kwargs()
        database_mod._session_factory = self._orig_factory
        app_settings.clear_cache()
        await self._engine.dispose()
        self._tmpdir.cleanup()

    def _install_upstream_mock(self) -> None:
        outer = self

        def handler(request: httpx.Request) -> httpx.Response:
            outer._upstream_requests.append(request)
            if request.url.host != "connect.linux.do":
                return httpx.Response(404)
            if request.url.path == "/oauth2/token":
                if outer._token_status != 200:
                    return httpx.Response(outer._token_status, json={"error": "bad"})
                return httpx.Response(200, json={"access_token": "at-1", "token_type": "bearer"})
            if request.url.path == "/api/user":
                if request.headers.get("Authorization") != "Bearer at-1":
                    return httpx.Response(401, json={"error": "invalid_token"})
                return httpx.Response(200, json=outer._profile)
            return httpx.Response(404)

        linuxdo_auth.set_http_client_kwargs(transport=httpx.MockTransport(handler))

    async def _make_invite(self, *, max_uses: int = 1, revoked: bool = False) -> str:
        service = UserAuthService()
        async with self._factory() as session:
            created = await service.create_invite_codes(session, count=1, max_uses=max_uses)
            code = created[0][1]
            if revoked:
                await service.revoke_invite_code(session, created[0][0].id)
            await session.commit()
        return code

    async def _start(self, client: AsyncClient, invite_code: str | None = None):
        return await client.post(
            "/api/auth/linuxdo/start", json={"invite_code": invite_code or ""}
        )

    async def test_start_returns_authorize_url_and_state_cookie(self) -> None:
        self._install_upstream_mock()
        r = await self._start(self._client, "sti_whatever")
        assert r.status_code == 200
        authorize_url = r.json()["authorize_url"]
        assert authorize_url.startswith(linuxdo_auth.AUTHORIZE_URL)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(authorize_url).query)
        assert query["client_id"] == ["cid-test"]
        assert query["response_type"] == ["code"]
        assert query["scope"] == ["user"]
        assert query["code_challenge_method"] == ["S256"]
        set_cookie = r.headers["set-cookie"]
        assert f"{linuxdo_auth.STATE_COOKIE_NAME}=" in set_cookie

    async def test_start_disabled_returns_404(self) -> None:
        with patch.dict(os.environ, {"LINUXDO_OAUTH_ENABLED": "false"}):
            r = await self._start(self._client)
        assert r.status_code == 404

    async def test_first_login_with_invite_creates_user_and_session(self) -> None:
        self._install_upstream_mock()
        code = await self._make_invite(max_uses=3)
        start = await self._start(self._client, code)
        state = urllib.parse.parse_qs(
            urllib.parse.urlparse(start.json()["authorize_url"]).query
        )["state"][0]

        callback = await self._client.get(
            "/api/auth/linuxdo/callback", params={"code": "auth-code-1", "state": state}
        )
        assert callback.status_code == 303
        assert callback.headers["location"] == "/"
        assert "imagen_session=" in callback.headers["set-cookie"]

        async with self._factory() as session:
            row = await session.execute(select(User).where(User.auth_kind == "linuxdo"))
            user = row.scalars().one()
            assert user.linuxdo_id == "4242"
            assert user.username == "Neo.User"
            invite = await session.get(InviteCode, user.invite_code_id)
            assert invite.used_count == 1

    async def test_returning_bound_user_logs_in_without_invite(self) -> None:
        self._install_upstream_mock()
        code = await self._make_invite(max_uses=3)
        for round_index in range(2):
            start = await self._start(self._client, code if round_index == 0 else None)
            state = urllib.parse.parse_qs(
                urllib.parse.urlparse(start.json()["authorize_url"]).query
            )["state"][0]
            callback = await self._client.get(
                "/api/auth/linuxdo/callback",
                params={"code": f"auth-code-{round_index}", "state": state},
            )
            assert callback.status_code == 303
            assert callback.headers["location"] == "/"

        async with self._factory() as session:
            row = await session.execute(select(User).where(User.auth_kind == "linuxdo"))
            users = row.scalars().all()
            assert len(users) == 1
            invite = await session.get(InviteCode, users[0].invite_code_id)
            assert invite.used_count == 1

    async def test_callback_state_mismatch_rejected(self) -> None:
        self._install_upstream_mock()
        await self._start(self._client)
        callback = await self._client.get(
            "/api/auth/linuxdo/callback",
            params={"code": "auth-code-1", "state": "tampered"},
        )
        assert callback.status_code == 303
        assert "auth_error=" in callback.headers["location"]
        assert "登录状态校验失败" in urllib.parse.unquote(callback.headers["location"])

    async def test_callback_without_cookie_rejected(self) -> None:
        self._install_upstream_mock()
        callback = await self._client.get(
            "/api/auth/linuxdo/callback", params={"code": "c", "state": "s"}
        )
        assert callback.status_code == 303
        assert "auth_error=" in callback.headers["location"]

    async def test_first_login_without_invite_redirects_with_hint(self) -> None:
        self._install_upstream_mock()
        start = await self._start(self._client)
        state = urllib.parse.parse_qs(
            urllib.parse.urlparse(start.json()["authorize_url"]).query
        )["state"][0]
        callback = await self._client.get(
            "/api/auth/linuxdo/callback", params={"code": "auth-code-1", "state": state}
        )
        assert callback.status_code == 303
        location = urllib.parse.unquote(callback.headers["location"])
        assert location.startswith("/?auth_error=")
        assert "邀请码" in location

    async def test_revoked_invite_rejected(self) -> None:
        self._install_upstream_mock()
        code = await self._make_invite(revoked=True)
        start = await self._start(self._client, code)
        state = urllib.parse.parse_qs(
            urllib.parse.urlparse(start.json()["authorize_url"]).query
        )["state"][0]
        callback = await self._client.get(
            "/api/auth/linuxdo/callback", params={"code": "auth-code-1", "state": state}
        )
        location = urllib.parse.unquote(callback.headers["location"])
        assert "邀请码已失效" in location

    async def test_exhausted_invite_rejected(self) -> None:
        self._install_upstream_mock()
        code = await self._make_invite(max_uses=1)
        async with self._factory() as session:
            row = await session.execute(select(InviteCode))
            invite = row.scalars().one()
            invite.used_count = 1
            await session.commit()
        assert code

        start = await self._start(self._client, code)
        state = urllib.parse.parse_qs(
            urllib.parse.urlparse(start.json()["authorize_url"]).query
        )["state"][0]
        callback = await self._client.get(
            "/api/auth/linuxdo/callback", params={"code": "auth-code-1", "state": state}
        )
        location = urllib.parse.unquote(callback.headers["location"])
        assert "使用上限" in location

    async def test_trust_level_gate(self) -> None:
        self._install_upstream_mock()
        code = await self._make_invite()
        start = await self._start(self._client, code)
        state = urllib.parse.parse_qs(
            urllib.parse.urlparse(start.json()["authorize_url"]).query
        )["state"][0]
        with patch.dict(os.environ, {"LINUXDO_MIN_TRUST_LEVEL": "3"}):
            callback = await self._client.get(
                "/api/auth/linuxdo/callback",
                params={"code": "auth-code-1", "state": state},
            )
        location = urllib.parse.unquote(callback.headers["location"])
        assert "信任等级不足" in location
        async with self._factory() as session:
            row = await session.execute(select(User).where(User.auth_kind == "linuxdo"))
            assert row.scalars().first() is None

    async def test_token_exchange_failure_redirects(self) -> None:
        self._install_upstream_mock()
        self._token_status = 400
        code = await self._make_invite()
        start = await self._start(self._client, code)
        state = urllib.parse.parse_qs(
            urllib.parse.urlparse(start.json()["authorize_url"]).query
        )["state"][0]
        callback = await self._client.get(
            "/api/auth/linuxdo/callback", params={"code": "auth-code-1", "state": state}
        )
        location = urllib.parse.unquote(callback.headers["location"])
        assert "授权失败" in location
        # state cookie 无论成败都应被清除（max_age=0）
        assert f'{linuxdo_auth.STATE_COOKIE_NAME}=""' in callback.headers["set-cookie"]

    async def test_pkce_verifier_sent_on_token_exchange(self) -> None:
        self._install_upstream_mock()
        code = await self._make_invite()
        start = await self._start(self._client, code)
        authorize_url = start.json()["authorize_url"]
        query = urllib.parse.parse_qs(urllib.parse.urlparse(authorize_url).query)
        state = query["state"][0]
        await self._client.get(
            "/api/auth/linuxdo/callback", params={"code": "auth-code-1", "state": state}
        )
        token_requests = [
            r for r in self._upstream_requests if r.url.path == "/oauth2/token"
        ]
        assert token_requests, "缺少令牌交换请求"
        form = urllib.parse.parse_qs(token_requests[0].content.decode("utf-8"))
        assert form["grant_type"] == ["authorization_code"]
        assert form["client_id"] == ["cid-test"]
        assert form["client_secret"] == ["secret-test"]
        assert form["redirect_uri"] == ["http://testserver/api/auth/linuxdo/callback"]
        assert form["code"] == ["auth-code-1"]
        assert 43 <= len(form["code_verifier"][0]) <= 128

    async def test_expired_invite_rejected(self) -> None:
        self._install_upstream_mock()
        expired = await self._make_invite()
        async with self._factory() as session:
            row = await session.execute(select(InviteCode))
            invite = row.scalars().one()
            invite.expires_at = utcnow_naive() - timedelta(days=1)
            await session.commit()
            assert invite

        start = await self._start(self._client, expired)
        state = urllib.parse.parse_qs(
            urllib.parse.urlparse(start.json()["authorize_url"]).query
        )["state"][0]
        callback = await self._client.get(
            "/api/auth/linuxdo/callback", params={"code": "auth-code-1", "state": state}
        )
        location = urllib.parse.unquote(callback.headers["location"])
        assert "邀请码已过期" in location

    async def test_invalid_invite_code_rejected(self) -> None:
        self._install_upstream_mock()
        start = await self._start(self._client, "sti_not_a_real_code")
        state = urllib.parse.parse_qs(
            urllib.parse.urlparse(start.json()["authorize_url"]).query
        )["state"][0]
        callback = await self._client.get(
            "/api/auth/linuxdo/callback", params={"code": "auth-code-1", "state": state}
        )
        location = urllib.parse.unquote(callback.headers["location"])
        assert "邀请码无效" in location


class EnabledFlagTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self._factory = async_sessionmaker(self._engine, expire_on_commit=False)
        self._orig_factory = database_mod._session_factory
        database_mod._session_factory = self._factory
        app_settings.clear_cache()

    async def asyncTearDown(self) -> None:
        database_mod._session_factory = self._orig_factory
        app_settings.clear_cache()
        await self._engine.dispose()

    async def test_runtime_setting_overrides_env(self) -> None:
        with patch.dict(os.environ, {"LINUXDO_OAUTH_ENABLED": "false"}):
            assert await linuxdo_auth.is_enabled() is False
            await app_settings.set_setting(app_settings.SETTING_LINUXDO_OAUTH_ENABLED, "true")
            assert await linuxdo_auth.is_enabled() is True
            await app_settings.set_setting(app_settings.SETTING_LINUXDO_OAUTH_ENABLED, None)
            assert await linuxdo_auth.is_enabled() is False

    async def test_env_default_parsing(self) -> None:
        with patch.dict(os.environ, {"LINUXDO_OAUTH_ENABLED": "true"}):
            assert await linuxdo_auth.is_enabled() is True

    async def test_is_configured_requires_both_credentials(self) -> None:
        with patch.dict(os.environ, {"LINUXDO_CLIENT_ID": "a", "LINUXDO_CLIENT_SECRET": ""}):
            assert linuxdo_auth.is_configured() is False
        with patch.dict(os.environ, {"LINUXDO_CLIENT_ID": "a", "LINUXDO_CLIENT_SECRET": "b"}):
            assert linuxdo_auth.is_configured() is True


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
