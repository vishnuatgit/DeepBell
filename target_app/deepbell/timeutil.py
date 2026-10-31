"""UTC time helpers. Call `timeutil.utcnow()` through the module so tests can set the clock."""

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(value) -> datetime | None:
    """Parse a stored timestamp: ISO-8601, or SQLite's 'YYYY-MM-DD HH:MM:SS' (which is UTC)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def to_iso_utc(value) -> str | None:
    dt = parse_utc(value)
    return iso(dt) if dt else value


def seconds_between(start, end) -> int | None:
    a, b = parse_utc(start), parse_utc(end)
    return max(0, round((b - a).total_seconds())) if a and b else None
