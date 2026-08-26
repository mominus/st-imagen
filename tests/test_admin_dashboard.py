from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from app.models.database import AdminAuditLog
from app.routers.admin import _record_admin_audit, dashboard_snapshot


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
