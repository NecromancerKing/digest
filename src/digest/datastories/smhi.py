"""SMHI impact-based weather warnings as template stories (spec section 4).

API: ``https://opendata-download-warnings.smhi.se/ibww/api/version/1/warning.json``
(documented at opendata.smhi.se/apidocs/IBWwarnings). Terms for warnings: "Informationen
får inte ändras och källan till informationen skall alltid anges", so the templates repeat
SMHI's level, event, areas and times unchanged inside our own sentences, and every story
names SMHI as the source.

One entry per warning and level: an upgrade from yellow to orange is a new entry (and a
new story revision once it clusters), while edits at the same level are not re-published.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from digest.config import TemplateSettings
from digest.datastories.render import join_list, local_time, render
from digest.i18n import t
from digest.ingest.feeds import Entry, FeedParseError

LEVELS = {"MESSAGE": 0, "YELLOW": 1, "ORANGE": 2, "RED": 3}
MAX_AREAS = 3
MAX_COUNTIES = 6


@dataclass(frozen=True)
class Area:
    name: str
    level: str
    start: dt.datetime | None
    end: dt.datetime | None
    published: dt.datetime | None
    detail: str | None
    counties: tuple[str, ...]


def _text(node: object, lang: str) -> str | None:
    if not isinstance(node, Mapping):
        return None
    texts: Mapping[str, Any] = node  # pyright: ignore[reportUnknownVariableType]
    value = texts.get(lang) or texts.get("en") or texts.get("sv")
    return str(value).strip() if value else None


def _time(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        t_ = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t_ if t_.tzinfo else t_.replace(tzinfo=dt.UTC)


def _lower_first(s: str) -> str:
    """``Wind at sea`` -> ``wind at sea``; acronyms stay as they are."""
    if len(s) > 1 and s[1].isupper():
        return s
    return s[:1].lower() + s[1:]


def _county(name: str) -> str:
    """``Skåne County`` -> ``Skåne``; Swedish names keep ``län`` (``Skåne län``)."""
    return name.removesuffix(" County")


def _areas(warning: Mapping[str, Any], lang: str) -> list[Area]:
    out: list[Area] = []
    for a in warning.get("warningAreas") or []:
        if not isinstance(a, Mapping):
            continue
        area: Mapping[str, Any] = a  # pyright: ignore[reportUnknownVariableType]
        level = area.get("warningLevel")
        code = str(level.get("code")) if isinstance(level, Mapping) else ""  # pyright: ignore[reportUnknownMemberType]
        name = _text(area.get("areaName"), lang)
        if code not in LEVELS or not name:
            continue
        counties = tuple(
            _county(n) for c in area.get("affectedAreas") or [] if (n := _text(c, lang))
        )
        out.append(
            Area(
                name=name,
                level=code,
                start=_time(area.get("approximateStart")),
                end=_time(area.get("approximateEnd")),
                published=_time(area.get("published")),
                detail=_text(area.get("eventDescription"), lang),
                counties=counties,
            )
        )
    return out


def parse_warnings(
    content: bytes,
    *,
    lang: str,
    link: str,
    zone: ZoneInfo,
    settings: TemplateSettings,
) -> list[Entry]:
    """Entries in the edition language for warnings at or above the configured level."""
    try:
        data = json.loads(content)
    except ValueError as e:
        raise FeedParseError("SMHI warnings: not JSON") from e
    if not isinstance(data, list):
        raise FeedParseError("SMHI warnings: expected a list")
    minimum = LEVELS[settings.smhi_min_warning_level.upper()]
    excluded = {c.upper() for c in settings.smhi_exclude_events}
    out: list[Entry] = []
    for w in data:  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(w, Mapping):
            continue
        warning: Mapping[str, Any] = w  # pyright: ignore[reportUnknownVariableType]
        event = warning.get("event")
        code = str(event.get("code")) if isinstance(event, Mapping) else ""  # pyright: ignore[reportUnknownMemberType]
        event_name = _text(event, lang)
        if not event_name or code.upper() in excluded or warning.get("id") is None:
            continue
        areas = [a for a in _areas(warning, lang) if LEVELS[a.level] >= minimum]
        if not areas:
            continue
        top = max(LEVELS[a.level] for a in areas)
        level = next(k for k, v in LEVELS.items() if v == top)
        at_top = [a for a in areas if a.level == level]  # SMHI's order
        out.append(_entry(str(warning["id"]), level, event_name, at_top, lang, link, zone))
    return out


def _entry(
    warning_id: str,
    level: str,
    event_name: str,
    areas: Sequence[Area],
    lang: str,
    link: str,
    zone: ZoneInfo,
) -> Entry:
    names = [a.name for a in areas]
    shown = names[:MAX_AREAS]
    if len(names) > MAX_AREAS:
        shown.append(t(lang, "words.more_areas").format(n=len(names) - MAX_AREAS))
    counties: list[str] = []
    for a in areas:
        for c in a.counties:
            if c not in counties and c not in names:
                counties.append(c)
    starts = [a.start for a in areas if a.start]
    ends = [a.end for a in areas if a.end]
    published = [a.published for a in areas if a.published]
    details = {a.detail for a in areas if a.detail and a.detail.lower() != event_name.lower()}
    ctx = {
        "level": t(lang, f"templates.level.{level.lower()}"),
        "a_level": t(lang, f"templates.a_level.{level.lower()}"),
        "event": _lower_first(event_name),
        "detail": _lower_first(sorted(details)[0]) if len(details) == 1 else None,
        "areas": join_list(shown, lang),
        "place": names[0] if len(names) == 1 else t(lang, "words.areas").format(n=len(names)),
        "counties": join_list(counties[:MAX_COUNTIES], lang) if counties else None,
        "start": local_time(min(starts), zone, lang) if starts else None,
        "end": local_time(max(ends), zone, lang) if ends else None,
    }
    headline = render("smhi_warning", lang, "headline", ctx)
    if len(headline) > 70:
        headline = render("smhi_warning", lang, "short_headline", ctx)
    summary = render("smhi_warning", lang, "summary", ctx)
    key = f"smhi-warning:{warning_id}:{level}"
    return Entry(
        title=headline,
        link=link,
        guid=key,
        summary=summary,
        published_at=max(published) if published else None,
        lang=lang,
        dedupe_key=key,
    )
