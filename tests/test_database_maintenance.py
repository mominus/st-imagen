from __future__ import annotations

import unittest
from datetime import timedelta
from unittest.mock import patch

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.database import Base, GenerationLog, UserSession
from app.services.database_maintenance import (
    remove_expired_sessions,
    remove_old_generation_logs,
)
from app.time_utils import utcnow_naive


class DatabaseMaintenanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_lifecycle_cleanup_removes_only_expired_data(self):
        factory = self.factory
        now = utcnow_naive()
        async with factory() as session:
            session.add_all(
                [
                    UserSession(
                        id="expired",
                        user_id="u1",
                        token_hash="expired-token",
                        expires_at=now - timedelta(minutes=1),
                        last_seen_at=now,
                    ),
                    UserSession(
                        id="active",
                        user_id="u1",
                        token_hash="active-token",
                        expires_at=now + timedelta(days=1),
                        last_seen_at=now,
                    ),
                    GenerationLog(
                        id="old-log",
                        timestamp=now - timedelta(days=91),
                        mode="text2img",
                        status="success",
                    ),
                    GenerationLog(
                        id="new-log",
                        timestamp=now,
                        mode="text2img",
                        status="success",
                    ),
                ]
            )
            await session.commit()

        with patch("app.services.database_maintenance.get_session_factory", return_value=factory):
            self.assertEqual(await remove_expired_sessions(), 1)
            self.assertEqual(await remove_old_generation_logs(90), 1)

        async with factory() as session:
            self.assertEqual(await session.scalar(select(func.count(UserSession.id))), 1)
            self.assertEqual(await session.scalar(select(func.count(GenerationLog.id))), 1)

    async def test_zero_log_retention_disables_cleanup(self):
        self.assertEqual(await remove_old_generation_logs(0), 0)
