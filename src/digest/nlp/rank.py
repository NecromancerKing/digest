"""Stage 4: rank and select (spec section 8). Pure functions, so the site builder (Phase 4)
can order the front page and Today page with the same code.

Score of an open cluster:

    1.0 x ln(1 + independent sources) + 0.5 if any source is official
    + 0.4 x recency (halves every 8 h since the last item) + category weight
    - 0.6 if there is only one source and it is not official
"""

from __future__ import annotations

import datetime as dt
import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from digest.config import RankSettings


@dataclass(frozen=True)
class ClusterView:
    id: int
    category: str | None
    independent_sources: int
    official: bool  # any member source is an authority
    open_license: bool  # any member item is open_license
    last_item_at: dt.datetime


@dataclass(frozen=True)
class Ranked:
    id: int
    category: str | None
    score: float
    at: dt.datetime  # last item time, or first publication time for Today


def recency(last_item_at: dt.datetime, now: dt.datetime, half_life_hours: float) -> float:
    hours = max(0.0, (now - last_item_at).total_seconds() / 3600)
    return 0.5 ** (hours / half_life_hours)


def score(
    c: ClusterView, now: dt.datetime, r: RankSettings, category_weights: Mapping[str, float]
) -> float:
    s = r.source_count_weight * math.log(1 + c.independent_sources)
    if c.official:
        s += r.official_bonus
    s += r.recency_weight * recency(c.last_item_at, now, r.recency_half_life_hours)
    s += category_weights.get(c.category or "", 0.0)
    if c.independent_sources <= 1 and not c.official:
        s -= r.single_source_penalty
    return s


def eligible(c: ClusterView, r: RankSettings) -> bool:
    """At least ``min_independent_sources``, or one official or open-licence source."""
    return (
        c.independent_sources >= r.min_independent_sources
        or (c.independent_sources >= 1 and (c.official or c.open_license))
    ) and c.category is not None


def select(
    candidates: Iterable[Ranked],
    already_today: Mapping[str, int],
    caps: Mapping[str, int],
) -> list[Ranked]:
    """Highest scores first, within what is left of each category's daily cap."""
    used = Counter[str](already_today)
    out: list[Ranked] = []
    for c in sorted(candidates, key=lambda c: (-c.score, c.id)):
        cat = c.category or ""
        if used[cat] < caps.get(cat, 0):
            used[cat] += 1
            out.append(c)
    return out


def _capped(items: Sequence[Ranked], n: int, per_category: int) -> list[Ranked]:
    per = Counter[str]()
    out: list[Ranked] = []
    for c in sorted(items, key=lambda c: (-c.score, c.id)):
        if len(out) >= n:
            break
        cat = c.category or ""
        if per[cat] < per_category:
            per[cat] += 1
            out.append(c)
    return out


def top_stories(stories: Sequence[Ranked], r: RankSettings) -> list[Ranked]:
    """The ``top_stories`` highest scores, at most ``top_stories_max_per_category`` each."""
    return _capped(stories, r.top_stories, r.top_stories_max_per_category)


def today(stories: Sequence[Ranked], now: dt.datetime, r: RankSettings) -> list[Ranked]:
    """Today page: the best stories first published in the window, capped per category."""
    since = now - dt.timedelta(hours=r.today_window_hours)
    recent = [s for s in stories if since <= s.at <= now]
    return _capped(recent, r.today_max_stories, r.today_max_per_category)
