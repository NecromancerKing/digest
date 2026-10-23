"""Housekeeping that runs every run (spec section 8). Later phases add cluster closing and
pruning of old items and logs."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import Engine, or_

from digest.db.schema import items
from digest.timeutil import iso


def purge_full_text(engine: Engine, now: dt.datetime, retention_hours: int) -> int:
    """Drop article text whose purge time has passed (rule L5). Returns rows purged.

    Also purges by ``fetched_at``, so a wrong or missing purge time can never keep text
    beyond the retention window.
    """
    cutoff = iso(now - dt.timedelta(hours=retention_hours))
    with engine.begin() as conn:
        return conn.execute(
            items.update()
            .where(
                items.c.full_text.is_not(None),
                or_(
                    items.c.full_text_purge_at.is_(None),
                    items.c.full_text_purge_at <= iso(now),
                    items.c.fetched_at <= cutoff,
                ),
            )
            .values(full_text=None, full_text_purge_at=None)
        ).rowcount
