from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.database import Account, AdminAuditLog, Base, GenerationLog, User
from app.routers.admin import _recent_logs_payload, _record_admin_audit, dashboard_snapshot


class AdminDashboardContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_contains_first_view_data_and_recent_logs(self) -> None:
        with patch(
            "app.routers.admin._stats_overview_payload",
            new=AsyncMock(return_value={"generations": {"total": 2}}),
        ), patch(
            "app.routers.admin.get_dashboard_analytics",
            new=AsyncMock(return_value={"period": "24h", "summary": {"requests": 2}}),
        ), patch(
            "app.routers.admin._runtime_status_payload",
            new=AsyncMock(return_value={"account_isolations": []}),
        ), patch(
            "app.routers.admin._runtime_metrics_payload",
            return_value={"generation": {"in_flight": 0}},
        ), patch(
            "app.routers.admin._recent_logs_payload",
            new=AsyncMock(return_value={"items": [{"id": "log-1"}], "total": 1}),
        ):
            result = await dashboard_snapshot(
                period="24h",
                payload={"sub": "admin-1", "username": "operator"},
                session=object(),
            )

        self.assertEqual(result["admin"], {"id": "admin-1", "username": "operator"})
        self.assertEqual(result["overview"]["generations"]["total"], 2)
        self.assertEqual(result["analytics"]["period"], "24h")
        self.assertEqual(result["recent_logs"], [{"id": "log-1"}])


class AdminLogQueryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)

        now = datetime(2026, 1, 1, 12, 0, 0)
        async with self.factory() as session:
            session.add(
                Account(
                    id="account-1",
                    name="alpha-account",
                    org_id="org",
                    flow_id="flow",
                    api_key_encrypted="secret",
                )
            )
            session.add(User(id="user-1", username="alice", password_hash="hash"))
            for index in range(5):
                failed = index in {1, 3}
                session.add(
                    GenerationLog(
                        id=f"log-{index}",
                        timestamp=now + timedelta(minutes=index),
                        user_id="user-1",
                        account_id="account-1",
                        mode="img2img" if index == 3 else "text2img",
                        model=f"model-{index}",
                        prompt_preview="literal 100% prompt" if index == 1 else f"prompt {index}",
                        status="error" if failed else "success",
                        error_message="upstream failed" if failed else None,
                        failure_category="upstream" if failed else None,
                    )
                )
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_query_paginates_over_all_matching_rows(self) -> None:
        async with self.factory() as session:
            first = await _recent_logs_payload(session, 2)
            second = await _recent_logs_payload(session, 2, offset=2)

        self.assertEqual(first["total"], 5)
        self.assertEqual([item["id"] for item in first["items"]], ["log-4", "log-3"])
        self.assertEqual([item["id"] for item in second["items"]], ["log-2", "log-1"])

    async def test_filters_run_in_sql_and_can_be_combined(self) -> None:
        async with self.factory() as session:
            result = await _recent_logs_payload(
                session,
                100,
                status="error",
                mode="img2img",
                failure_category="upstream",
                query="alice",
            )

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["id"], "log-3")

    async def test_search_treats_sql_wildcards_as_literal_text(self) -> None:
        async with self.factory() as session:
            result = await _recent_logs_payload(session, 100, query="100%")

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["id"], "log-1")


class AdminAuditLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_audit_persists_operator_and_safe_detail(self) -> None:
        class FakeSession:
            def __init__(self) -> None:
                self.row = None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def add(self, row) -> None:
                self.row = row

            async def commit(self) -> None:
                return None

        session = FakeSession()

        class FakeFactory:
            def __call__(self):
                return session

        with patch("app.routers.admin.get_session_factory", return_value=FakeFactory()):
            await _record_admin_audit(
                {"sub": "admin-1", "username": "operator"},
                action="clear_account_isolation",
                target_type="account",
                target_id="account-1",
                detail={"cleared": True},
            )

        row = session.row
        self.assertIsInstance(row, AdminAuditLog)

        self.assertEqual(row.admin_id, "admin-1")
        self.assertEqual(row.admin_username, "operator")
        self.assertEqual(row.action, "clear_account_isolation")
        self.assertEqual(row.target_id, "account-1")
        self.assertEqual(row.detail_json, '{"cleared":true}')
        self.assertTrue(row.success)
