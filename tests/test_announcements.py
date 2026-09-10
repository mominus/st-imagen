from __future__ import annotations

import unittest
from datetime import timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.database import Announcement, Base
from app.routers.admin import AnnouncementRequest, create_announcement, update_announcement
from app.routers.announcements import list_published_announcements
from app.time_utils import utcnow_naive


class AnnouncementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_public_timeline_excludes_drafts_and_orders_newest_first(self) -> None:
        now = utcnow_naive()
        async with self.factory() as session:
            session.add_all([
                Announcement(id="old", title="旧公告", content="old", published_at=now - timedelta(days=1)),
                Announcement(id="new", title="新公告", content="new", published_at=now),
                Announcement(id="draft", title="草稿", content="draft", published_at=None),
            ])
            await session.commit()
            result = await list_published_announcements(session=session)
        self.assertEqual([item["id"] for item in result["items"]], ["new", "old"])

    async def test_admin_can_create_draft_and_publish_it(self) -> None:
        async with self.factory() as session:
            created = await create_announcement(
                AnnouncementRequest(title=" 维护 ", content=" 内容 ", published=False),
                payload={"sub": "admin"}, session=session,
            )
            self.assertFalse(created["published"])
            updated = await update_announcement(
                created["id"], AnnouncementRequest(title="维护", content="内容", published=True),
                payload={"sub": "admin"}, session=session,
            )
        self.assertTrue(updated["published"])
        self.assertIsNotNone(updated["published_at"])
