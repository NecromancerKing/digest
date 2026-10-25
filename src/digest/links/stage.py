"""Links mode and the links fallback (spec section 8): a story for every selected cluster.

``SUMMARY_MODE=links``: every selected open cluster gets a links card, or a template story
when it holds an item from a data source. ``SUMMARY_MODE=llm``: the same runs after the
LLM stages for clusters without an approved LLM story (``FALLBACK_TO_LINKS``), so the
site never goes stale, and replaces the LLM story of a cluster that became sensitive
(D19); template stories run in both modes.

Gates here: G4 by keywords (recorded; labels in flagged clusters never name people) and
G5 duplicates against other recent stories' clusters. G2 and G3 are not needed because
no text is generated.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, Engine, select

from digest.config import Config, Settings
from digest.datastories import credit_line
from digest.db.schema import cluster_items, clusters, items, stories
from digest.gates.checks import is_sensitive
from digest.gates.duplicate import find_duplicate
from digest.links.cards import Card, Member, build_card
from digest.nlp.vectors import Vector, from_blob
from digest.stories import (
    Revision,
    StoryRow,
    add_revision,
    create_story,
    find_story,
    is_frozen,
)
from digest.timeutil import iso, parse_iso

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Selected:
    id: int
    category: str
    score: float
    centroid: Vector | None
    sensitive: bool = False  # G4 flags from stage 5: no AI summary (D19)


def load_members(
    conn: Connection, config: Config, cluster_ids: Sequence[int]
) -> dict[int, list[Member]]:
    by_id = {s.id: s for s in config.sources}
    out: dict[int, list[Member]] = defaultdict(list)
    if not cluster_ids:
        return out
    rows = conn.execute(
        select(
            cluster_items.c.cluster_id,
            items.c.id,
            items.c.source_id,
            items.c.url,
            items.c.title,
            items.c.feed_summary,
            items.c.published_at,
            items.c.fetched_at,
            items.c.lang,
            items.c.usage_policy,
        )
        .join(items, items.c.id == cluster_items.c.item_id)
        .where(cluster_items.c.cluster_id.in_(cluster_ids))
        .order_by(items.c.id)
    )
    for r in rows:
        s = by_id.get(r.source_id)
        out[r.cluster_id].append(
            Member(
                item_id=r.id,
                source_id=r.source_id,
                source_name=s.name if s else r.source_id,
                url=r.url,
                title=r.title,
                feed_summary=r.feed_summary,
                published_at=parse_iso(r.published_at or r.fetched_at),
                lang=r.lang,
                usage_policy=r.usage_policy,
                # A source removed from the registry loses its permissions too.
                display_headlines=bool(s and s.display_headlines),
                official=bool(s and s.official),
                template=s.template if s else None,
            )
        )
    return out


def follow_merges(conn: Connection) -> Counter[str]:
    """Move stories of merged-away clusters to the surviving cluster.

    If the survivor already has a story, the other one is a duplicate: it goes to review.
    """
    stats = Counter[str]()
    merged = {
        r.id: r.merged_into
        for r in conn.execute(
            select(clusters.c.id, clusters.c.merged_into).where(clusters.c.merged_into.is_not(None))
        )
    }
    if not merged:
        return stats
    for s in conn.execute(
        select(stories.c.id, stories.c.cluster_id, stories.c.status, stories.c.owner_edited).where(
            stories.c.cluster_id.in_(list(merged))
        )
    ).all():
        target = s.cluster_id
        seen: set[int] = set()
        while target in merged and target not in seen:
            seen.add(target)
            target = merged[target]
        holder = conn.execute(
            select(stories.c.id).where(stories.c.cluster_id == target)
        ).scalar_one_or_none()
        if holder is None:
            conn.execute(stories.update().where(stories.c.id == s.id).values(cluster_id=target))
            stats["stories_moved_after_merge"] += 1
        elif s.status in ("draft", "approved") and not s.owner_edited:
            conn.execute(stories.update().where(stories.c.id == s.id).values(status="needs_review"))
            stats["stories_duplicate_after_merge"] += 1
    return stats


def selected_clusters(conn: Connection) -> list[Selected]:
    return [
        Selected(
            r.id,
            r.category,
            r.score,
            from_blob(r.centroid) if r.centroid else None,
            is_sensitive(json.loads(r.extraction).get("sensitivity", []) if r.extraction else []),
        )
        for r in conn.execute(
            select(
                clusters.c.id,
                clusters.c.category,
                clusters.c.score,
                clusters.c.centroid,
                clusters.c.extraction,
            )
            .where(
                clusters.c.is_open == 1,
                clusters.c.selected_at.is_not(None),
                clusters.c.category.is_not(None),
            )
            .order_by(clusters.c.score.desc(), clusters.c.id)
        )
    ]


def recent_story_vectors(
    conn: Connection, now: dt.datetime, hours: int
) -> list[tuple[int, int, Vector]]:
    """``(story id, cluster id, centroid)`` of open clusters with a story in the window."""
    since = iso(now - dt.timedelta(hours=hours))
    return [
        (r.story_id, r.cluster_id, from_blob(r.centroid))
        for r in conn.execute(
            select(
                stories.c.id.label("story_id"),
                clusters.c.id.label("cluster_id"),
                clusters.c.centroid,
            )
            .join(clusters, clusters.c.id == stories.c.cluster_id)
            .where(stories.c.updated_at >= since, clusters.c.centroid.is_not(None))
        )
    ]


def template_revision(members: Sequence[Member], card: Card, lang: str) -> Revision | None:
    """A template story from the newest data-source item, with the other sources listed."""
    data = [m for m in members if m.template and m.feed_summary]
    if not data:
        return None
    lead = max(data, key=lambda m: (m.published_at, m.item_id))
    assert lead.template is not None and lead.feed_summary is not None
    summary = f"{lead.feed_summary} {credit_line(lead.template, lang)}"
    return Revision(
        kind="template",
        headline=lead.title,
        summary=summary,
        sources=[s.as_dict() for s in card.sources],
        verification={"g4": card.flags},
    )


def links_revision(card: Card) -> Revision:
    return Revision(
        kind="links",
        headline=card.label,
        summary=None,
        sources=[s.as_dict() for s in card.sources],
        verification={"g4": card.flags, "label": card.label_kind},
    )


def run_links(
    engine: Engine, config: Config, settings: Settings, now: dt.datetime, zone: ZoneInfo
) -> dict[str, int]:
    """Links cards and template stories for selected clusters that need one."""
    llm_mode = settings.summary_mode == "llm"
    lang = settings.edition_lang
    names = {c.id: c.names.get(lang) or c.names.get("en") or c.id for c in config.categories}
    gates = config.settings.gates
    stats = Counter[str]()
    with engine.begin() as conn:
        stats.update(follow_merges(conn))
        selected = selected_clusters(conn)
        members = load_members(conn, config, [c.id for c in selected])
        recent = recent_story_vectors(conn, now, gates.duplicate_window_hours)
        for c in selected:
            ms = members.get(c.id)
            if not ms:
                continue
            story = find_story(conn, c.id)
            if story is not None and is_frozen(story):
                stats["frozen"] += 1
                continue
            if story is not None and story.current_kind == "owner_blurb":
                continue
            if story is not None and story.current_kind == "llm" and not c.sensitive:
                continue  # owned by the LLM stages; a cluster that became sensitive is not
            card = build_card(ms, names.get(c.category, c.category), lang)
            rev = template_revision(ms, card, lang) if settings.template_stories else None
            if rev is None:
                if llm_mode and not settings.fallback_to_links:
                    continue
                rev = links_revision(card)
            others = [(sid, v) for sid, cid, v in recent if cid != c.id]
            dup = find_duplicate(c.centroid, others, gates.duplicate_cosine)
            rev = replace(rev, verification={**rev.verification, "g5": dup or "pass"})
            if story is None:
                story = create_story(
                    conn,
                    cluster_id=c.id,
                    category=c.category,
                    slug_basis=card.slug_basis,
                    lang=lang,
                    rank_score=c.score,
                    now=now,
                    zone=zone,
                )
                stats["stories_created"] += 1
                if c.centroid is not None:  # later clusters in this run compare against it
                    recent.append((story.id, c.id, c.centroid))
            if _write(conn, story, rev, c, dup, now):
                stats[f"{rev.kind}_revisions"] += 1
    return dict(stats)


def _write(
    conn: Connection, story: StoryRow, rev: Revision, c: Selected, dup: int | None, now: dt.datetime
) -> bool:
    status = "needs_review" if dup is not None else "approved"
    return add_revision(
        conn, story, rev, status=status, now=now, category=c.category, rank_score=c.score
    )
