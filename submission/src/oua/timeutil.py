"""Clocks. Server timestamps are trusted; IST is UTC+05:30."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def ist_date(value: datetime):
    return as_utc(value).astimezone(IST).date()


def hours_between(start: datetime, end: datetime) -> float:
    return (as_utc(end) - as_utc(start)).total_seconds() / 3600.0


def days_between(start: datetime, end: datetime) -> float:
    return hours_between(start, end) / 24.0
