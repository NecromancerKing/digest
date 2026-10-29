"""Where a build gets its stories.

``StorySource`` hides the difference between the state file (Phase 4) and the story files
on the content repository's ``main`` (Phase 5), so templates never change when the source
does. Only approved and published stories are shown; drafts, stories waiting for review,
rejected and unpublished ones never are. Links cards are left out unless ``show_links``
(D28: in llm mode a story without a summary isn't shown).
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
from typing import Any, Protocol

from sqlalchemy import Engine, and_, select

from digest.config import Config
from digest.datastories.render import join_list
from digest.db.schema import cluster_items, clusters, items, stories, story_revisions
from digest.i18n import t
from digest.publish.model import Change, SiteStory, SourceKind, SourceRef
from digest.regions import RegionMatcher
from digest.timeutil import parse_iso

VISIBLE_STATUSES = ("approved", "published")


class StorySource(Protocol):
    def stories(self) -> list[SiteStory]: ...

    def redirects(self) -> list[tuple[str, str]]: ...


class StateStorySource:
    """Stories from the pipeline's state file, through their current revision."""

    def __init__(
        self, engine: Engine, config: Config, lang: str, *, show_links: bool = True
    ) -> None:
        self.engine = engine
        self.config = config
        self.lang = lang
        self.show_links = show_links
        self.regions = RegionMatcher(config.regions)

    def stories(self) -> list[SiteStory]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(
                    stories,
                    clusters.c.created_at.label("cluster_created_at"),
                    clusters.c.source_count,
                )
                .join(clusters, clusters.c.id == stories.c.cluster_id)
                .where(
                    stories.c.status.in_(VISIBLE_STATUSES),
                    stories.c.edition_lang == self.lang,
                    stories.c.current_rev.is_not(None),
                )
            ).all()
            revs: dict[int, list[Any]] = defaultdict(list)
            for r in conn.execute(
                select(story_revisions)
                .join(
                    stories,
                    and_(
                        stories.c.id == story_revisions.c.story_id,
                        stories.c.status.in_(VISIBLE_STATUSES),
                        stories.c.edition_lang == self.lang,
                    ),
                )
                .order_by(story_revisions.c.story_id, story_revisions.c.rev_no)
            ):
                revs[r.story_id].append(r)
            titles: dict[int, list[str]] = defaultdict(list)
            for cid, title in conn.execute(
                select(cluster_items.c.cluster_id, items.c.title)
                .join(items, items.c.id == cluster_items.c.item_id)
                .where(cluster_items.c.cluster_id.in_([r.cluster_id for r in rows]))
            ):
                titles[cid].append(title)
        out: list[SiteStory] = []
        for row in rows:
            history = [r for r in revs[row.id] if r.rev_no <= row.current_rev]
            if history and (self.show_links or history[-1].kind != "links"):
                out.append(self._story(row, history, titles[row.cluster_id]))
        return out

    def redirects(self) -> list[tuple[str, str]]:
        """``(old, new)`` addresses of stories folded into another story's event (D31): their
        cluster merged into one whose story kept the page, so their own went to review. The
        build keeps only those whose new address is on the site."""
        with self.engine.connect() as conn:
            merged: dict[int, int] = {
                r.id: r.merged_into
                for r in conn.execute(
                    select(clusters.c.id, clusters.c.merged_into).where(
                        clusters.c.merged_into.is_not(None)
                    )
                )
            }
            cols = (stories.c.cluster_id, stories.c.status, stories.c.path, stories.c.slug)
            rows = conn.execute(select(*cols).where(stories.c.edition_lang == self.lang)).all()
        url = {r.cluster_id: f"/story/{r.path.split('/')[1]}/{r.slug}" for r in rows}
        out: list[tuple[str, str]] = []
        for r in rows:
            if r.status != "needs_review" or r.cluster_id not in merged:
                continue
            target = r.cluster_id
            seen: set[int] = set()
            while target in merged and target not in seen:
                seen.add(target)
                target = merged[target]
            if target in url:
                out.append((url[r.cluster_id], url[target]))
        return out

    def _story(self, row: Any, history: Sequence[Any], titles: Sequence[str]) -> SiteStory:
        current = history[-1]
        sources = tuple(self._source(s, current.kind) for s in json.loads(current.sources))
        published = parse_iso(row.first_published_at or history[0].created_at)
        return SiteStory(
            id=row.id,
            slug=row.slug,
            day=row.path.split("/")[1],
            category=row.category,
            kind=current.kind,
            headline=current.headline,
            summary=current.summary,
            why_it_matters=current.why_it_matters,
            key_facts=tuple(json.loads(current.key_facts)) if current.key_facts else (),
            sources=sources,
            independent_sources=row.source_count or len({s.name for s in sources}),
            rank_score=row.rank_score,
            first_item_at=parse_iso(row.cluster_created_at),
            published_at=published,
            updated_at=parse_iso(current.created_at) if len(history) > 1 else None,
            changes=tuple(reversed(self._changes(history))),
            # Names in our text and in the reports' titles (facts, D25); never shown.
            regions=self.regions.regions_of([current.headline, current.summary, *titles]),
        )

    def _source(self, s: dict[str, Any], story_kind: str) -> SourceRef:
        kind: SourceKind = "news"
        lang = ""
        try:
            src = self.config.source(s["source_id"])
        except KeyError:
            src = None
        if src is not None:
            kind = "data" if src.template else "official" if src.official else "news"
            lang = src.lang
        return SourceRef(
            name=s["name"],
            url=s["url"],
            published_at=parse_iso(s["published_at"]),
            kind=kind,
            lang=lang,
            # Rule L10: a source headline only on links cards, and only where allowed
            # (the links stage stores one only for display_headlines sources).
            headline=s.get("headline") if story_kind == "links" else None,
        )

    def _changes(self, history: Sequence[Any]) -> list[Change]:
        out: list[Change] = []
        seen: list[str] = []
        prev_kind = None
        for r in history:
            names = [s["name"] for s in json.loads(r.sources)]
            new = [n for n in dict.fromkeys(names) if n not in seen]
            if not out:
                note = t(self.lang, "site.change.first").format(sources=join_list(new, self.lang))
            elif new:
                key = "site.change.joined_one" if len(new) == 1 else "site.change.joined_many"
                note = t(self.lang, key).format(sources=join_list(new, self.lang))
            elif r.kind == "owner_blurb":
                note = t(self.lang, "site.change.by_editor")
            elif r.kind == "llm" and prev_kind != "llm":
                note = t(self.lang, "site.change.summary_written")
            else:
                note = t(self.lang, "site.change.updated")
            out.append(Change(at=parse_iso(r.created_at), note=note))
            seen.extend(new)
            prev_kind = r.kind
        return out
