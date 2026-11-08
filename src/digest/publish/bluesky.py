"""Post new stories to Bluesky (D39), so people who don't know the site can find them.

After each publish, the highest-ranked AI story that is live on the site, new (younger than
``bluesky.max_story_age_hours``) and not yet posted is posted, at most one per run, at most
``bluesky.posts_per_day`` a local day and ``bluesky.min_minutes_between_posts`` apart. The
post is our own words only: the headline, how many sources the summary came from, and
hashtags (the common ones, the section's, and the country of the story's main place), with
a link card carrying the story's share image (D29). Bluesky's topic feeds
pick posts up by their hashtags, which is how an account with no followers gets seen.

Runs only when ``BLUESKY_HANDLE`` and ``BLUESKY_APP_PASSWORD`` are set and the site is
public. A failure is logged by status only and never fails the run or the deploy; the story
is then tried again on the next run.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import Engine

from digest.config import BlueskySettings, Config, Settings
from digest.publish import maps
from digest.publish.model import SiteStory
from digest.timeutil import iso, parse_iso

log = logging.getLogger(__name__)

KV_POSTED = "bluesky.posted"  # JSON list of {"story": id, "at": iso, "uri": at-uri}, newest last
KEEP_POSTED = 500  # far more than max_story_age_hours ever needs
MAX_TEXT = 300  # Bluesky counts graphemes; we count code points, which is never fewer
MAX_DESCRIPTION = 300
MAX_THUMB_BYTES = 976_560  # Bluesky's limit for an image blob
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
# The story's country as a hashtag: the forms people use, where they differ from the name.
PLACE_TAGS = {
    "US": "USA", "GB": "UK", "AE": "UAE", "CD": "DRC", "CI": "IvoryCoast", "TR": "Turkey",
    "VA": "Vatican", "BA": "Bosnia", "PS": "Palestine",
}  # fmt: skip
PLACE_TAGS_BY_NAME = {  # a named part of a country that is its own, bigger tag
    "gaza": "Gaza", "gaza strip": "Gaza", "gazaremsan": "Gaza",
    "west bank": "WestBank", "västbanken": "WestBank",
}  # fmt: skip
WORD = re.compile(r"[^\W_]+")


def enabled(settings: Settings) -> bool:
    return (
        bool(settings.bluesky_handle)
        and settings.bluesky_app_password is not None
        and bool(settings.site_url)
        and not settings.site_noindex
    )


@dataclass(frozen=True)
class Posted:
    story: int
    at: dt.datetime
    uri: str


def load_posted(engine: Engine) -> list[Posted]:
    from digest.runner import get_kv

    raw = get_kv(engine, KV_POSTED)
    if not raw:
        return []
    return [Posted(int(p["story"]), parse_iso(p["at"]), str(p["uri"])) for p in json.loads(raw)]


def save_posted(engine: Engine, posted: Sequence[Posted]) -> None:
    from digest.runner import set_kv

    keep = list(posted)[-KEEP_POSTED:]
    set_kv(
        engine,
        KV_POSTED,
        json.dumps([{"story": p.story, "at": iso(p.at), "uri": p.uri} for p in keep]),
    )


def pick(
    stories: Sequence[SiteStory],
    posted: Sequence[Posted],
    rules: BlueskySettings,
    *,
    now: dt.datetime,
    live_since: dt.datetime | None,
    zone: dt.tzinfo,
) -> SiteStory | None:
    """The story to post now, or None when nothing is due."""
    if live_since is None or rules.posts_per_day == 0:
        return None
    if posted:
        last = max(p.at for p in posted)
        if now - last < dt.timedelta(minutes=rules.min_minutes_between_posts):
            return None
        today = now.astimezone(zone).date()
        if sum(p.at.astimezone(zone).date() == today for p in posted) >= rules.posts_per_day:
            return None
    done = {p.story for p in posted}
    oldest = now - dt.timedelta(hours=rules.max_story_age_hours)
    due = [
        s
        for s in stories
        if s.ai
        and s.id not in done
        and oldest <= s.published_at <= live_since  # live: in a deployed build
        and s.independent_sources >= rules.min_sources
    ]
    return max(due, key=lambda s: (s.rank_score, s.published_at), default=None)


def place_tag(story: SiteStory) -> str | None:
    """The country of the story's main place (the one its map shows), as a hashtag.

    Countries rather than towns: far more people follow #Ukraine than #Kyiv.
    """
    found = maps.main_place(story.names, story.place_text)
    if found is None:
        return None
    code, text = found
    if (own := PLACE_TAGS_BY_NAME.get(text.strip().lower())) is not None:
        return own
    if (own := PLACE_TAGS.get(code)) is not None:
        return own
    words = WORD.findall(maps.country_label(code, "en"))
    tag = "".join(w[:1].upper() + w[1:] for w in words)
    return tag if tag[:1].isalpha() and len(tag) <= 64 else None


def hashtags(story: SiteStory, rules: BlueskySettings) -> list[str]:
    """The common tags, the section's, and the story's country."""
    tags = [*rules.common_hashtags, *rules.hashtags.get(story.category, [])]
    if rules.place_hashtag and (place := place_tag(story)) is not None:
        tags.append(place)
    return list(dict.fromkeys(tags))


def post_text(story: SiteStory, tags: Sequence[str]) -> tuple[str, list[dict[str, Any]]]:
    """The post's text and its hashtag facets (byte offsets in UTF-8, as Bluesky wants)."""
    n = story.independent_sources
    note = f"AI summary from {n} sources." if n > 1 else "AI summary."
    tail = f"\n\n{note}"
    tag_text = " ".join(f"#{t}" for t in tags)
    if tag_text:
        tail += f" {tag_text}"
    room = MAX_TEXT - len(tail)
    headline = story.headline if len(story.headline) <= room else story.headline[: room - 1] + "…"
    text = headline + tail
    facets: list[dict[str, Any]] = []
    raw = text.encode()
    start = len(raw) - len(tag_text.encode())  # the tags close the text
    for t in tags:
        token = f"#{t}".encode()
        at = raw.index(token, start)
        facets.append(
            {
                "index": {"byteStart": at, "byteEnd": at + len(token)},
                "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": t}],
            }
        )
        start = at + len(token)
    return text, facets


