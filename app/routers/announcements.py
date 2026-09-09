"""无需登录即可读取的已发布公告。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import Announcement, get_session
from app.time_utils import utcnow_naive

router = APIRouter(prefix="/api/announcements", tags=["announcements"])


def announcement_payload(item: Announcement) -> dict:
    return {
        "id": item.id,
        "title": item.title,
        "content": item.content,
        "published_at": item.published_at.isoformat() if item.published_at else None,
    }


@router.get("")
async def list_published_announcements(session: AsyncSession = Depends(get_session)):
    rows = (
        await session.execute(
            select(Announcement)
            .where(
                Announcement.published_at.is_not(None),
                Announcement.published_at <= utcnow_naive(),
            )
            .order_by(Announcement.published_at.desc(), Announcement.id.desc())
            .limit(100)
        )
    ).scalars().all()
    return {"items": [announcement_payload(item) for item in rows]}
