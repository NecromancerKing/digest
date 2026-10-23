from __future__ import annotations

import datetime as dt
from pathlib import Path

import httpx
import pytest
from sqlalchemy import Engine, func, select

from digest import db
from digest.config import Config, FetchSettings
from digest.db.schema import feeds, items
from digest.housekeeping import purge_full_text
from digest.ingest.hostpolicy import HostPolicyCache
from digest.ingest.stage import IngestStats, next_poll_at, run_ingest
from digest.timeutil import iso
from tests.fake_web import FakeWeb, item, rss
from tests.test_fetcher import Clock, make_fetcher

T0 = dt.datetime(2026, 10, 23, 9, 0, tzinfo=dt.UTC)
DATE = "Fri, 23 Oct 2026 08:00:00 GMT"
PAGE = (
    b"<html><body><article><p>"
    + b"Body text of the article page. " * 30
    + b"</p></article></body></html>"
)


class Budget:
    def __init__(self, seconds: float = 600) -> None:
        self.seconds = seconds

    def remaining(self) -> float:
        return self.seconds


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    path = tmp_path / "state.sqlite"
    db.migrate(path)
    return db.make_engine(path)


@pytest.fixture
def web() -> FakeWeb:
    w = FakeWeb()
    for host in ("news.example", "radio.example"):
        w.route(f"https://{host}/robots.txt", httpx.Response(404))
    w.route("https://news.example/.well-known/tdmrep.json", httpx.Response(404))
    w.route(
        "https://news.example/feed.xml",
        httpx.Response(
            200,
            content=rss(
                item(
                    "One",
                    "https://news.example/1?utm_source=rss",
                    guid="n1",
                    desc="Desc one",
                    date=DATE,
                ),
                item("Two", "https://news.example/2", guid="n2", desc="Desc two", date=DATE),
                item("Same as one", "https://news.example/1#frag", guid="n1-dup", date=DATE),
                item("Old", "https://news.example/old", date="Mon, 01 Jan 2024 00:00:00 GMT"),
                item("Paywalled", "https://www.dn.se/x", date=DATE),
            ),
            headers={"etag": '"v1"'},
        ),
    )
    w.route(
        "https://radio.example/rss",
        httpx.Response(
            200,
            content=rss(
                item("Radio title", "https://radio.example/r1", desc="Radio desc", date=DATE)
            ),
        ),
    )
    for n in (1, 2):
        w.route(
            f"https://news.example/{n}",
            httpx.Response(200, content=PAGE, headers={"content-type": "text/html"}),
        )
    return w


def ingest(
    engine: Engine, config: Config, web: FakeWeb, now: dt.datetime = T0, budget: float = 600
) -> IngestStats:
    fetcher = make_fetcher(
        web, config, policies=HostPolicyCache(engine), now=now, **config.settings.fetch.model_dump()
    )
    return run_ingest(engine, config, fetcher, Budget(budget), retention_hours=72, now=lambda: now)


def rows(engine: Engine) -> list[dict[str, object]]:
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(select(items).order_by(items.c.id))]


def test_first_run_inserts_canonical_new_items(
    engine: Engine, ingest_config: Config, web: FakeWeb
) -> None:
    stats = ingest(engine, ingest_config, web)
    got = rows(engine)
    assert [(r["source_id"], r["url"]) for r in got] == [
        ("news", "https://news.example/1"),
        ("news", "https://news.example/2"),
        ("radio", "https://radio.example/r1"),
    ]
    assert all(r["status"] == "new" for r in got)
    assert [r["usage_policy"] for r in got] == ["facts_only", "facts_only", "link_only"]
    assert stats.duplicates == 1 and stats.too_old == 1 and stats.blocked_links == 1
    assert "www.dn.se" not in web.hosts() and "dormant.example" not in web.hosts()
    assert web.unrouted() == []


def test_link_only_items_keep_only_the_title(
    engine: Engine, ingest_config: Config, web: FakeWeb
) -> None:
    ingest(engine, ingest_config, web)
    radio = rows(engine)[-1]
    assert radio["title"] == "Radio title"
    assert radio["feed_summary"] is None
    assert radio["full_text"] is None and radio["embedding"] is None and radio["facts"] is None


def test_article_text_stored_with_purge_time(
    engine: Engine, ingest_config: Config, web: FakeWeb
) -> None:
    stats = ingest(engine, ingest_config, web)
    assert stats.articles["stored"] == 2
    one = rows(engine)[0]
    assert isinstance(one["full_text"], str) and len(one["full_text"]) <= 3000
    assert one["full_text_purge_at"] == iso(T0 + dt.timedelta(hours=72))


def test_rerun_is_idempotent_and_conditional(
    engine: Engine, ingest_config: Config, web: FakeWeb
) -> None:
    ingest(engine, ingest_config, web)
    later = T0 + dt.timedelta(minutes=30)
    stats = ingest(engine, ingest_config, web, now=later)
    assert stats.feeds_polled == 2 and stats.items_new == 0
    assert len(rows(engine)) == 3
    feed_req = [r for r in web.requests if str(r.url) == "https://news.example/feed.xml"][-1]
    assert feed_req.headers["If-None-Match"] == '"v1"'


