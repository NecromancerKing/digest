"""What the templates see: allow-listed story fields only (spec section 12).

Facts, claims, gate results and feed text never reach these objects, so they can never
reach the HTML.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Literal

SourceKind = Literal["news", "official", "data"]


@dataclass(frozen=True)
class SourceRef:
    name: str
    url: str
    published_at: dt.datetime
    kind: SourceKind
    lang: str
    headline: str | None = None  # links cards only, and only for display_headlines sources


@dataclass(frozen=True)
class Change:
    """One line of "What changed": our own words, never source text."""

    at: dt.datetime
    note: str


@dataclass(frozen=True)
class Correction:
    """A takedown on the corrections page: the owner's note, our headline if we wrote one."""

    at: dt.datetime
    day: str  # YYYY-MM-DD the story was from
    headline: str | None
    note: str


@dataclass(frozen=True)
class SiteStory:
    id: int
    slug: str
    day: str  # YYYY-MM-DD, from the story's path
    category: str  # category id
    kind: str  # llm | links | template | owner_blurb
    headline: str
    summary: str | None
    why_it_matters: str | None
    sources: tuple[SourceRef, ...]
    independent_sources: int
    rank_score: float
    first_item_at: dt.datetime  # when its cluster began: how young the story is
    published_at: dt.datetime
    updated_at: dt.datetime | None  # set only when a later revision changed what readers see
    changes: tuple[Change, ...] = ()  # newest first
    regions: tuple[str, ...] = ()  # region ids, most named first (D27)

    @property
    def url(self) -> str:
        return f"/story/{self.day}/{self.slug}"

    @property
    def ai(self) -> bool:
        """AI-written text: carries the AI label (rule L11)."""
        return self.kind == "llm"

    @property
    def is_links(self) -> bool:
        return self.kind == "links"

    @property
    def by_editor(self) -> bool:
        return self.kind == "owner_blurb"

    @property
    def last_change_at(self) -> dt.datetime:
        return self.updated_at or self.published_at
