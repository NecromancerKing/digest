"""Links-mode cards (spec section 8): a label and up to five source lines, no generative AI.

Pure functions over a cluster's member items, so tests can pin every rule:

- The label is the headline of the best member source marked ``display_headlines``,
  prefixed with its name ("Yle News: ..."). Best means official sources first, then the
  earliest report.
- Otherwise the label is built from the category and the two or three most frequent
  named entities ("Economy · Riksbank · Stockholm"). In crime, accident and court
  clusters only known places are used, plus a word for the kind of story, so no person
  is ever named (rule L12).
- Source lines show the source's name, its headline only if ``display_headlines``, the
  time and the link. Feed descriptions and article text are never shown.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from digest.gates.sensitivity import keyword_flags
from digest.i18n import t
from digest.links.places import place_name
from digest.nlp.entities import extract_entities
from digest.timeutil import iso

MAX_SOURCE_LINES = 5
MAX_LABEL_ENTITIES = 3
MAX_LABEL_PLACES = 2

LabelKind = Literal["headline", "entities", "places", "category"]


@dataclass(frozen=True)
class Member:
    """One item of a cluster, with what the registry says about its source."""

    item_id: int
    source_id: str
    source_name: str
    url: str
    title: str
    feed_summary: str | None
    published_at: dt.datetime
    lang: str | None
    usage_policy: str
    display_headlines: bool = False
    official: bool = False
    template: str | None = None  # data source for template stories


@dataclass(frozen=True)
class SourceLine:
    item_id: int
    source_id: str
    name: str
    url: str
    published_at: str
    headline: str | None  # only for sources marked display_headlines

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "source_id": self.source_id,
            "name": self.name,
            "url": self.url,
            "published_at": self.published_at,
            "headline": self.headline,
        }


@dataclass(frozen=True)
class Card:
    label: str
    label_kind: LabelKind
    slug_basis: str  # entity or place label; never a source headline
    sources: list[SourceLine]
    flags: list[str]  # G4 keyword flags: crime, accident, court


def sensitivity(members: Sequence[Member]) -> list[str]:
    """Keyword flags over titles (all members) and feed summaries (where stored)."""
    texts: list[tuple[str, str | None]] = []
    for m in members:
        texts.append((m.title, m.lang))
        if m.feed_summary and m.usage_policy != "link_only":
            texts.append((m.feed_summary, m.lang))
    return keyword_flags(texts)


def _original_case(entity: str, members: Sequence[Member]) -> str:
    pattern = re.compile(rf"(?<![^\W_]){re.escape(entity)}(?![^\W_])", re.IGNORECASE)
    for m in members:
        for text in (m.title, m.feed_summary or ""):
            found = pattern.search(text)
            if found:
                return found.group(0)
    return entity


def ranked_entities(members: Sequence[Member]) -> list[str]:
    """Entities by how many members mention them, then first appearance; original case.

    ``link_only`` members are left out: their text is only for matching.
    """
    counts = Counter[str]()
    order: dict[str, int] = {}
    readable = [m for m in members if m.usage_policy != "link_only" and m.template is None]
    for m in readable:
        for e in extract_entities(m.title, m.feed_summary):
            counts[e] += 1
            order.setdefault(e, len(order))
    ranked = sorted(counts, key=lambda e: (-counts[e], order[e]))
    return [_original_case(e, readable) for e in ranked]


def _distinct(entities: Sequence[str], limit: int) -> list[str]:
    """The first ``limit`` entities that share no word with an earlier choice.

    Drops repeats such as "Eneroth" after "Tomas Eneroth".
    """
    chosen: list[str] = []
    words: set[str] = set()
    for e in entities:
        ws = set(e.lower().split())
        if ws & words:
            continue
        chosen.append(e)
        words |= ws
        if len(chosen) == limit:
            break
    return chosen


def _headline_member(members: Sequence[Member]) -> Member | None:
    eligible = [m for m in members if m.display_headlines and m.title.strip()]
    if not eligible:
        return None
    return min(eligible, key=lambda m: (not m.official, m.published_at, m.item_id))


def entity_label(
    members: Sequence[Member], category_name: str, flags: Sequence[str], lang: str
) -> tuple[str, LabelKind]:
    """The label built without any source headline; safe for sensitive clusters."""
    sep = t(lang, "links.separator")
    lowered_category = category_name.lower()
    entities = [e for e in ranked_entities(members) if e.lower() != lowered_category]
    if flags:
        places: list[str] = []
        for e in entities:
            p = place_name(e, lang)
            if p and p not in places and p.lower() != lowered_category:
                places.append(p)
        parts = [category_name, *places[:MAX_LABEL_PLACES], t(lang, f"links.flag.{flags[0]}")]
        return sep.join(parts), "places"
    chosen = _distinct(entities, MAX_LABEL_ENTITIES)
    if not chosen:
        return category_name, "category"
    return sep.join([category_name, *chosen]), "entities"


def source_lines(members: Sequence[Member], limit: int = MAX_SOURCE_LINES) -> list[SourceLine]:
    """One line per source (its first report), in time order, at most ``limit``."""
    first: dict[str, Member] = {}
    for m in sorted(members, key=lambda m: (m.published_at, m.item_id)):
        first.setdefault(m.source_id, m)
    chosen = sorted(first.values(), key=lambda m: (m.published_at, m.item_id))
    # Keep official sources when there are more sources than lines.
    if len(chosen) > limit:
        official = [m for m in chosen if m.official][:limit]
        rest = [m for m in chosen if not m.official][: limit - len(official)]
        chosen = sorted([*official, *rest], key=lambda m: (m.published_at, m.item_id))
    return [
        SourceLine(
            item_id=m.item_id,
            source_id=m.source_id,
            name=m.source_name,
            url=m.url,
            published_at=iso(m.published_at),
            headline=m.title if m.display_headlines else None,
        )
        for m in chosen
    ]


def build_card(members: Sequence[Member], category_name: str, lang: str) -> Card:
    if not members:
        raise ValueError("a card needs at least one member item")
    flags = sensitivity(members)
    basis, basis_kind = entity_label(members, category_name, flags, lang)
    lead = _headline_member(members)
    if lead is not None:
        label, kind = f"{lead.source_name}: {lead.title.strip()}", "headline"
    else:
        label, kind = basis, basis_kind
    return Card(
        label=label,
        label_kind=kind,
        slug_basis=basis,
        sources=source_lines(members),
        flags=flags,
    )
