"""Active SMHI weather warnings for the site's warnings box (spec section 10).

Read from the data items the ingest stage stores: SMHI's level, event, areas and times
exactly as published (SMHI's terms forbid changing them). A warning shows while it is in
force or starts within ``UPCOMING_HOURS``, and only while SMHI's feed still lists it: a
warning withdrawn early disappears after a few missed polls.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass

from sqlalchemy import Engine, select

from digest.config import Config
from digest.db.schema import items
from digest.timeutil import iso, parse_iso

LEVELS = {"yellow": 1, "orange": 2, "red": 3}
UPCOMING_HOURS = 24
MISSED_POLLS = 3  # polls a warning may be missing from the feed before it is dropped


@dataclass(frozen=True)
class WeatherWarning:
    level: str  # yellow | orange | red
    event: str
    areas: tuple[str, ...]
    starts_at: dt.datetime | None
    ends_at: dt.datetime | None
    url: str


def active_warnings(engine: Engine, config: Config, now: dt.datetime) -> list[WeatherWarning]:
    sources = {s.id: s for s in config.sources if s.template == "smhi_warning" and s.enabled}
    if not sources:
        return []
    since = now - dt.timedelta(days=7)
    best: dict[str, tuple[int, WeatherWarning]] = {}
    with engine.connect() as conn:
        rows = conn.execute(
            select(items.c.source_id, items.c.url, items.c.data).where(
                items.c.source_id.in_(list(sources)),
                items.c.data.is_not(None),
                items.c.fetched_at >= iso(since),
            )
        ).all()
    for r in rows:
        d = json.loads(r.data)
        level = d.get("level")
        if d.get("template") != "smhi_warning" or level not in LEVELS:
            continue
        poll = dt.timedelta(minutes=sources[r.source_id].poll_minutes * MISSED_POLLS)
        if not d.get("seen_at") or parse_iso(d["seen_at"]) < now - poll:
            continue
        starts = parse_iso(d["starts_at"]) if d.get("starts_at") else None
        ends = parse_iso(d["ends_at"]) if d.get("ends_at") else None
        if ends is not None and ends <= now:
            continue
        if starts is not None and starts > now + dt.timedelta(hours=UPCOMING_HOURS):
            continue
        w = WeatherWarning(
            level=level,
            event=d["event"],
            areas=tuple(d.get("areas") or ()),
            starts_at=starts,
            ends_at=ends,
            url=r.url,
        )
        key = str(d.get("warning_id"))
        if key not in best or LEVELS[level] > best[key][0]:  # an upgrade replaces its warning
            best[key] = (LEVELS[level], w)
    return sorted(
        (w for _, w in best.values()),
        key=lambda w: (-LEVELS[w.level], w.starts_at or now, w.event),
    )
