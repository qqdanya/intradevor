"""Utility functions for working with application time zones."""
from __future__ import annotations

from datetime import datetime, timezone, tzinfo
from zoneinfo import ZoneInfo

MOSCOW_ZONE = ZoneInfo("Europe/Moscow")


def _detect_local_tz() -> tzinfo:
    """Return the current local time zone, falling back to UTC."""
    current = datetime.now().astimezone().tzinfo
    if current is not None:
        return current
    return timezone.utc


LOCAL_ZONE = _detect_local_tz()


def to_local_time(dt: datetime) -> datetime:
    """Convert the given datetime to the detected local time zone."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MOSCOW_ZONE)
    return dt.astimezone(LOCAL_ZONE)


def format_local_time(dt: datetime, fmt: str = "%d.%m.%Y %H:%M:%S") -> str:
    """Format a datetime using the local time zone."""
    return to_local_time(dt).strftime(fmt)
