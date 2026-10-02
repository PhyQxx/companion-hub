"""UTC normalization for persistent timestamps from SQLite and PostgreSQL."""

from datetime import UTC, datetime


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
