"""Stage 1: ingest (spec section 8).

Polls due feeds of enabled sources and inserts new items with status ``new``. Article pages
are fetched later, only for items stage 5 is about to read (``fetch_articles``, D28).
Idempotent: re-running on the same feeds inserts
nothing twice (de-duplication on ``url_hash``, then on GUID per source).

Requests are scheduled across hosts so the per-host interval rarely makes us wait, and no
new request starts once the run's time budget is spent.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, Engine, and_, or_, select

from digest.config import Config, FetchSettings, Source
from digest.db.schema import feeds, items
from digest.ingest.articles import fetch_article
from digest.ingest.canonical import canonicalize, url_hash
from digest.ingest.feeds import Entry, FeedParseError, parse_feed
from digest.ingest.fetcher import Fetcher, FetchError
from digest.ingest.guard import RequestBlocked
from digest.timeutil import iso, parse_iso

log = logging.getLogger(__name__)

INGESTED_KINDS = frozenset({"rss", "atom", "json_api"})
DATA_KIND = "data_api"  # template stories: parsed by the source's template


class Budget(Protocol):
    def remaining(self) -> float: ...


@dataclass
class IngestStats:
    feeds_due: int = 0
    feeds_polled: int = 0
    feeds_not_modified: int = 0
    feeds_failed: int = 0
    feeds_disabled: int = 0
    feeds_robots_disallowed: int = 0
    feeds_skipped_for_budget: int = 0
    entries_seen: int = 0
    items_new: int = 0
    duplicates: int = 0
    too_old: int = 0
    blocked_links: int = 0

    def as_dict(self) -> dict[str, int]:
        return {k: v for k, v in vars(self).items() if isinstance(v, int)}


@dataclass(frozen=True)
class DataContext:
    """What template parsers need to write entries: the edition language and local time."""

    lang: str = "en"
    zone: ZoneInfo = field(default_factory=lambda: ZoneInfo("Europe/Stockholm"))
    enabled: bool = True  # TEMPLATE_STORIES


@dataclass(frozen=True)
class FeedJob:
    source: Source
    url: str
    kind: str
    etag: str | None
    last_modified: str | None
    consecutive_errors: int
    disabled: bool

    @property
    def host(self) -> str:
        return (urlsplit(self.url).hostname or "").lower()


@dataclass(frozen=True)
class ArticleJob:
    item_id: int
    url: str

    @property
    def host(self) -> str:
        return (urlsplit(self.url).hostname or "").lower()


# --------------------------------------------------------------------------- feed rows


def sync_feeds(conn: Connection, sources: Iterable[Source]) -> None:
    """Make sure every registry feed has a ``feeds`` row with the current kind and source."""
    existing = {
        r.url: r for r in conn.execute(select(feeds.c.url, feeds.c.source_id, feeds.c.kind))
    }
    for s in sources:
        for f in s.feeds:
            url = str(f.url)
            row = existing.get(url)
            if row is None:
                conn.execute(feeds.insert().values(url=url, source_id=s.id, kind=f.kind))
            elif (row.source_id, row.kind) != (s.id, f.kind):
                conn.execute(
                    feeds.update().where(feeds.c.url == url).values(source_id=s.id, kind=f.kind)
                )


def next_poll_at(
    last_polled_at: str | None,
    consecutive_errors: int,
    disabled: bool,
    poll_minutes: int,
    fs: FetchSettings,
) -> dt.datetime | None:
    """When a feed is next due; None means now. Never sooner than ``min_poll_minutes``."""
    if last_polled_at is None:
        return None
    last = parse_iso(last_polled_at)
    if disabled:
        return last + dt.timedelta(hours=fs.disabled_feed_probe_hours)
    minutes = float(max(poll_minutes, fs.min_poll_minutes))
    if consecutive_errors:
        minutes = min(minutes * 2**consecutive_errors, fs.backoff_max_hours * 60.0)
    minutes = max(float(fs.min_poll_minutes), minutes - fs.poll_grace_minutes)
    return last + dt.timedelta(minutes=minutes)


def due_feeds(
    conn: Connection,
    sources: Iterable[Source],
    fs: FetchSettings,
    now: dt.datetime,
    templates: bool = True,
) -> tuple[list[FeedJob], int]:
    """Due feeds of the given sources, and how many data feeds were left out.

    A ``data_api`` feed is polled only for a template source while template stories are on.
    """
    rows = {r.url: r for r in conn.execute(select(feeds))}
    jobs: list[FeedJob] = []
    data_feeds = 0
    for s in sources:
        for f in s.feeds:
            if f.kind == DATA_KIND and not (templates and s.template):
                data_feeds += 1
                continue
            if f.kind not in INGESTED_KINDS and f.kind != DATA_KIND:
                continue
            r = rows[str(f.url)]
            at = next_poll_at(
                r.last_polled_at, r.consecutive_errors, bool(r.disabled_reason), s.poll_minutes, fs
            )
            if at is None or at <= now:
                jobs.append(
                    FeedJob(
                        source=s,
                        url=str(f.url),
                        kind=f.kind,
                        etag=r.etag,
                        last_modified=r.last_modified,
                        consecutive_errors=r.consecutive_errors,
                        disabled=bool(r.disabled_reason),
                    )
                )
    return jobs, data_feeds


# --------------------------------------------------------------------------- scheduling


def _by_host[J: (FeedJob, ArticleJob)](jobs: Iterable[J]) -> dict[str, list[J]]:
    queues: dict[str, list[J]] = {}
    for j in jobs:
        queues.setdefault(j.host, []).append(j)
    return queues


def schedule[J: (FeedJob, ArticleJob)](
    jobs: Iterable[J], fetcher: Fetcher, budget: Budget, run: Callable[[J], None]
) -> int:
    """Run jobs, always picking the host that is ready soonest. Returns jobs left undone."""
    queues = _by_host(jobs)
    while queues:
        host = min(queues, key=fetcher.wait_seconds)
        # Each job may need several requests (robots.txt, redirects); keep a margin.
        if fetcher.wait_seconds(host) + fetcher.settings.timeout_seconds * 2 > budget.remaining():
            break
        job = queues[host].pop(0)
        if not queues[host]:
            del queues[host]
        run(job)
    return sum(len(q) for q in queues.values())


# --------------------------------------------------------------------------- one feed


def _exists(conn: Connection, source_id: str, h: bytes, guid: str | None) -> bool:
    cond = items.c.url_hash == h
    if guid is not None:
        cond = or_(cond, and_(items.c.source_id == source_id, items.c.guid == guid))
    return conn.execute(select(items.c.id).where(cond).limit(1)).first() is not None


def store_entries(
    conn: Connection,
    job: FeedJob,
    entries: list[Entry],
    *,
    config: Config,
    now: dt.datetime,
    stats: IngestStats,
) -> None:
    """Insert new entries as items."""
    fs = config.settings.fetch
    oldest = now - dt.timedelta(hours=fs.max_entry_age_hours)
    link_only = job.source.usage_policy == "link_only"
    for e in entries:
        stats.entries_seen += 1
        url = canonicalize(e.link, fs.drop_query_params)
        host = (urlsplit(url).hostname or "").lower()
        if urlsplit(url).scheme not in ("http", "https") or config.blocklist.is_blocked(host):
            stats.blocked_links += 1
            continue
        published = e.published_at
        if published is not None and published < oldest:
            stats.too_old += 1
            continue
        if published is not None and published > now:
            published = now
        h = url_hash(f"{url}#{e.dedupe_key}" if e.dedupe_key else url)
        guid = e.guid[:500] if e.guid else None
        if _exists(conn, job.source.id, h, guid):
            stats.duplicates += 1
            if e.data:  # still in the data feed: the site keeps showing it (warnings box)
                conn.execute(
                    items.update()
                    .where(items.c.source_id == job.source.id, items.c.url_hash == h)
                    .values(data=json.dumps({**e.data, "seen_at": iso(now)}, ensure_ascii=False))
                )
            continue
        conn.execute(
            items.insert().values(
                source_id=job.source.id,
                feed_url=job.url,
                url=url,
                url_hash=h,
                guid=guid,
                title=e.title,
                # link_only text may not be stored beyond what matching needs (the title).
                feed_summary=None if link_only else e.summary,
                published_at=iso(published) if published else None,
                fetched_at=iso(now),
                lang=e.lang or job.source.lang,
                usage_policy=job.source.usage_policy,
                status="new",
                data=(
                    json.dumps({**e.data, "seen_at": iso(now)}, ensure_ascii=False)
                    if e.data
                    else None
                ),
            )
        )
        stats.items_new += 1


def _record_error(
    conn: Connection,
    job: FeedJob,
    reason: str,
    status: int | None,
    now: dt.datetime,
    fs: FetchSettings,
    stats: IngestStats,
) -> None:
    errors = job.consecutive_errors + 1
    values: dict[str, object] = {
        "last_polled_at": iso(now),
        "last_status": status,
        "consecutive_errors": errors,
    }
    stats.feeds_failed += 1
    if errors >= fs.disable_feed_after_errors and not job.disabled:
        values["disabled_reason"] = f"{errors} consecutive errors, last: {reason}"
        stats.feeds_disabled += 1
        log.warning(
            "feed_disabled",
            extra={"source_id": job.source.id, "feed_url": job.url, "reason": reason},
        )
    log.info(
        "feed_error",
        extra={
            "source_id": job.source.id,
            "feed_url": job.url,
            "reason": reason,
            "errors": errors,
        },
    )
    conn.execute(feeds.update().where(feeds.c.url == job.url).values(**values))


def poll_feed(
    engine: Engine,
    fetcher: Fetcher,
    job: FeedJob,
    *,
    config: Config,
    now: Callable[[], dt.datetime],
    stats: IngestStats,
    data: DataContext | None = None,
) -> None:
    fs = config.settings.fetch
    headers: dict[str, str] = {}
    if job.etag:
        headers["If-None-Match"] = job.etag
    if job.last_modified:
        headers["If-Modified-Since"] = job.last_modified
    stats.feeds_polled += 1
    # Fetch first, outside any write transaction: the fetcher may write host_policy.
    try:
        resp = fetcher.get(job.url, headers)
    except RequestBlocked as e:
        with engine.begin() as conn:
            if e.reason == "robots":
                stats.feeds_robots_disallowed += 1
                log.warning(
                    "feed_robots_disallowed",
                    extra={"source_id": job.source.id, "feed_url": job.url},
                )
                conn.execute(
                    feeds.update()
                    .where(feeds.c.url == job.url)
                    .values(last_polled_at=iso(now()), last_status=None)
                )
            else:
                _record_error(conn, job, f"blocked_{e.reason}", None, now(), fs, stats)
        return
    except FetchError as e:
        with engine.begin() as conn:
            _record_error(conn, job, e.reason, None, now(), fs, stats)
        return

    t = now()
    if resp.status == 304:
        stats.feeds_not_modified += 1
        entries: list[Entry] = []
    elif resp.ok:
        try:
            if job.kind == DATA_KIND:
                entries = _parse_data(job, resp.content, config, data or DataContext())
            else:
                entries = parse_feed(
                    resp.content,
                    job.kind,
                    resp.url,
                    title_max=fs.title_max_chars,
                    summary_max=fs.feed_summary_max_chars,
                )
        except FeedParseError:
            with engine.begin() as conn:
                _record_error(conn, job, "parse_error", resp.status, t, fs, stats)
            return
    else:
        with engine.begin() as conn:
            _record_error(conn, job, f"http_{resp.status}", resp.status, t, fs, stats)
        return

    with engine.begin() as conn:
        before = stats.items_new
        store_entries(conn, job, entries, config=config, now=t, stats=stats)
        values: dict[str, object] = {
            "last_polled_at": iso(t),
            "last_status": resp.status,
            "consecutive_errors": 0,
            "disabled_reason": None,
        }
        if resp.status != 304:
            values["etag"] = resp.headers.get("etag")
            values["last_modified"] = resp.headers.get("last-modified")
        conn.execute(feeds.update().where(feeds.c.url == job.url).values(**values))
    if job.disabled:
        log.info("feed_reenabled", extra={"source_id": job.source.id, "feed_url": job.url})
    log.info(
        "feed_polled",
        extra={
            "source_id": job.source.id,
            "feed_url": job.url,
            "status": str(resp.status),
            "entries": len(entries),
            "new": stats.items_new - before,
        },
    )


def _parse_data(job: FeedJob, content: bytes, config: Config, data: DataContext) -> list[Entry]:
    from digest.datastories import parse_data_feed

    if job.source.template is None:
        raise FeedParseError("data feed without a template")
    return parse_data_feed(
        job.source.template,
        content,
        lang=data.lang,
        link=str(job.source.homepage),
        zone=data.zone,
        settings=config.settings.templates,
    )


# --------------------------------------------------------------------------- the stage


def store_article(engine: Engine, item_id: int, text: str, purge_at: dt.datetime) -> None:
    with engine.begin() as conn:
        conn.execute(
            items.update()
            .where(items.c.id == item_id, items.c.usage_policy != "link_only")
            .values(full_text=text, full_text_purge_at=iso(purge_at))
        )


def run_ingest(
    engine: Engine,
    config: Config,
    fetcher: Fetcher,
    budget: Budget,
    *,
    retention_hours: int,
    now: Callable[[], dt.datetime],
    data: DataContext | None = None,
) -> IngestStats:
    stats = IngestStats()
    sources = config.enabled_sources
    data = data or DataContext()
    with engine.begin() as conn:
        sync_feeds(conn, sources)
        jobs, _ = due_feeds(conn, sources, config.settings.fetch, now(), data.enabled)
    stats.feeds_due = len(jobs)

    def run_feed(job: FeedJob) -> None:
        poll_feed(engine, fetcher, job, config=config, now=now, stats=stats, data=data)

    stats.feeds_skipped_for_budget = schedule(jobs, fetcher, budget, run_feed)
    return stats


def fetch_articles(
    engine: Engine,
    config: Config,
    fetcher: Fetcher,
    item_ids: Sequence[int],
    budget: Budget,
    *,
    retention_hours: int,
    now: Callable[[], dt.datetime],
    limit: int,
) -> Counter[str]:
    """Article text for those of ``item_ids`` that stage 5 hasn't read yet, whose source
    allows it and that have none.

    Stage 5 calls this just before it reads the items (D28), so pages are fetched only for
    stories being written. At most ``limit`` pages; returns outcomes by name.
    """
    allowed = {s.id for s in config.sources if s.fetch_article_pages}
    with engine.connect() as conn:
        rows = conn.execute(
            select(items.c.id, items.c.url, items.c.source_id).where(
                items.c.id.in_(list(item_ids)),
                items.c.usage_policy != "link_only",
                items.c.status == "clustered",
                items.c.full_text.is_(None),
            )
        ).all()
    jobs = [ArticleJob(r.id, r.url) for r in rows if r.source_id in allowed][:limit]
    max_chars = config.settings.fetch.article_text_max_chars
    outcomes = Counter[str]()

    def run_article(job: ArticleJob) -> None:
        result = fetch_article(fetcher, job.url, max_chars)
        outcomes[result.outcome] += 1
        if result.text is not None:
            store_article(
                engine, job.item_id, result.text, now() + dt.timedelta(hours=retention_hours)
            )

    left = schedule(jobs, fetcher, budget, run_article)
    if left:
        outcomes["skipped_for_budget"] += left
    return outcomes
