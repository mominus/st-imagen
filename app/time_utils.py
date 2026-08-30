"""UTC helpers preserving the database's legacy naive-UTC representation."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

BEIJING_TIME_ZONE = ZoneInfo("Asia/Shanghai")


def utcnow_naive() -> datetime:
    """Return current UTC without tzinfo for compatibility with existing SQLite rows."""
    return datetime.now(UTC).replace(tzinfo=None)


def format_beijing_time(value: datetime, fmt: str = "%m/%d %H:%M:%S") -> str:
    """Render a naive-UTC or aware datetime in Asia/Shanghai, e.g. 08/30 04:21:13."""
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(BEIJING_TIME_ZONE).strftime(fmt)