def description(story: SiteStory) -> str:
    """The link card's text: the summary's first sentences that fit."""
    summary = (story.summary or story.why_it_matters or "").strip()
    out = ""
    for sentence in SENTENCE_END.split(summary):
        nxt = f"{out} {sentence}".strip()
        if len(nxt) > MAX_DESCRIPTION:
            break
        out = nxt
    if not out and summary:
        out = summary[: MAX_DESCRIPTION - 1] + "…"
    return out


class BlueskyError(Exception):
    def __init__(self, step: str, status: int) -> None:
        super().__init__(f"{step}: HTTP {status}")
        self.step = step
        self.status = status


class Bluesky:
    """The few XRPC calls a post needs: log in, upload the card image, create the post."""

    def __init__(self, client: httpx.Client, service: str) -> None:
        self.client = client
        self.service = service.rstrip("/")
        self.did = ""
        self.token = ""

    def _call(self, step: str, method: str, **kwargs: Any) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        headers |= kwargs.pop("headers", {})
        r = self.client.post(f"{self.service}/xrpc/{method}", headers=headers, **kwargs)
        if r.status_code >= 300:
            raise BlueskyError(step, r.status_code)
        return r.json()

    def login(self, handle: str, password: str) -> None:
        body = {"identifier": handle, "password": password}
        session = self._call("login", "com.atproto.server.createSession", json=body)
        self.did, self.token = session["did"], session["accessJwt"]

    def recent_links(self, limit: int = 30) -> dict[str, str]:
        """The link-card addresses of the account's newest posts, each to its post's at-uri.

        Catches a story posted by a run whose state never got saved, so it isn't posted twice.
        """
        r = self.client.get(
            f"{self.service}/xrpc/com.atproto.repo.listRecords",
            params={"repo": self.did, "collection": "app.bsky.feed.post", "limit": limit},
            headers={"Authorization": f"Bearer {self.token}"},
        )
        if r.status_code >= 300:
            raise BlueskyError("recent", r.status_code)
        links: dict[str, str] = {}
        for rec in r.json().get("records", []):
            card = (rec.get("value", {}).get("embed") or {}).get("external") or {}
            if isinstance(card.get("uri"), str):
                links[card["uri"]] = str(rec["uri"])
        return links

    def upload_png(self, data: bytes) -> dict[str, Any]:
        blob = self._call(
            "upload",
            "com.atproto.repo.uploadBlob",
            content=data,
            headers={"Content-Type": "image/png"},
        )
        return blob["blob"]

    def create_post(self, record: dict[str, Any]) -> str:
        body = {"repo": self.did, "collection": "app.bsky.feed.post", "record": record}
        return str(self._call("post", "com.atproto.repo.createRecord", json=body)["uri"])


