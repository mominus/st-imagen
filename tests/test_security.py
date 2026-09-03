from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from starlette.requests import Request

from app.main import _metric_path
from app.models.database import Account, Admin, InviteCode, User
from app.services import account_pool as account_pool_mod
from app.services import auth as auth_mod
from app.services import outbound_url as outbound_url_mod
from app.services import st_client as st_client_mod
from app.services import user_auth as user_auth_mod
from app.services.crypto import CryptoService


class FakeExecResult:
    def __init__(self, *, scalar=None, scalars=None, rowcount=None, rows=None, first=None):
        self._scalar = scalar
        self._scalars = list(scalars or [])
        self.rowcount = rowcount
        self._rows = list(rows or [])
        self._first = first

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._scalars))

    def fetchall(self):
        return list(self._rows)

    def first(self):
        return self._first


class FakeSession:
    def __init__(self, results=None):
        self._results = list(results or [])
        self.executed = []
        self.flush_count = 0
        self.commit_count = 0
        self.refreshed = []
        self.added = []
        self.deleted = []

    async def execute(self, stmt):
        self.executed.append(stmt)
        stmt_text = str(stmt)
        if "PRAGMA table_info(users)" in stmt_text:
            if self._results and getattr(self._results[0], "_rows", None):
                return self._results.pop(0)
            return FakeExecResult(rows=[(0, "expires_at", "DATETIME", 0, None, 0)])
        if self._results:
            return self._results.pop(0)
        return FakeExecResult()

    async def flush(self):
        self.flush_count += 1

    async def commit(self):
        self.commit_count += 1

    async def refresh(self, obj):
        self.refreshed.append(obj)

    async def rollback(self):
        return None

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)


class FakeSessionContext:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, exc, tb):
        return False


class RepeatingSessionFactory:
    def __init__(self, builder):
        self._builder = builder

    def __call__(self):
        return FakeSessionContext(self._builder())


class SharedUserSession(FakeSession):
    def __init__(self, user):
        super().__init__(results=None)
        self.user = user

    async def execute(self, stmt):
        self.executed.append(stmt)
        return FakeExecResult(scalar=self.user)


class FakeStreamResponse:
    def __init__(self, *, status_code=200, chunks=None, body=b""):
        self.status_code = status_code
        self._chunks = list(chunks or [])
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aread(self):
        return self._body

    async def aiter_text(self):
        for chunk in self._chunks:
            yield chunk


class FakeAsyncClient:
    instances = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.calls = []
        FakeAsyncClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream(self, method, url, headers=None, json=None, timeout=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "json": json,
                "timeout": timeout,
            }
        )
        return FakeStreamResponse(chunks=['data: {"ok": true}\n\n'])


class OutboundUrlTests(unittest.IsolatedAsyncioTestCase):
    async def test_outbound_url_blocks_private_targets(self) -> None:
        target = await outbound_url_mod.ensure_safe_outbound_url("https://8.8.8.8/image.png")
        self.assertEqual(target.hostname, "8.8.8.8")

        with self.assertRaises(outbound_url_mod.UnsafeOutboundURLError):
            await outbound_url_mod.ensure_safe_outbound_url("http://127.0.0.1/test.png")

        with self.assertRaises(outbound_url_mod.UnsafeOutboundURLError):
            await outbound_url_mod.ensure_safe_outbound_url("http://localhost/test.png")

    async def test_safe_stream_pins_validated_ip_and_preserves_host_and_sni(self) -> None:
        class RecordingClient:
            def __init__(self) -> None:
                self.request = None

            def build_request(self, method, url, **kwargs):
                self.request = httpx.Request(method, url, **kwargs)
                return self.request

            async def send(self, request, *, stream):
                return httpx.Response(200, request=request)

        client = RecordingClient()
        with patch.object(
            outbound_url_mod,
            "_resolve_hostname_ips",
            return_value={"93.184.216.34"},
        ):
            await outbound_url_mod.open_safe_stream(
                client, "GET", "https://example.com:8443/image.png"
            )

        self.assertEqual(str(client.request.url), "https://93.184.216.34:8443/image.png")
        self.assertEqual(client.request.headers["host"], "example.com:8443")
        self.assertEqual(client.request.extensions["sni_hostname"], "example.com")


