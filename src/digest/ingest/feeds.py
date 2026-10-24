"""Parse feed documents into entries (stage 1, step 4). Pure: no I/O, no database."""

from __future__ import annotations

import calendar
import datetime as dt
import html
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, cast
from urllib.parse import urljoin

import feedparser  # pyright: ignore[reportMissingTypeStubs]

_feedparser_parse = cast(Callable[..., Any], feedparser.parse)  # pyright: ignore[reportUnknownMemberType]

_WS = re.compile(r"\s+")


class FeedParseError(ValueError):
    """The document is not a feed we can read."""


@dataclass(frozen=True)
class Entry:
    title: str
    link: str  # absolute, not yet canonicalised
    guid: str | None
    summary: str | None  # plain text
    published_at: dt.datetime | None
    lang: str | None
    # Data sources link many entries to one page; this keeps their url_hash apart.
    dedupe_key: str | None = None
    data: dict[str, Any] | None = None  # data sources: the values behind the text


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("p", "br", "div", "li"):
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def plain_text(value: str | None, max_chars: int | None = None) -> str | None:
    """Strip markup and collapse whitespace; None for empty results."""
    if not value:
        return None
    if "<" in value:
        p = _TextExtractor()
        p.feed(value)
        p.close()
        value = "".join(p.parts)
    text = _WS.sub(" ", html.unescape(value)).strip()
    if max_chars is not None and len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0]
    return text or None


def _lang(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().lower().replace("_", "-").split("-")[0][:3] or None


def _struct_time(value: object) -> dt.datetime | None:
    if value is None:
        return None
    try:
        return dt.datetime.fromtimestamp(calendar.timegm(cast(Any, value)), dt.UTC)
    except (TypeError, ValueError, OverflowError):
        return None


def _parse_rss_atom(content: bytes, base_url: str, title_max: int, summary_max: int) -> list[Entry]:
    parsed = _feedparser_parse(content)  # no base URL: GUIDs stay as published
    raw_entries = cast(list[Any], parsed.get("entries") or [])
    if not raw_entries and parsed.get("bozo") and not parsed.get("feed"):
        raise FeedParseError("not a feed")
    feed_lang = _lang(cast(Any, parsed.get("feed") or {}).get("language"))
    out: list[Entry] = []
    for e in raw_entries:
        title = plain_text(e.get("title"), title_max)
        link = e.get("link")
        if not title or not isinstance(link, str) or not link.strip():
            continue
        out.append(
            Entry(
                title=title,
                link=urljoin(base_url, link.strip()),
                guid=(str(e.get("id")).strip() or None) if e.get("id") else None,
                summary=plain_text(e.get("summary"), summary_max),
                published_at=_struct_time(e.get("published_parsed") or e.get("updated_parsed")),
                lang=_lang(e.get("language")) or feed_lang,
            )
        )
    return out


def _iso_or_none(value: object) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        t = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t.astimezone(dt.UTC) if t.tzinfo else t.replace(tzinfo=dt.UTC)


def _parse_json_feed(
    content: bytes, base_url: str, title_max: int, summary_max: int
) -> list[Entry]:
    """JSON Feed 1.0/1.1 (jsonfeed.org)."""
    try:
        doc: object = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise FeedParseError("invalid JSON") from e
    if not isinstance(doc, dict):
        raise FeedParseError("not a JSON Feed")
    feed = cast(dict[str, Any], doc)
    if not isinstance(feed.get("items"), list):
        raise FeedParseError("not a JSON Feed")
    feed_lang = _lang(feed.get("language"))
    out: list[Entry] = []
    for item in cast(list[Any], feed["items"]):
        if not isinstance(item, dict):
            continue
        it = cast(dict[str, Any], item)
        title = plain_text(it.get("title"), title_max)
        link = it.get("url") or it.get("external_url")
        if not title or not isinstance(link, str) or not link.strip():
            continue
        summary = it.get("summary") or it.get("content_text") or it.get("content_html")
        out.append(
            Entry(
                title=title,
                link=urljoin(base_url, link.strip()),
                guid=str(it["id"]) if it.get("id") is not None else None,
                summary=plain_text(summary if isinstance(summary, str) else None, summary_max),
                published_at=_iso_or_none(it.get("date_published") or it.get("date_modified")),
                lang=_lang(it.get("language")) or feed_lang,
            )
        )
    return out


def parse_feed(
    content: bytes, kind: str, base_url: str, *, title_max: int = 300, summary_max: int = 2000
) -> list[Entry]:
    """Entries with a title and a link. Raises ``FeedParseError`` for unreadable documents."""
    if kind in ("rss", "atom"):
        return _parse_rss_atom(content, base_url, title_max, summary_max)
    if kind == "json_api":
        return _parse_json_feed(content, base_url, title_max, summary_max)
    raise FeedParseError(f"feed kind {kind} is not parsed by ingest")
