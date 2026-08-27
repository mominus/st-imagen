"""Small, safe lifecycle jobs for the single-node SQLite deployment."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete

from app.models.database import GenerationLog, UserSession, get_session_factory
from app.time_utils import utcnow_naive


async def remove_expired_sessions() -> int:
    """Delete expired or revoked sessions; safe to call repeatedly at startup."""
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            delete(UserSession).where(
                (UserSession.expires_at <= utcnow_naive()) | (UserSession.revoked_at.is_not(None))
            )
        )
        await session.commit()
        return max(0, int(result.rowcount or 0))


async def remove_old_generation_logs(retention_days: int) -> int:
    """Delete raw generation logs older than the configured retention window."""
    days = max(0, int(retention_days))
    if days == 0:
        return 0
    cutoff = utcnow_naive() - timedelta(days=days)
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(delete(GenerationLog).where(GenerationLog.timestamp < cutoff))
        await session.commit()
        return max(0, int(result.rowcount or 0))
