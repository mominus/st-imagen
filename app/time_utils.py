"""UTC helpers preserving the database's legacy naive-UTC representation."""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow_naive() -> datetime:
    """Return current UTC without tzinfo for compatibility with existing SQLite rows."""
    return datetime.now(UTC).replace(tzinfo=None)
