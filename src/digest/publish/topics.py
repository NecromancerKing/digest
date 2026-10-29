"""Topic pages (D32): one page per name that ``publish.topic_min_stories`` stories share.

Names come from stage 5's extraction (``clusters.extraction``): organisations, places and
people, but people only where the model marked them a public figure, so a private person
named in a crime or court story never gets a page (content filters are off, D28). News
outlets that reports cite are no topic: the news sources' names and the skip list in
``config/topics.yaml``. Spellings that differ only in accents, case, dashes, dots or
apostrophes are one name, and the file's aliases join the rest. No AI calls. Stories leave
the site after ``publish.keep_days`` (D30), so a topic covers at most that long and its
page goes when it has too few stories left.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from digest.config import Config
from digest.nlp.entities import APOSTROPHES
from digest.publish.model import Name, SiteStory
from digest.stories import slugify

KINDS = ("person", "org", "place")  # a name's kind: what stage 5 called it most, ties in this order
RELATED_MIN_SHARED = 2  # stories two topics must share to be shown as related
# Letters NFKD can't take apart, and marks that don't make another name ("U.S.", "Yemen's").
_FOLD = str.maketrans(
    {"ø": "o", "Ø": "O", "æ": "ae", "Æ": "Ae", "ß": "ss", "ł": "l", "Ł": "L"}
    | dict.fromkeys("." + APOSTROPHES)
)
_OUTLET_SUFFIX = re.compile(r"\s+(?:\([^)]*\)|English|News)$")


@dataclass(frozen=True)
class Topic:
    slug: str
    name: str
    kind: str  # person | org | place
    stories: tuple[SiteStory, ...]  # newest first

    @property
    def url(self) -> str:
        return f"/topic/{self.slug}/"


def clean(name: str) -> str:
    """One spelling: every dash a hyphen, single spaces."""
    dashes = "".join("-" if unicodedata.category(c) == "Pd" else c for c in name)
    return " ".join(dashes.split())


def topic_slug(name: str) -> str:
    """``Luiz Inácio Lula da Silva`` -> ``luiz-inacio-lula-da-silva``; ``U.S.`` and ``US``
    share one. Empty for a name without Latin letters or digits."""
    text = clean(name).translate(_FOLD)
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return slugify(ascii_text) if re.search(r"[A-Za-z0-9]{2}", ascii_text) else ""


def names_of(extraction: Mapping[str, Any] | None) -> tuple[Name, ...]:
    """A story's names from its cluster's extraction: public figures, organisations, places."""
    ents: Mapping[str, Any] = (extraction or {}).get("entities") or {}
    out = [
        Name(clean(p["name"]), "person")
        for p in ents.get("people", [])
        if p.get("public_figure") and p.get("name")
    ]
    out += [Name(clean(o), "org") for o in ents.get("orgs", []) if o]
    out += [Name(clean(p), "place") for p in ents.get("places", []) if p]
    return tuple(dict.fromkeys(out))


def _outlet_names(name: str) -> list[str]:
    """A news source's name and its short forms: ``ABC News (Australia)``, ``ABC News``,
    ``ABC``."""
    out = [name]
    while (short := _OUTLET_SUFFIX.sub("", out[-1])) != out[-1]:
        out.append(short)
    return out


def _spelling(seen: Counter[str]) -> str:
    """The name as shown: accented spellings first ("Kylian Mbappé"), then the most used."""
    return min(seen, key=lambda n: (n.isascii(), -seen[n], n))


def find_topics(stories: Sequence[SiteStory], config: Config) -> list[Topic]:
    """The site's topics, biggest first, at most ``publish.max_topics``."""
    p = config.settings.publish
    outlets = [
        n
        for s in config.sources
        if not s.official and s.template is None
        for n in _outlet_names(s.name)
    ]
    skip = {topic_slug(n) for n in [*outlets, *config.topics.skip]}
    alias = {topic_slug(k): clean(v) for k, v in config.topics.aliases.items()}
    seen: dict[str, Counter[str]] = defaultdict(Counter)
    kinds: dict[str, Counter[str]] = defaultdict(Counter)
    members: dict[str, dict[int, SiteStory]] = defaultdict(dict)
    for s in stories:
        for n in s.names:
            text = alias.get(topic_slug(n.text), n.text)
            slug = topic_slug(text)
            if not slug or slug in skip:
                continue
            seen[slug][text] += 1
            kinds[slug][n.kind] += 1
            members[slug][s.id] = s
    found = [
        Topic(
            slug=slug,
            name=_spelling(seen[slug]),
            kind=min(KINDS, key=lambda k: (-kinds[slug][k], KINDS.index(k))),
            stories=tuple(
                sorted(mine.values(), key=lambda s: (s.published_at, s.id), reverse=True)
            ),
        )
        for slug, mine in members.items()
        if len(mine) >= p.topic_min_stories
    ]
    # A name on exactly the same stories as a shorter name inside it would only repeat that
    # page ("Karak governorate" and "Karak"); "Houthis" and "Yemen" both stay.
    same: dict[frozenset[int], list[Topic]] = defaultdict(list)
    for t in sorted(found, key=lambda t: (len(t.slug), t.slug)):
        same[frozenset(s.id for s in t.stories)].append(t)
    kept = [
        t
        for group in same.values()
        for i, t in enumerate(group)
        if not any(set(k.slug.split("-")) <= set(t.slug.split("-")) for k in group[:i])
    ]
    ranked = sorted(
        kept,
        key=lambda t: (-len(t.stories), -t.stories[0].published_at.timestamp(), t.slug),
    )
    return ranked[: p.max_topics]


def by_story(topics: Sequence[Topic]) -> dict[int, list[Topic]]:
    """Each story's topics, biggest first."""
    out: dict[int, list[Topic]] = defaultdict(list)
    for t in topics:
        for s in t.stories:
            out[s.id].append(t)
    return dict(out)


def related(topic: Topic, story_topics: Mapping[int, Sequence[Topic]], limit: int) -> list[Topic]:
    """The topics that share the most stories with ``topic``, at least two of them."""
    shared = Counter[str]()
    by_slug: dict[str, Topic] = {}
    for s in topic.stories:
        for t in story_topics.get(s.id, ()):
            if t.slug != topic.slug:
                shared[t.slug] += 1
                by_slug[t.slug] = t
    ranked = sorted(shared, key=lambda slug: (-shared[slug], -len(by_slug[slug].stories), slug))
    return [by_slug[slug] for slug in ranked if shared[slug] >= RELATED_MIN_SHARED][:limit]
