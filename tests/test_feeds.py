from __future__ import annotations

import datetime as dt
import json

import pytest

from digest.ingest.canonical import canonicalize, url_hash
from digest.ingest.feeds import FeedParseError, parse_feed, plain_text
from tests.fake_web import item, rss

BASE = "https://news.example/feed.xml"


# --------------------------------------------------------------------------- canonical URLs


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("HTTPS://News.Example/a?utm_source=x&id=7#top", "https://news.example/a?id=7"),
        ("https://news.example/a?fbclid=1&gclid=2&UTM_Medium=3", "https://news.example/a"),
        ("https://news.example:443/a", "https://news.example/a"),
        ("http://news.example:8080/a", "http://news.example:8080/a"),
        ("https://news.example", "https://news.example/"),
        ("https://news.example/a?q=a%20b&x=", "https://news.example/a?q=a%20b&x="),
        ("https://news.example/Path/Kept", "https://news.example/Path/Kept"),
    ],
)
def test_canonicalize(raw: str, canonical: str) -> None:
    assert canonicalize(raw) == canonical


def test_tracking_variants_share_a_hash() -> None:
    a = url_hash(canonicalize("https://news.example/a?utm_campaign=rss"))
    b = url_hash(canonicalize("https://NEWS.example/a#comments"))
    assert a == b
    assert len(a) == 32


# --------------------------------------------------------------------------- parsing


def test_rss_entries() -> None:
    doc = rss(
        item(
            "First &amp; foremost",
            "/a?utm_source=rss",
            guid="g1",
            desc="<p>Some <b>bold</b>\n text</p>",
            date="Fri, 23 Oct 2026 08:00:00 GMT",
        ),
        item("No link", ""),
        lang="sv-SE",
    )
    [e] = parse_feed(doc, "rss", BASE)
    assert e.title == "First & foremost"
    assert e.link == "https://news.example/a?utm_source=rss"  # canonicalised later
    assert e.guid == "g1"
    assert e.summary == "Some bold text"
    assert e.published_at == dt.datetime(2026, 10, 23, 8, 0, tzinfo=dt.UTC)
    assert e.lang == "sv"


def test_atom_entries() -> None:
    doc = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" xml:lang="en">
      <title>t</title><entry><title>Atom entry</title><id>urn:1</id>
      <link href="https://news.example/atom/1"/><updated>2026-10-23T07:30:00Z</updated>
      <summary>Short</summary></entry></feed>"""
    [e] = parse_feed(doc, "atom", BASE)
    assert (e.title, e.link, e.guid, e.summary) == (
        "Atom entry",
        "https://news.example/atom/1",
        "urn:1",
        "Short",
    )
    assert e.published_at == dt.datetime(2026, 10, 23, 7, 30, tzinfo=dt.UTC)


def test_json_feed_entries() -> None:
    doc = json.dumps(
        {
            "version": "https://jsonfeed.org/version/1.1",
            "language": "en",
            "items": [
                {
                    "id": 5,
                    "url": "https://news.example/j/5",
                    "title": "JSON item",
                    "content_html": "<p>Body</p>",
                    "date_published": "2026-10-23T06:00:00Z",
                },
                {"id": 6, "title": "no url"},
            ],
        }
    ).encode()
    [e] = parse_feed(doc, "json_api", BASE)
    assert (e.guid, e.summary, e.lang) == ("5", "Body", "en")


def test_summary_is_trimmed_at_a_word() -> None:
    assert plain_text("one two three four", max_chars=9) == "one two"


@pytest.mark.parametrize(("content", "kind"), [(b"not json", "json_api"), (b"{}", "json_api")])
def test_unreadable_documents_raise(content: bytes, kind: str) -> None:
    with pytest.raises(FeedParseError):
        parse_feed(content, kind, BASE)


def test_data_api_is_not_parsed_by_ingest() -> None:
    with pytest.raises(FeedParseError):
        parse_feed(b"{}", "data_api", BASE)