def _thumb(client: httpx.Client, url: str) -> bytes | None:
    """The story's share image from the live site; None when it can't be had."""
    try:
        r = client.get(url)
    except httpx.HTTPError:
        return None
    ok = r.status_code == 200 and r.headers.get("content-type", "").startswith("image/png")
    return r.content if ok and len(r.content) <= MAX_THUMB_BYTES else None


def post_due(
    engine: Engine,
    settings: Settings,
    config: Config,
    stories: Sequence[SiteStory],
    now: dt.datetime,
    *,
    live_since: dt.datetime | None,
    client: httpx.Client | None = None,
) -> int:
    """Post the next due story, if any. Returns how many were posted (0 or 1)."""
    if not enabled(settings) or settings.bluesky_app_password is None or not settings.site_url:
        return 0
    rules = config.settings.bluesky
    posted = load_posted(engine)
    story = pick(stories, posted, rules, now=now, live_since=live_since, zone=settings.zone)
    if story is None:
        return 0
    base = settings.site_url.rstrip("/")
    text, facets = post_text(story, hashtags(story, rules))
    card: dict[str, Any] = {
        "uri": base + story.url,
        "title": story.headline,
        "description": description(story),
    }
    own = client is None
    http = client or httpx.Client(timeout=30, follow_redirects=True)
    try:
        bsky = Bluesky(http, settings.bluesky_service)
        bsky.login(settings.bluesky_handle or "", settings.bluesky_app_password.get_secret_value())
        if (earlier := bsky.recent_links().get(card["uri"])) is not None:
            save_posted(engine, [*posted, Posted(story.id, now, earlier)])
            log.info("bluesky_already_posted", extra={"story": story.id})
            return 0
        if (png := _thumb(http, base + story.share_path)) is not None:
            card["thumb"] = bsky.upload_png(png)
        uri = bsky.create_post(
            {
                "$type": "app.bsky.feed.post",
                "text": text,
                "facets": facets,
                "langs": [settings.edition_lang],
                "createdAt": iso(now),
                "embed": {"$type": "app.bsky.embed.external", "external": card},
            }
        )
    except BlueskyError as e:
        log.warning("bluesky_failed", extra={"stage": e.step, "status": e.status})
        return 0
    except (httpx.HTTPError, KeyError, ValueError) as e:
        log.warning("bluesky_failed", extra={"error_type": type(e).__name__})
        return 0
    finally:
        if own:
            http.close()
    save_posted(engine, [*posted, Posted(story.id, now, uri)])
    log.info("bluesky_posted", extra={"story": story.id})
    return 1
