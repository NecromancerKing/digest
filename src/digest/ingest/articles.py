"""Optional article-page text (stage 1, step 7; rule L4).

Only for sources with ``fetch_article_pages: true``. A page is kept only when robots.txt
allows it and no TDM reservation applies (site file, HTTP header or HTML meta). The text is
trimmed to ``article_text_max_chars`` and purged after ``FULLTEXT_RETENTION_HOURS``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import trafilatura

from digest.ingest import tdm
from digest.ingest.fetcher import Fetcher, FetchError
from digest.ingest.guard import RequestBlocked

ArticleOutcome = Literal[
    "stored",
    "blocked",
    "robots",
    "tdm_reserved",
    "tdm_unknown",
    "http_error",
    "not_html",
    "no_text",
    "error",
]


@dataclass(frozen=True)
class ArticleResult:
    outcome: ArticleOutcome
    text: str | None = None


def extract_text(html: bytes, url: str, max_chars: int) -> str | None:
    text = trafilatura.extract(
        html,
        url=url,
        include_comments=False,
        include_tables=False,
        include_images=False,
        include_links=False,
        favor_precision=True,
        deduplicate=False,  # its cache spans documents: identical pages would vanish
    )
    if not text or not text.strip():
        return None
    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0]
    return text


def fetch_article(fetcher: Fetcher, url: str, max_chars: int) -> ArticleResult:
    """Fetch one article page and return its main text, or why it was not kept."""
    try:
        fetcher.guard.check(url)  # before tdmrep.json is requested from the article's host
    except RequestBlocked:
        return ArticleResult("blocked")
    site = fetcher.site_tdm(url)
    if site == "reserved":
        return ArticleResult("tdm_reserved")
    if site == "unknown":
        return ArticleResult("tdm_unknown")
    try:
        resp = fetcher.get(url, headers={"Accept": "text/html,application/xhtml+xml"})
    except RequestBlocked as e:
        return ArticleResult("robots" if e.reason == "robots" else "blocked")
    except FetchError:
        return ArticleResult("error")
    if not resp.ok:
        return ArticleResult("http_error")
    # The response is a temporary copy: on a reservation it is dropped right here, unread.
    if tdm.reserved_by_headers(resp.headers) or tdm.reserved_by_html(resp.content):
        return ArticleResult("tdm_reserved")
    if "html" not in resp.headers.get("content-type", "html").lower():
        return ArticleResult("not_html")
    text = extract_text(resp.content, resp.url, max_chars)
    return ArticleResult("stored", text) if text else ArticleResult("no_text")
