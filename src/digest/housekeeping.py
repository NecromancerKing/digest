"""Housekeeping that runs every run (spec sections 7 and 8). Cluster closing is part of the
clustering maintenance pass (``digest.nlp.stages.run_maintenance``)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

from sqlalchemy import Engine, and_, or_, select

from digest.db.schema import cluster_items, clusters, items, llm_calls, runs, stories
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


def prune(
    engine: Engine,
    now: dt.datetime,
    *,
    item_days: int,
    runs_days: int,
    llm_calls_days: int,
) -> dict[str, int]:
    """Delete items ``item_days`` after their cluster closed, and old run logs (section 7).

    A cluster's close time is not stored; its last item time stands in for it. Items that
    never joined a cluster go ``item_days`` after they were fetched.
    """
    item_cutoff = iso(now - dt.timedelta(days=item_days))
    in_old_closed = (
        select(cluster_items.c.item_id)
        .join(clusters, clusters.c.id == cluster_items.c.cluster_id)
        .where(clusters.c.is_open == 0, clusters.c.last_item_at < item_cutoff)
    )
    in_any = select(cluster_items.c.item_id)
    with engine.begin() as conn:
        n_items = conn.execute(
            items.delete().where(
                or_(
                    items.c.id.in_(in_old_closed),
                    and_(items.c.id.not_in(in_any), items.c.fetched_at < item_cutoff),
                )
            )
        ).rowcount
        n_calls = conn.execute(
            llm_calls.delete().where(
                llm_calls.c.created_at < iso(now - dt.timedelta(days=llm_calls_days))
            )
        ).rowcount
        n_runs = conn.execute(
            runs.delete().where(
                runs.c.started_at < iso(now - dt.timedelta(days=runs_days)),
                runs.c.id.not_in(select(llm_calls.c.run_id).where(llm_calls.c.run_id.is_not(None))),
            )
        ).rowcount
    return {"items_deleted": n_items, "llm_calls_deleted": n_calls, "runs_deleted": n_runs}


def remap_categories(engine: Engine, known: Sequence[str], fallback: str) -> int:
    """Move clusters and stories filed under a category no longer in config to
    ``fallback``, so a removed category never breaks the build. Returns rows moved."""
    with engine.begin() as conn:
        moved = conn.execute(
            clusters.update()
            .where(clusters.c.category.is_not(None), clusters.c.category.not_in(known))
            .values(category=fallback)
        ).rowcount
        moved += conn.execute(
            stories.update().where(stories.c.category.not_in(known)).values(category=fallback)
        ).rowcount
    return moved
