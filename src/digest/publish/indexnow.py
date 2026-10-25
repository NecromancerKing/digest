"""Tell search engines which pages changed after a deploy (IndexNow: Bing, Yandex, Seznam, ...).

The key is not a secret: it is published as ``/<key>.txt`` so engines can check that the
ping came from the site's owner. It is derived from ``SITE_URL``, so there is nothing to set
up. Only runs when the site is indexable (``SITE_URL`` set, ``SITE_NOINDEX`` off). A failed
ping is logged by status only and never fails the run: the sitemap still covers every page.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
from collections.abc import Sequence
from urllib.parse import urlsplit

import httpx

from digest.config import Settings
from digest.publish.model import SiteStory

log = logging.getLogger(__name__)

ENDPOINT = "https://api.indexnow.org/indexnow"
MAX_URLS = 10_000  # per request (IndexNow protocol)


def enabled(settings: Settings) -> bool:
    return bool(settings.site_url) and not settings.site_noindex


def key(site_url: str) -> str:
    return hashlib.sha256(f"indexnow:{site_url.rstrip('/')}".encode()).hexdigest()[:32]


def changed_urls(
    site_url: str, stories: Sequence[SiteStory], since: dt.datetime | None
) -> list[str]:
    """The front pages, and the stories new or changed since the last deploy (all on the first)."""
    base = site_url.rstrip("/")
    urls = [f"{base}/", f"{base}/today"]
    urls += [base + s.url for s in stories if since is None or s.last_change_at > since]
    return urls[:MAX_URLS]


def ping(site_url: str, urls: Sequence[str], *, client: httpx.Client | None = None) -> int:
    """Submit ``urls``; returns the HTTP status (0 when the request failed)."""
    base = site_url.rstrip("/")
    k = key(site_url)
    body = {
        "host": urlsplit(base).hostname,
        "key": k,
        "keyLocation": f"{base}/{k}.txt",
        "urlList": list(urls),
    }
    try:
        if client is None:
            with httpx.Client(timeout=30) as own:
                r = own.post(ENDPOINT, json=body)
        else:
            r = client.post(ENDPOINT, json=body)
    except httpx.HTTPError as e:
        log.warning("indexnow_failed", extra={"error": type(e).__name__})
        return 0
    if r.status_code >= 300:  # 200 OK, 202 accepted while the key is being checked
        log.warning("indexnow_rejected", extra={"status": r.status_code})
    return r.status_code
