"""永久生成统计。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import GenerationCounter


TOTAL_GENERATED_IMAGES_KEY = "total_generated_images"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def get_total_generated_images(session: AsyncSession) -> int:
    row = (
        await session.execute(
            select(GenerationCounter).where(
                GenerationCounter.key == TOTAL_GENERATED_IMAGES_KEY
            )
        )
    ).scalars().first()
    return max(0, int(row.value)) if row is not None else 0


async def increment_total_generated_images(
    session: AsyncSession,
    count: int,
) -> None:
    """原子增加永久图片计数，日志和图片文件清理不会触碰该表。"""
    amount = int(count)
    if amount <= 0:
        return

    statement = sqlite_insert(GenerationCounter).values(
        key=TOTAL_GENERATED_IMAGES_KEY,
        value=amount,
        updated_at=_utcnow(),
    )
    statement = statement.on_conflict_do_update(
        index_elements=[GenerationCounter.key],
        set_={
            "value": GenerationCounter.value + amount,
            "updated_at": _utcnow(),
        },
    )
    await session.execute(statement)
