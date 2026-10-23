"""Stages 2-4 against the state file: embed, cluster (plus maintenance), rank and select.

Each stage reads records in one status and writes the next, so re-running is safe.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, Engine, and_, func, select

from digest.config import Config, Source
from digest.db.schema import cluster_items, clusters, items
from digest.nlp import rank
from digest.nlp.cluster import ClusterAcc, Clusterer, Params, absorb, merge_groups
from digest.nlp.embed import Embedder, EmbedInput
from digest.nlp.entities import extract_entities
from digest.nlp.trigram import similarity
from digest.nlp.vectors import from_blob, to_blob
from digest.timeutil import iso, parse_iso

log = logging.getLogger(__name__)

EMBED_BATCH = 128
EMBED_TEXT_CHARS = 2000  # the model reads ~512 tokens; no point sending more


class Budget(Protocol):
    def remaining(self) -> float: ...


def item_time(published_at: str | None, fetched_at: str) -> dt.datetime:
    return parse_iso(published_at or fetched_at)


class SourceIndex:
    """Registry lookups by source id, tolerant of sources since removed from config."""

    def __init__(self, config: Config) -> None:
        self._by_id: dict[str, Source] = {s.id: s for s in config.sources}

    def key(self, source_id: str) -> str:
        s = self._by_id.get(source_id)
        return s.independence_key if s else source_id

    def official(self, source_id: str) -> bool:
        s = self._by_id.get(source_id)
        return bool(s and s.official)

    def category(self, source_id: str, feed_url: str | None) -> str | None:
        s = self._by_id.get(source_id)
        return s.category_for(feed_url) if s else None


# --------------------------------------------------------------------------- stage 2


def run_embed(engine: Engine, embedder: Embedder, config: Config, budget: Budget) -> dict[str, int]:
    """Embed new items (never ``link_only`` ones) and extract their entities."""
    limit = config.settings.cluster.max_embed_per_run
    with engine.connect() as conn:
        rows = conn.execute(
            select(items.c.id, items.c.title, items.c.feed_summary, items.c.usage_policy)
            .where(items.c.status == "new", items.c.usage_policy != "link_only")
            .order_by(items.c.id)
            .limit(limit)
        ).all()
    done = 0
    for start in range(0, len(rows), EMBED_BATCH):
        if budget.remaining() < 30:
            break
        batch = rows[start : start + EMBED_BATCH]
        inputs = [
            EmbedInput(f"{r.title}\n{r.feed_summary or ''}"[:EMBED_TEXT_CHARS], r.usage_policy)
            for r in batch
        ]
        vectors = embedder.embed(inputs)
        with engine.begin() as conn:
            for r, v in zip(batch, vectors, strict=True):
                conn.execute(
                    items.update()
                    .where(items.c.id == r.id, items.c.usage_policy != "link_only")
                    .values(
                        embedding=to_blob(v),
                        entities=json.dumps(extract_entities(r.title, r.feed_summary)),
                        status="embedded",
                    )
                )
        done += len(batch)
    return {"items_embedded": done, "items_waiting": len(rows) - done}


# --------------------------------------------------------------------------- stage 3


def load_open_clusters(conn: Connection, sources: SourceIndex) -> list[ClusterAcc]:
    members: dict[int, set[str]] = defaultdict(set)
    for cid, sid in conn.execute(
        select(cluster_items.c.cluster_id, items.c.source_id)
        .join(items, items.c.id == cluster_items.c.item_id)
        .join(clusters, clusters.c.id == cluster_items.c.cluster_id)
        .where(clusters.c.is_open == 1)
    ):
        members[cid].add(sources.key(sid))
    out: list[ClusterAcc] = []
    for r in conn.execute(
        select(clusters).where(clusters.c.is_open == 1, clusters.c.centroid.is_not(None))
    ):
        out.append(
            ClusterAcc(
                key=r.id,
                mean=from_blob(r.centroid),
                count=r.item_count,
                last_item_at=parse_iso(r.last_item_at),
                created_at=parse_iso(r.created_at),
                entities=Counter[str](json.loads(r.entity_set)),
                sources=members[r.id],
            )
        )
    return out


def _save_clusters(conn: Connection, accs: Sequence[ClusterAcc], limit: int) -> dict[int, int]:
    """Insert new and update changed clusters. Returns temporary key -> database id."""
    ids: dict[int, int] = {}
    for c in accs:
        values: dict[str, Any] = {
            "last_item_at": iso(c.last_item_at),
            "centroid": to_blob(c.mean),
            "entity_set": json.dumps(c.entity_set(limit)),
            "item_count": c.count,
            "source_count": len(c.sources),
        }
        if c.is_new:
            ids[c.key] = conn.execute(
                clusters.insert()
                .values(created_at=iso(c.created_at), **values)
                .returning(clusters.c.id)
            ).scalar_one()
        else:
            ids[c.key] = c.key
            if c.dirty:
                conn.execute(clusters.update().where(clusters.c.id == c.key).values(**values))
    return ids


def run_cluster(engine: Engine, config: Config, now: dt.datetime) -> dict[str, int]:
    """Cluster embedded items, then attach ``link_only`` items by title."""
    cs = config.settings.cluster
    params = Params.from_settings(cs)
    sources = SourceIndex(config)
    stats = Counter[str]()
    with engine.begin() as conn:
        clusterer = Clusterer(params, load_open_clusters(conn, sources))
        rows = conn.execute(
            select(
                items.c.id,
                items.c.source_id,
                items.c.embedding,
                items.c.entities,
                items.c.published_at,
                items.c.fetched_at,
            ).where(items.c.status == "embedded", items.c.embedding.is_not(None))
        ).all()
        rows = sorted(rows, key=lambda r: (item_time(r.published_at, r.fetched_at), r.id))
        placed: list[tuple[int, ClusterAcc, float]] = []
        for r in rows:
            a = clusterer.assign(
                from_blob(r.embedding),
                json.loads(r.entities or "[]"),
                item_time(r.published_at, r.fetched_at),
                sources.key(r.source_id),
            )
            placed.append((r.id, a.cluster, a.score))
            stats["new_clusters" if a.opened else "items_joined"] += 1
        ids = _save_clusters(conn, clusterer.clusters, cs.entity_set_max)
        for item_id, c, s in placed:
            conn.execute(
                cluster_items.insert().values(
                    cluster_id=ids[c.key], item_id=item_id, similarity=s, added_at=iso(now)
                )
            )
            conn.execute(items.update().where(items.c.id == item_id).values(status="clustered"))
        stats.update(_attach_link_only(conn, config, sources, now))
    return dict(stats)


def _attach_link_only(
    conn: Connection, config: Config, sources: SourceIndex, now: dt.datetime
) -> Counter[str]:
    """Join ``link_only`` items to a cluster by title trigrams; they never open clusters.

    Unmatched items are retried each run until ``link_only_max_hours_apart`` has passed,
    since the matching story may only form later; then they become ``skipped``.
    """
    cs = config.settings.cluster
    span = dt.timedelta(hours=cs.link_only_max_hours_apart)
    stats = Counter[str]()
    pending = conn.execute(
        select(
            items.c.id, items.c.source_id, items.c.title, items.c.published_at, items.c.fetched_at
        ).where(items.c.status == "new", items.c.usage_policy == "link_only")
    ).all()
    if not pending:
        return stats
    members = conn.execute(
        select(
            cluster_items.c.cluster_id,
            items.c.title,
            items.c.published_at,
            items.c.fetched_at,
        )
        .join(items, items.c.id == cluster_items.c.item_id)
        .join(clusters, clusters.c.id == cluster_items.c.cluster_id)
        .where(clusters.c.is_open == 1, items.c.usage_policy != "link_only")
    ).all()
    for p in pending:
        at = item_time(p.published_at, p.fetched_at)
        best: tuple[int, float] | None = None
        for m in members:
            if abs(item_time(m.published_at, m.fetched_at) - at) > span:
                continue
            s = similarity(p.title, m.title)
            if s >= cs.link_only_trigram_threshold and (best is None or s > best[1]):
                best = (m.cluster_id, s)
        if best is not None:
            cid, s = best
            conn.execute(
                cluster_items.insert().values(
                    cluster_id=cid, item_id=p.id, similarity=s, added_at=iso(now)
                )
            )
            conn.execute(
                items.update()
                .where(items.c.id == p.id)
                .values(status="clustered", entities=json.dumps(extract_entities(p.title)))
            )
            _refresh_counts(conn, cid, sources)
            stats["link_only_attached"] += 1
        elif now - parse_iso(p.fetched_at) > span:
            conn.execute(items.update().where(items.c.id == p.id).values(status="skipped"))
            stats["link_only_skipped"] += 1
    return stats


def _refresh_counts(conn: Connection, cluster_id: int, sources: SourceIndex) -> None:
    rows = conn.execute(
        select(items.c.source_id, items.c.published_at, items.c.fetched_at)
        .join(cluster_items, cluster_items.c.item_id == items.c.id)
        .where(cluster_items.c.cluster_id == cluster_id)
    ).all()
    conn.execute(
        clusters.update()
        .where(clusters.c.id == cluster_id)
        .values(
            item_count=len(rows),
            source_count=len({sources.key(r.source_id) for r in rows}),
            last_item_at=iso(max(item_time(r.published_at, r.fetched_at) for r in rows)),
        )
    )


# --------------------------------------------------------------------------- maintenance


def run_maintenance(engine: Engine, config: Config, now: dt.datetime) -> dict[str, int]:
    """Merge near-identical open clusters, then close idle ones and drop their vectors."""
    cs = config.settings.cluster
    params = Params.from_settings(cs)
    sources = SourceIndex(config)
    stats = Counter[str]()
    with engine.begin() as conn:
        for group in merge_groups(load_open_clusters(conn, sources), params):
            winner, losers = group[0], group[1:]
            absorb(winner, losers)
            selected = conn.execute(
                select(func.min(clusters.c.selected_at)).where(
                    clusters.c.id.in_([c.key for c in group])
                )
            ).scalar_one()
            for c in losers:
                conn.execute(
                    cluster_items.update()
                    .where(cluster_items.c.cluster_id == c.key)
                    .values(cluster_id=winner.key)
                )
                conn.execute(
                    clusters.update()
                    .where(clusters.c.id == c.key)
                    .values(is_open=0, merged_into=winner.key, centroid=None, item_count=0)
                )
            _save_clusters(conn, [winner], cs.entity_set_max)
            conn.execute(
                clusters.update().where(clusters.c.id == winner.key).values(selected_at=selected)
            )
            stats["clusters_merged"] += len(losers)

        idle = iso(now - dt.timedelta(hours=cs.close_after_idle_hours))
        closing = [
            r.id
            for r in conn.execute(
                select(clusters.c.id).where(clusters.c.is_open == 1, clusters.c.last_item_at < idle)
            )
        ]
        if closing:
            conn.execute(
                clusters.update().where(clusters.c.id.in_(closing)).values(is_open=0, centroid=None)
            )
            conn.execute(
                items.update()
                .where(
                    items.c.id.in_(
                        select(cluster_items.c.item_id).where(
                            cluster_items.c.cluster_id.in_(closing)
                        )
                    )
                )
                .values(embedding=None)
            )
        stats["clusters_closed"] = len(closing)
    return dict(stats)


# --------------------------------------------------------------------------- stage 4


@dataclass(frozen=True)
class _Member:
    source_id: str
    feed_url: str | None
    usage_policy: str


def cluster_views(conn: Connection, config: Config, now: dt.datetime) -> list[rank.ClusterView]:
    """Ranking inputs for every open cluster, from its member items and the registry."""
    sources = SourceIndex(config)
    members: dict[int, list[_Member]] = defaultdict(list)
    for r in conn.execute(
        select(
            cluster_items.c.cluster_id, items.c.source_id, items.c.feed_url, items.c.usage_policy
        )
        .join(items, items.c.id == cluster_items.c.item_id)
        .join(clusters, clusters.c.id == cluster_items.c.cluster_id)
        .where(clusters.c.is_open == 1)
    ):
        members[r.cluster_id].append(_Member(r.source_id, r.feed_url, r.usage_policy))
    weights = {c.id: c.weight for c in config.categories}
    out: list[rank.ClusterView] = []
    for c in conn.execute(select(clusters).where(clusters.c.is_open == 1)):
        ms = members.get(c.id, [])
        if not ms:
            continue
        category = c.category if c.extraction else _majority_category(ms, sources, weights)
        out.append(
            rank.ClusterView(
                id=c.id,
                category=category,
                independent_sources=len({sources.key(m.source_id) for m in ms}),
                official=any(sources.official(m.source_id) for m in ms),
                open_license=any(m.usage_policy == "open_license" for m in ms),
                last_item_at=parse_iso(c.last_item_at),
            )
        )
    return out


def _majority_category(
    members: Sequence[_Member], sources: SourceIndex, weights: dict[str, float]
) -> str | None:
    """Until stage 5 decides, the category most member feeds are filed under."""
    votes = Counter[str](
        cat for m in members if (cat := sources.category(m.source_id, m.feed_url)) in weights
    )
    if not votes:
        return None
    return max(votes, key=lambda cat: (votes[cat], weights[cat], cat))


def local_day_bounds(now: dt.datetime, zone: ZoneInfo) -> tuple[str, str]:
    """UTC ISO bounds of the Europe/Stockholm calendar day containing ``now``."""
    day = now.astimezone(zone).date()
    start = dt.datetime.combine(day, dt.time(), zone)
    return iso(start), iso(start + dt.timedelta(days=1))


def run_rank(engine: Engine, config: Config, zone: ZoneInfo, now: dt.datetime) -> dict[str, int]:
    """Score open clusters and select new ones for stories, within the daily caps."""
    r = config.settings.rank
    weights = {c.id: c.weight for c in config.categories}
    caps = {c.id: c.daily_cap for c in config.categories}
    with engine.begin() as conn:
        views = cluster_views(conn, config, now)
        scored = {v.id: rank.score(v, now, r, weights) for v in views}
        for v in views:
            conn.execute(
                clusters.update()
                .where(clusters.c.id == v.id)
                .values(score=scored[v.id], category=v.category)
            )
        start, end = local_day_bounds(now, zone)
        today = Counter[str](
            {
                cat or "": n
                for cat, n in conn.execute(
                    select(clusters.c.category, func.count())
                    .where(and_(clusters.c.selected_at >= start, clusters.c.selected_at < end))
                    .group_by(clusters.c.category)
                )
            }
        )
        already = {
            row.id
            for row in conn.execute(
                select(clusters.c.id).where(clusters.c.selected_at.is_not(None))
            )
        }
        candidates = [
            rank.Ranked(v.id, v.category, scored[v.id], v.last_item_at)
            for v in views
            if v.id not in already and rank.eligible(v, r)
        ]
        chosen = rank.select(candidates, today, caps)
        if chosen:
            conn.execute(
                clusters.update()
                .where(clusters.c.id.in_([c.id for c in chosen]))
                .values(selected_at=iso(now))
            )
    return {
        "open_clusters": len(views),
        "eligible": len(candidates),
        "selected_new": len(chosen),
        "selected_today": sum(today.values()) + len(chosen),
        "multi_source_clusters": sum(v.independent_sources >= 2 for v in views),
        "top_score_x100": round(max(scored.values(), default=0) * 100),
    }
