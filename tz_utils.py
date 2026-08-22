"""Time-zone aware datetime helpers.

Migrated from pytz to stdlib `zoneinfo` so the project has a single TZ library
(the only other one used by `scheduler.py` was already `zoneinfo`).
`tzdata` is required for IANA zones to resolve on minimal Linux images where
`/usr/share/zoneinfo` is stripped — pinned in requirements.txt.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE: str = "Asia/Almaty"


def _tz(timezone: str = DEFAULT_TIMEZONE) -> ZoneInfo:
    """Resolve a IANA TZ name. Raises ZoneInfoNotFoundError on garbage input."""
    return ZoneInfo(timezone)


def get_now(timezone: str = DEFAULT_TIMEZONE) -> datetime:
    """Wall-clock-aware current datetime in the given IANA timezone."""
    return datetime.now(_tz(timezone))


def format_datetime(dt: datetime, timezone: str = DEFAULT_TIMEZONE) -> str:
    """Format an aware datetime into 'YYYY-MM-DD HH:MM' in the given zone."""
    return dt.astimezone(_tz(timezone)).strftime("%Y-%m-%d %H:%M")


def parse_datetime(date_str: str, time_str: str, timezone: str = DEFAULT_TIMEZONE) -> datetime:
    """Parse 'YYYY-MM-DD' + 'HH:MM' into a TZ-aware datetime.

    The result represents the local wall-clock time in `timezone`. We attach
    the zone via `replace(tzinfo=…)` rather than localising because the input
    is a wall time (e.g. "10:00") and not a precise instant. DST-aware
    handling is not needed for "is this in the past?" queries.
    """
    base = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    return base.replace(tzinfo=_tz(timezone))


def get_today(timezone: str = DEFAULT_TIMEZONE) -> str:
    return get_now(timezone).strftime("%Y-%m-%d")


def get_tomorrow(timezone: str = DEFAULT_TIMEZONE) -> str:
    return (get_now(timezone) + timedelta(days=1)).strftime("%Y-%m-%d")


def get_current_time(timezone: str = DEFAULT_TIMEZONE) -> str:
    return get_now(timezone).strftime("%H:%M")


def is_past(date_str: str, time_str: str, timezone: str = DEFAULT_TIMEZONE) -> bool:
    dt = parse_datetime(date_str, time_str, timezone)
    return dt < get_now(timezone)