class MetricsCardinalityTests(unittest.TestCase):
    def test_unmatched_paths_share_one_metric_label(self) -> None:
        first = Request({"type": "http", "method": "GET", "path": "/random-a", "headers": []})
        second = Request({"type": "http", "method": "GET", "path": "/random-b", "headers": []})

        self.assertEqual(_metric_path(first), "__unmatched__")
        self.assertEqual(_metric_path(second), "__unmatched__")

    def test_matched_path_uses_route_template(self) -> None:
        route = SimpleNamespace(path="/api/users/{user_id}")
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/users/123",
                "headers": [],
                "route": route,
            }
        )

        self.assertEqual(_metric_path(request), "/api/users/{user_id}")


class UserAuthServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_invites_accepts_admin_specified_codes(self) -> None:
        session = FakeSession()
        service = user_auth_mod.UserAuthService()

        created = await service.create_invite_codes(
            session,
            count=99,
            specified_codes="summer-guest\n\npartner-2026\n",
            daily_quota=8,
        )

        self.assertEqual([raw for _, raw in created], ["summer-guest", "partner-2026"])
        self.assertEqual([invite.code_prefix for invite, _ in created], ["summer-guest", "partner-2026"])
        self.assertTrue(all(invite.daily_quota == 8 for invite, _ in created))

    async def test_create_invites_rejects_duplicate_specified_codes(self) -> None:
        with self.assertRaisesRegex(ValueError, "重复"):
            await user_auth_mod.UserAuthService().create_invite_codes(
                FakeSession(),
                count=1,
                specified_codes="same-code\nsame-code",
            )

    async def test_ensure_user_schema_adds_missing_expires_at_column(self) -> None:
        session = FakeSession(
            [FakeExecResult(rows=[(0, "id", "VARCHAR(36)", 1, None, 1)])]
        )
        service = user_auth_mod.UserAuthService()

        await service.ensure_user_schema(session)

        self.assertTrue(
            any("ALTER TABLE users ADD COLUMN expires_at DATETIME" in str(stmt) for stmt in session.executed),
            "ensure_user_schema should auto-migrate missing expires_at column",
        )

    async def test_invite_login_creates_guest_without_user_credentials(self) -> None:
        invite = InviteCode(
            id="invite-guest-1",
            code_hash=CryptoService.hash_api_key("sti_guest_code"),
            code_prefix="sti_guest",
            code_suffix="code",
            max_uses=2,
            used_count=0,
            daily_quota=12,
            max_inflight=1,
        )
        session = FakeSession(
            [
                FakeExecResult(
                    rows=[
                        (0, "expires_at", "DATETIME", 0, None, 0),
                        (1, "auth_kind", "VARCHAR(24)", 1, "password", 0),
                    ]
                ),
                FakeExecResult(scalar=invite),
            ]
        )
        service = user_auth_mod.UserAuthService()

        user, raw_token = await service.login_with_invite(
            session,
            invite_code="sti_guest_code",
            ip_address="203.0.113.5",
            user_agent="pytest",
        )

        self.assertEqual(user.auth_kind, "invite_guest")
        self.assertEqual(user.username, "sti_guest_code")
        self.assertEqual(user.invite_code_id, invite.id)
        self.assertEqual(user.daily_quota, 12)
        self.assertEqual(user.max_inflight, 1)
        self.assertEqual(invite.used_count, 1)
        self.assertTrue(raw_token)
        self.assertEqual(len(session.added), 2, "guest user and opaque session should be persisted")

    async def test_invite_login_applies_invite_expiry_to_new_guest_user(self) -> None:
        expires_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=7)
        invite = InviteCode(
            id="invite-guest-expiry",
            code_hash=CryptoService.hash_api_key("sti_expiring_code"),
            code_prefix="sti_expiring",
            code_suffix="code",
            max_uses=1,
            used_count=0,
            daily_quota=5,
            max_inflight=1,
            expires_at=expires_at,
        )
        session = FakeSession(
            [
                FakeExecResult(rows=[(0, "expires_at", "DATETIME", 0, None, 0)]),
                FakeExecResult(scalar=invite),
                FakeExecResult(scalar=None),
            ]
        )
        service = user_auth_mod.UserAuthService()

        user, _ = await service.login_with_invite(
            session,
            invite_code="sti_expiring_code",
            ip_address="203.0.113.5",
            user_agent="pytest",
        )

        self.assertEqual(user.expires_at, expires_at, "guest account should inherit invite expiry")

    async def test_invite_login_backfills_expiry_for_legacy_guest_user(self) -> None:
        expires_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=7)
        invite = InviteCode(
            id="invite-guest-legacy",
            code_hash=CryptoService.hash_api_key("sti_legacy_code"),
            code_prefix="sti_legacy",
            code_suffix="code",
            max_uses=5,
            used_count=1,
            daily_quota=5,
            max_inflight=1,
            expires_at=expires_at,
        )
        existing = User(
            id="legacy-guest",
            username="sti_legacy_code",
            password_hash="unused",
            status="active",
            auth_kind="invite_guest",
            invite_code_id=invite.id,
            daily_quota=5,
            daily_used=0,
            max_inflight=1,
            last_login_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1),
        )
        session = FakeSession(
            [
                FakeExecResult(rows=[(0, "expires_at", "DATETIME", 0, None, 0)]),
                FakeExecResult(scalar=invite),
                FakeExecResult(scalar=existing),
            ]
        )
        service = user_auth_mod.UserAuthService()

        user, _ = await service.login_with_invite(
            session,
            invite_code="sti_legacy_code",
            ip_address="203.0.113.5",
            user_agent="pytest",
        )

        self.assertIs(user, existing)
        self.assertEqual(
            user.expires_at,
            expires_at,
            "permanent legacy guest should adopt the invite expiry on re-login",
        )

    async def test_create_user_supports_quota_and_expiry(self) -> None:
        session = FakeSession(
            [
                FakeExecResult(rows=[(0, "expires_at", "DATETIME", 0, None, 0)]),
                FakeExecResult(scalar=None),
            ]
        )
        service = user_auth_mod.UserAuthService()
        expires_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=30)

        created = await service.create_user(
            session,
            username="tester004",
            password="CreatePass!234",
            daily_quota=88,
            max_inflight=5,
            expires_at=expires_at,
        )

        self.assertEqual(created.username, "tester004")
        self.assertEqual(created.status, "active")
        self.assertEqual(created.daily_quota, 88)
        self.assertEqual(created.max_inflight, 5)
        self.assertEqual(created.expires_at, expires_at)
        self.assertEqual(session.flush_count, 1)
        self.assertEqual(session.added, [created])

    async def test_password_change_revokes_existing_sessions(self) -> None:
        user = User(
            id="user-1",
            username="tester001",
            password_hash=CryptoService.hash_password("InitialPass!234"),
            status="active",
            daily_quota=0,
            max_inflight=2,
        )
        session = FakeSession([FakeExecResult(scalar=user), FakeExecResult()])
        service = user_auth_mod.UserAuthService()

        updated = await service.update_user(
            session,
            "user-1",
            new_password="ChangedPass!234",
        )

        self.assertIs(updated, user)
        self.assertEqual(session.flush_count, 2)
        self.assertTrue(
            any("UPDATE user_sessions" in str(stmt) for stmt in session.executed),
            "password change should revoke active sessions",
        )

    async def test_acquire_generation_slot_uses_fresh_db_session(self) -> None:
        service = user_auth_mod.UserAuthService()
        shared_user = User(
            id="user-2",
            username="tester002",
            password_hash=CryptoService.hash_password("InitialPass!234"),
            status="active",
            daily_quota=0,
            daily_used=0,
            in_flight=0,
            max_inflight=1,
        )
        stale_request_session = FakeSession(
            [
                FakeExecResult(
                    scalar=User(
                        id="user-2",
                        username="tester002",
                        password_hash=shared_user.password_hash,
                        status="active",
                        daily_quota=0,
                        daily_used=0,
                        in_flight=0,
                        max_inflight=1,
                    )
                ),
                FakeExecResult(
                    scalar=User(
                        id="user-2",
                        username="tester002",
                        password_hash=shared_user.password_hash,
                        status="active",
                        daily_quota=0,
                        daily_used=0,
                        in_flight=0,
                        max_inflight=1,
                    )
                ),
            ]
        )
        factory = RepeatingSessionFactory(lambda: SharedUserSession(shared_user))

        with patch("app.models.database.get_session_factory", return_value=factory):
            acquired = await service.acquire_generation_slot(stale_request_session, "user-2")
            self.assertEqual(acquired.id, "user-2")
            self.assertEqual(shared_user.in_flight, 1)
            self.assertEqual(stale_request_session.commit_count, 0)

            with self.assertRaises(user_auth_mod.UserConcurrencyExceededError):
                await service.acquire_generation_slot(stale_request_session, "user-2")

    async def test_authenticate_rejects_expired_user(self) -> None:
        expired_user = User(
            id="user-expired-login",
            username="tester005",
            password_hash=CryptoService.hash_password("InitialPass!234"),
            status="active",
            daily_quota=0,
            max_inflight=2,
            expires_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1),
        )
        session = FakeSession([FakeExecResult(scalar=expired_user)])
        service = user_auth_mod.UserAuthService()

        with self.assertRaises(user_auth_mod.UserExpiredError):
            await service.authenticate(
                session,
                username="tester005",
                password="InitialPass!234",
                ip_address="127.0.0.1",
                user_agent="pytest",
            )

    async def test_acquire_generation_slot_rejects_expired_user(self) -> None:
        service = user_auth_mod.UserAuthService()
        shared_user = User(
            id="user-expired-slot",
            username="tester006",
            password_hash=CryptoService.hash_password("InitialPass!234"),
            status="active",
            daily_quota=0,
            daily_used=0,
            in_flight=0,
            max_inflight=1,
            expires_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1),
        )
        factory = RepeatingSessionFactory(lambda: SharedUserSession(shared_user))

        with patch("app.models.database.get_session_factory", return_value=factory):
            with self.assertRaises(user_auth_mod.UserExpiredError):
                await service.acquire_generation_slot(FakeSession(), "user-expired-slot")

    async def test_delete_user_removes_sessions_and_user(self) -> None:
        user = User(
            id="user-3",
            username="tester003",
            password_hash=CryptoService.hash_password("InitialPass!234"),
            status="active",
            daily_quota=0,
            max_inflight=2,
        )
        session = FakeSession([FakeExecResult(scalar=user), FakeExecResult()])
        service = user_auth_mod.UserAuthService()

        deleted = await service.delete_user(session, "user-3")

        self.assertTrue(deleted)
        self.assertEqual(session.deleted, [user])
        self.assertEqual(session.flush_count, 1)
        self.assertTrue(
            any("DELETE FROM user_sessions" in str(stmt) for stmt in session.executed),
            "delete_user should clear persisted sessions first",
        )

    async def test_delete_invite_code_clears_user_references(self) -> None:
        invite = InviteCode(
            id="invite-1",
            code_hash="hash",
            code_prefix="sti_abcdef",
            code_suffix="wxyz",
            max_uses=1,
            used_count=1,
            daily_quota=0,
            max_inflight=2,
        )
        session = FakeSession([FakeExecResult(scalar=invite), FakeExecResult()])
        service = user_auth_mod.UserAuthService()

        deleted = await service.delete_invite_code(session, "invite-1")

        self.assertTrue(deleted)
        self.assertEqual(session.deleted, [invite])
        self.assertEqual(session.flush_count, 1)
        self.assertTrue(
            any("UPDATE users" in str(stmt) for stmt in session.executed),
            "delete_invite_code should clear invite references before deleting the invite",
        )

    async def test_delete_all_users_returns_deleted_count(self) -> None:
        session = FakeSession([FakeExecResult(), FakeExecResult(rowcount=3)])
        service = user_auth_mod.UserAuthService()

        affected = await service.delete_all_users(session)

        self.assertEqual(affected, 3)
        self.assertEqual(session.flush_count, 1)
        self.assertTrue(
            any("DELETE FROM user_sessions" in str(stmt) for stmt in session.executed),
            "delete_all_users should clear user sessions",
        )
        self.assertTrue(
            any("DELETE FROM users" in str(stmt) for stmt in session.executed),
            "delete_all_users should delete users",
        )

    async def test_delete_all_invite_codes_clears_links_and_returns_deleted_count(self) -> None:
        session = FakeSession([FakeExecResult(), FakeExecResult(rowcount=5)])
        service = user_auth_mod.UserAuthService()

        affected = await service.delete_all_invite_codes(session)

        self.assertEqual(affected, 5)
        self.assertEqual(session.flush_count, 1)
        self.assertTrue(
            any("UPDATE users" in str(stmt) for stmt in session.executed),
            "delete_all_invite_codes should clear user invite references",
        )
        self.assertTrue(
            any("DELETE FROM invite_codes" in str(stmt) for stmt in session.executed),
            "delete_all_invite_codes should delete invite rows",
        )


class STClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_inference_text2img_reuses_shared_client(self) -> None:
        FakeAsyncClient.instances = []
        client = st_client_mod.STClient(base_url="https://st.example")
        shared_client = FakeAsyncClient()

        with patch.object(client, "_get_client", return_value=shared_client):
            with patch("app.services.st_client.httpx.AsyncClient", FakeAsyncClient):
                agen = client.stream_inference(
                    "org-1",
                    "flow-1",
                    "api-key-1",
                    {"in-0": "prompt", "in-6": ""},
                )
                try:
                    first = await anext(agen)
                finally:
                    await agen.aclose()

        self.assertEqual(first, '{"ok": true}')
        self.assertEqual(len(FakeAsyncClient.instances), 1)
        self.assertEqual(shared_client.calls[0]["method"], "POST")
        self.assertEqual(
            shared_client.calls[0]["url"],
            "https://st.example/inference/v0/stream/org-1/flow-1",
        )

    async def test_stream_inference_img2img_reuses_shared_client(self) -> None:
        FakeAsyncClient.instances = []
        client = st_client_mod.STClient(base_url="https://st.example")
        shared_client = FakeAsyncClient()

        with patch.object(client, "_get_client", return_value=shared_client):
            with patch("app.services.st_client.httpx.AsyncClient", FakeAsyncClient):
                agen = client.stream_inference(
                    "org-1",
                    "flow-1",
                    "api-key-1",
                    {"in-0": "prompt", "in-6": "https://example.com/ref.png"},
                )
                try:
                    first = await anext(agen)
                finally:
                    await agen.aclose()

        self.assertEqual(first, '{"ok": true}')
        self.assertEqual(len(FakeAsyncClient.instances), 1)
        self.assertEqual(shared_client.calls[0]["method"], "POST")
        self.assertEqual(
            shared_client.calls[0]["url"],
            "https://st.example/inference/v0/stream/org-1/flow-1",
        )


class AuthServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._env_backup = os.environ.copy()
        os.environ["JWT_SECRET_KEY"] = "unit-test-jwt-secret-at-least-32-bytes"
        os.environ["ADMIN_PASSWORD"] = "StrongAdminPassword!234"

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env_backup)

    async def test_admin_password_change_invalidates_old_jwt(self) -> None:
        admin = Admin(
            id="admin-1",
            username="admin",
            password_hash=CryptoService.hash_password("StrongAdminPassword!234"),
            token_version=0,
        )
        auth = auth_mod.AuthService()
        factory = RepeatingSessionFactory(lambda: FakeSession([FakeExecResult(scalar=admin)]))

        with patch("app.services.auth.get_session_factory", return_value=factory):
            token, admin_payload = await auth.authenticate("admin", "StrongAdminPassword!234")
            verified = await auth.verify_token(token)
            self.assertEqual(verified["sub"], admin_payload["id"])

            changed = await auth.change_password(
                admin_payload["id"],
                "StrongAdminPassword!234",
                "EvenStrongerPassword!567",
            )
            self.assertTrue(changed)
            self.assertEqual(admin.token_version, 1)

            with self.assertRaises(auth_mod.InvalidTokenError):
                await auth.verify_token(token)

            fresh_token, fresh_admin = await auth.authenticate(
                "admin",
                "EvenStrongerPassword!567",
            )
            fresh_payload = await auth.verify_token(fresh_token)
            self.assertEqual(fresh_payload["sub"], fresh_admin["id"])

    async def test_placeholder_admin_password_only_blocks_bootstrap_when_needed(self) -> None:
        os.environ["ADMIN_PASSWORD"] = "replace-with-strong-admin-password"

        existing_admin = Admin(
            id="admin-existing",
            username="admin",
            password_hash=CryptoService.hash_password("StrongAdminPassword!234"),
            token_version=0,
        )
        auth_existing = auth_mod.AuthService()
        factory_existing = RepeatingSessionFactory(
            lambda: FakeSession([FakeExecResult(scalar=existing_admin)])
        )
        with patch("app.services.auth.get_session_factory", return_value=factory_existing):
            await auth_existing.ensure_default_admin()

        auth_empty = auth_mod.AuthService()
        factory_empty = RepeatingSessionFactory(lambda: FakeSession([FakeExecResult(scalar=None)]))
        with patch("app.services.auth.get_session_factory", return_value=factory_empty):
            with self.assertRaisesRegex(RuntimeError, "默认/占位值"):
                await auth_empty.ensure_default_admin()


class AccountPoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_account_selection_uses_runtime_capacity(self) -> None:
        account = Account(
            id="account-1",
            name="quota@test.local",
            org_id="org-1",
            flow_id="flow-1",
            api_key_encrypted="enc",
            status="active",
            max_inflight=10,
            last_used_at=datetime.now(UTC).replace(tzinfo=None),
            created_at=datetime.now(UTC).replace(tzinfo=None),
        )
        session = FakeSession([FakeExecResult(scalars=[account]), FakeExecResult()])
        pool = account_pool_mod.AccountPoolService()

        selected = await pool.select_account(session)

        self.assertEqual(selected.id, account.id)
        self.assertEqual(session.commit_count, 1)
        self.assertEqual(session.refreshed, [account])


if __name__ == "__main__":
    unittest.main()