def test_guid_dedup_within_a_source(engine: Engine, ingest_config: Config, web: FakeWeb) -> None:
    ingest(engine, ingest_config, web)
    web.route(
        "https://news.example/feed.xml",
        httpx.Response(
            200, content=rss(item("Moved", "https://news.example/moved", guid="n2", date=DATE))
        ),
    )
    stats = ingest(engine, ingest_config, web, now=T0 + dt.timedelta(minutes=30))
    assert stats.items_new == 0 and stats.duplicates == 2  # the moved item and radio's


def test_not_modified_counts_as_success(
    engine: Engine, ingest_config: Config, web: FakeWeb
) -> None:
    ingest(engine, ingest_config, web)
    web.route("https://news.example/feed.xml", httpx.Response(304))
    stats = ingest(engine, ingest_config, web, now=T0 + dt.timedelta(minutes=30))
    assert stats.feeds_not_modified == 1 and stats.feeds_failed == 0


def test_feeds_are_not_polled_before_they_are_due(
    engine: Engine, ingest_config: Config, web: FakeWeb
) -> None:
    ingest(engine, ingest_config, web)
    n = len(web.requests)
    stats = ingest(engine, ingest_config, web, now=T0 + dt.timedelta(minutes=9))
    assert stats.feeds_due == 0 and len(web.requests) == n


def test_poll_interval_rules() -> None:
    fs = FetchSettings()
    last = iso(T0)
    assert next_poll_at(None, 0, False, 15, fs) is None
    assert next_poll_at(last, 0, False, 30, fs) == T0 + dt.timedelta(minutes=25)  # grace
    assert next_poll_at(last, 0, False, 5, fs) == T0 + dt.timedelta(minutes=15)  # L3 floor
    assert next_poll_at(last, 3, False, 15, fs) == T0 + dt.timedelta(minutes=115)  # backoff
    assert next_poll_at(last, 20, False, 15, fs) == T0 + dt.timedelta(hours=24) - dt.timedelta(
        minutes=5
    )
    assert next_poll_at(last, 10, True, 15, fs) == T0 + dt.timedelta(hours=24)


def test_feed_is_disabled_after_repeated_errors_and_reenabled_on_success(
    engine: Engine, ingest_config: Config, web: FakeWeb
) -> None:
    web.route("https://news.example/feed.xml", httpx.Response(503))
    now = T0
    disabled = 0
    for _ in range(10):
        stats = ingest(engine, ingest_config, web, now=now)
        disabled += stats.feeds_disabled
        now += dt.timedelta(days=2)  # past any backoff
    with engine.connect() as conn:
        row = conn.execute(
            select(feeds).where(feeds.c.url == "https://news.example/feed.xml")
        ).one()
    assert disabled == 1
    assert row.consecutive_errors == 10 and row.last_status == 503
    assert row.disabled_reason == "10 consecutive errors, last: http_503"

    web.route("https://news.example/feed.xml", httpx.Response(200, content=rss()))
    ingest(engine, ingest_config, web, now=now)
    with engine.connect() as conn:
        row = conn.execute(
            select(feeds).where(feeds.c.url == "https://news.example/feed.xml")
        ).one()
    assert row.disabled_reason is None and row.consecutive_errors == 0


def test_robots_disallowed_feed_is_skipped_not_failed(
    engine: Engine, ingest_config: Config, web: FakeWeb
) -> None:
    web.route(
        "https://radio.example/robots.txt", httpx.Response(200, text="User-agent: *\nDisallow: /\n")
    )
    stats = ingest(engine, ingest_config, web)
    assert stats.feeds_robots_disallowed == 1 and stats.feeds_failed == 0
    assert "https://radio.example/rss" not in web.urls()


def test_no_work_starts_without_budget(engine: Engine, ingest_config: Config, web: FakeWeb) -> None:
    stats = ingest(engine, ingest_config, web, budget=5)
    assert stats.feeds_skipped_for_budget == 2
    assert web.requests == []


def test_hosts_are_interleaved_to_avoid_waiting(
    ingest_config: Config, web: FakeWeb, engine: Engine
) -> None:
    clock = Clock()
    fetcher = make_fetcher(web, ingest_config, policies=HostPolicyCache(engine), clock=clock)
    run_ingest(engine, ingest_config, fetcher, Budget(), retention_hours=72, now=lambda: T0)
    # Two hosts, each: robots + feed (+ tdmrep + 2 articles on news): waits only within a host.
    assert sum(clock.slept) <= 5 * 5


# --------------------------------------------------------------------------- retention (L5)


def test_purge_removes_expired_text_and_nothing_older_survives(
    engine: Engine, ingest_config: Config, web: FakeWeb
) -> None:
    ingest(engine, ingest_config, web)
    assert purge_full_text(engine, T0 + dt.timedelta(hours=71), 72) == 0
    with engine.begin() as conn:  # a row with a wrong purge time is still caught by age
        conn.execute(
            items.update()
            .where(items.c.id == 2)
            .values(full_text_purge_at=iso(T0 + dt.timedelta(days=30)))
        )
    assert purge_full_text(engine, T0 + dt.timedelta(hours=72), 72) == 2
    with engine.connect() as conn:
        left = conn.execute(select(func.count()).where(items.c.full_text.is_not(None))).scalar_one()
    assert left == 0
