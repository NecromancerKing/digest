"""Our own feeds and the sitemap (spec sections 10 and 11): RSS 2.0, JSON Feed 1.1.

Only our stories: our headline, our summary (or, for a links card, who reported it) and
our link. Source text never goes into a feed.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable, Sequence
from email.utils import format_datetime
from xml.etree import ElementTree as ET

from digest.publish.model import SiteStory

FEED_SIZE = 50
NEWS_SITEMAP_HOURS = 48  # Google's news sitemaps hold the last two days
NEWS_SITEMAP_MAX = 1000


def _newest(stories: Sequence[SiteStory]) -> list[SiteStory]:
    return sorted(stories, key=lambda s: (s.published_at, s.id), reverse=True)[:FEED_SIZE]


def rss(
    stories: Sequence[SiteStory],
    *,
    title: str,
    description: str,
    home: str,
    self_url: str,
    lang: str,
    now: dt.datetime,
    abs_url: Callable[[str], str],
    text: Callable[[SiteStory], str],
    category: Callable[[str], str],
) -> str:
    atom = "http://www.w3.org/2005/Atom"
    ET.register_namespace("atom", atom)
    root = ET.Element("rss", {"version": "2.0"})
    ch = ET.SubElement(root, "channel")
    ET.SubElement(ch, "title").text = title
    ET.SubElement(ch, "link").text = home
    ET.SubElement(ch, "description").text = description
    ET.SubElement(ch, "language").text = lang
    ET.SubElement(ch, "lastBuildDate").text = format_datetime(now.astimezone(dt.UTC), usegmt=True)
    ET.SubElement(
        ch, f"{{{atom}}}link", {"href": self_url, "rel": "self", "type": "application/rss+xml"}
    )
    for s in _newest(stories):
        item = ET.SubElement(ch, "item")
        url = abs_url(s.url)
        ET.SubElement(item, "title").text = s.headline
        ET.SubElement(item, "link").text = url
        ET.SubElement(item, "guid", {"isPermaLink": "true"}).text = url
        ET.SubElement(item, "pubDate").text = format_datetime(
            s.published_at.astimezone(dt.UTC), usegmt=True
        )
        ET.SubElement(item, "category").text = category(s.category)
        ET.SubElement(item, "description").text = text(s)
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="unicode")


def json_feed(
    stories: Sequence[SiteStory],
    *,
    title: str,
    description: str,
    home: str,
    self_url: str,
    lang: str,
    abs_url: Callable[[str], str],
    text: Callable[[SiteStory], str],
    category: Callable[[str], str],
) -> str:
    feed = {
        "version": "https://jsonfeed.org/version/1.1",
        "title": title,
        "description": description,
        "home_page_url": home,
        "feed_url": self_url,
        "language": lang,
        "items": [
            {
                "id": abs_url(s.url),
                "url": abs_url(s.url),
                "title": s.headline,
                "content_text": text(s),
                "date_published": s.published_at.astimezone(dt.UTC).isoformat(),
                "date_modified": s.last_change_at.astimezone(dt.UTC).isoformat(),
                "tags": [category(s.category)],
            }
            for s in _newest(stories)
        ],
    }
    return json.dumps(feed, ensure_ascii=False, indent=1) + "\n"


def sitemap(urls: Sequence[tuple[str, dt.datetime | None]]) -> str:
    ns = "http://www.sitemaps.org/schemas/sitemap/0.9"
    root = ET.Element("urlset", {"xmlns": ns})
    for loc, lastmod in urls:
        u = ET.SubElement(root, "url")
        ET.SubElement(u, "loc").text = loc
        if lastmod is not None:
            ET.SubElement(u, "lastmod").text = lastmod.astimezone(dt.UTC).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="unicode")


def news_sitemap(
    stories: Sequence[SiteStory],
    *,
    name: str,
    lang: str,
    now: dt.datetime,
    abs_url: Callable[[str], str],
) -> str:
    """Google's news sitemap (D29): our stories of the last two days, newest first."""
    ns = "http://www.sitemaps.org/schemas/sitemap/0.9"
    news = "http://www.google.com/schemas/sitemap-news/0.9"
    ET.register_namespace("news", news)
    root = ET.Element("urlset", {"xmlns": ns})
    since = now - dt.timedelta(hours=NEWS_SITEMAP_HOURS)
    fresh = sorted(
        (s for s in stories if s.published_at >= since),
        key=lambda s: (s.published_at, s.id),
        reverse=True,
    )
    for s in fresh[:NEWS_SITEMAP_MAX]:
        u = ET.SubElement(root, "url")
        ET.SubElement(u, "loc").text = abs_url(s.url)
        item = ET.SubElement(u, f"{{{news}}}news")
        pub = ET.SubElement(item, f"{{{news}}}publication")
        ET.SubElement(pub, f"{{{news}}}name").text = name
        ET.SubElement(pub, f"{{{news}}}language").text = lang
        ET.SubElement(item, f"{{{news}}}publication_date").text = s.published_at.astimezone(
            dt.UTC
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        ET.SubElement(item, f"{{{news}}}title").text = s.headline
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="unicode")


def robots(sitemap_urls: Sequence[str], *, index: bool) -> str:
    """Everything open to crawlers, or nothing while the site is a preview (shadow week)."""
    rule = "Allow: /" if index else "Disallow: /"
    lines = "".join(f"Sitemap: {u}\n" for u in sitemap_urls)
    return f"User-agent: *\n{rule}\n\n{lines}"
