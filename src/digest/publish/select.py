"""What goes where: the front page's blocks and the Today briefing (spec sections 10, 11).

Pure functions over ``SiteStory`` lists, so they test without a database.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from digest.config import AppSettings, Category
from digest.publish.model import SiteStory


@dataclass(frozen=True)
class Section:
    category: Category
    lead: SiteStory | None
    rows: list[SiteStory]
    count_today: int


@dataclass(frozen=True)
class FrontPage:
    lead: SiteStory | None
    top: list[SiteStory]  # the top stories after the lead
    most_covered: list[SiteStory]
    developing: list[SiteStory]
    sections: list[Section]
    latest: list[SiteStory]  # newest first, for the phone layout
    count_today: int


def _by_rank(stories: Iterable[SiteStory]) -> list[SiteStory]:
    return sorted(stories, key=lambda s: (-s.rank_score, s.id))


def _newest(stories: Iterable[SiteStory]) -> list[SiteStory]:
    return sorted(stories, key=lambda s: (s.published_at, s.id), reverse=True)


def recent(stories: Iterable[SiteStory], now: dt.datetime, hours: float) -> list[SiteStory]:
    since = now - dt.timedelta(hours=hours)
    return [s for s in stories if s.last_change_at >= since]


def capped(stories: Iterable[SiteStory], limit: int, per_category: int) -> list[SiteStory]:
    """The first ``limit`` stories, at most ``per_category`` from any one category."""
    out: list[SiteStory] = []
    per = Counter[str]()
    for s in stories:
        if len(out) >= limit:
            break
        if per[s.category] < per_category:
            out.append(s)
            per[s.category] += 1
    return out


def today_stories(
    stories: Sequence[SiteStory], now: dt.datetime, settings: AppSettings
) -> list[SiteStory]:
    """The briefing: most important first, within the window, capped per category."""
    r = settings.rank
    window = recent(stories, now, r.today_window_hours)
    return capped(_by_rank(window), r.today_max_stories, r.today_max_per_category)


def published_today(s: SiteStory, now: dt.datetime, zone: ZoneInfo) -> bool:
    return s.published_at.astimezone(zone).date() == now.astimezone(zone).date()


def front_page(
    stories: Sequence[SiteStory],
    categories: Sequence[Category],
    now: dt.datetime,
    zone: ZoneInfo,
    settings: AppSettings,
    *,
    llm_mode: bool,
) -> FrontPage:
    r, p = settings.rank, settings.publish
    day = recent(stories, now, r.today_window_hours)
    top = capped(_by_rank(day), r.top_stories, r.top_stories_max_per_category)
    # The lead card is built around a summary: prefer the best story that has one.
    lead = next((s for s in top if s.summary), top[0] if top else None)
    rest = [s for s in top if s is not lead]
    shown = {s.id for s in top}

    most_covered = sorted(day, key=lambda s: (-s.independent_sources, -s.rank_score, s.id))[
        : p.glance_most_covered
    ]
    developing: list[SiteStory] = []
    if llm_mode:  # in links mode every card is a links card, so the box would say nothing
        young = now - dt.timedelta(hours=p.developing_max_age_hours)
        developing = sorted(
            (s for s in stories if s.is_links and s.first_item_at >= young),
            key=lambda s: (s.first_item_at, s.id),
            reverse=True,
        )[: p.glance_developing_max]

    window = recent(stories, now, p.category_page_hours)
    sections: list[Section] = []
    for c in categories:
        mine = _newest(s for s in window if s.category == c.id and s.id not in shown)
        sections.append(
            Section(
                category=c,
                lead=mine[0] if mine else None,
                rows=mine[1 : 1 + p.section_rows],
                count_today=sum(
                    1 for s in stories if s.category == c.id and published_today(s, now, zone)
                ),
            )
        )
    return FrontPage(
        lead=lead,
        top=rest,
        most_covered=most_covered,
        developing=developing,
        sections=sections,
        latest=_newest(day)[:5],
        count_today=sum(1 for s in stories if published_today(s, now, zone)),
    )


def category_stories(
    stories: Sequence[SiteStory], category: str, now: dt.datetime, hours: float
) -> list[SiteStory]:
    return _newest(s for s in recent(stories, now, hours) if s.category == category)


def by_day(stories: Sequence[SiteStory]) -> dict[str, list[SiteStory]]:
    """Day archive pages: every story under its path's day, newest first."""
    out: dict[str, list[SiteStory]] = {}
    for s in _newest(stories):
        out.setdefault(s.day, []).append(s)
    return out
