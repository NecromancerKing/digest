"""UTC timestamps as stored in the state file: ISO 8601 text that sorts by time."""

from __future__ import annotations

import datetime as dt

FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def iso(t: dt.datetime) -> str:
    """``2026-10-23T09:00:00Z``. Fixed width, so string comparison in SQL orders by time."""
    if t.tzinfo is None:
        raise ValueError("naive datetime")
    return t.astimezone(dt.UTC).strftime(FORMAT)


def parse_iso(s: str) -> dt.datetime:
    t = dt.datetime.fromisoformat(s)
    return t.replace(tzinfo=dt.UTC) if t.tzinfo is None else t.astimezone(dt.UTC)
