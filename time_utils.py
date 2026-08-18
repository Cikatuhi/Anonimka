from datetime import datetime, timezone
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")

def now_msk():
    """Текущее московское время с таймзоной (timezone-aware)"""
    return datetime.now(MSK)


def to_db_timestamp(value):
    """Normalize a datetime for legacy PostgreSQL ``TIMESTAMP`` columns."""
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def as_utc(value):
    """Return a datetime as timezone-aware UTC for safe Python comparisons."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
