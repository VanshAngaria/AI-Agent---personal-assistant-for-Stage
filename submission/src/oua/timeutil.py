"""Clocks. Server timestamps (received_at, deliver_at) are trusted; device clocks are not. IST is UTC+05:30."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def ist_date(dt: datetime) -> date:
    return dt.astimezone(IST).date()


def in_quiet_hours(dt: datetime, start: str = "22:00", end: str = "08:00") -> bool:
    """True when dt falls in the IST quiet window [start, end) that wraps midnight."""
    local = dt.astimezone(IST).time()
    s, e = time.fromisoformat(start), time.fromisoformat(end)
    return local >= s or local < e if s > e else s <= local < e


def ist_at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST).astimezone(UTC)


def seconds(a: datetime, b: datetime) -> float:
    """b - a in seconds."""
    return (b - a).total_seconds()
