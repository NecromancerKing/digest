from __future__ import annotations

import datetime as dt
from pathlib import Path

import httpx
import pytest

from digest import db
from digest.config import Config, FetchSettings
from digest.ingest.fetcher import Fetcher, FetchError
from digest.ingest.guard import RequestBlocked, RequestGuard
from digest.ingest.hostpolicy import HostPolicyCache
from digest.ingest.review import MemoryPolicyCache
from tests.fake_web import FakeWeb

T0 = dt.datetime(2026, 10, 23, 9, 0, tzinfo=dt.UTC)
UA = "DigestBot/1.0 (+https://digest.example/bot)"


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


def make_fetcher(
    web: FakeWeb,
    config: Config,
    *,
    policies: MemoryPolicyCache | HostPolicyCache | None = None,
    clock: Clock | None = None,
    now: dt.datetime = T0,
    **fetch: object,
) -> Fetcher:
    clock = clock or Clock()
    return Fetcher(
        RequestGuard.for_sources(config, config.enabled_sources),
        policies or MemoryPolicyCache(),
        FetchSettings.model_validate(fetch),
        user_agent=UA,
        robots_token="DigestBot",
        client=web.client(),
        now=lambda: now,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )


@pytest.fixture
def web() -> FakeWeb:
    w = FakeWeb()
    w.route("https://news.example/robots.txt", httpx.Response(404))
    w.route("https://news.example/feed.xml", httpx.Response(200, content=b"<rss/>"))
    return w


# --------------------------------------------------------------------------- guard (L1, L2)


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("https://www.dn.se/nyheter/x", "blocklist"),
        ("https://dn.se/", "blocklist"),
        ("https://unknown.example/feed", "unregistered"),
        ("https://dormant.example/rss", "unregistered"),  # registered but disabled
        ("ftp://news.example/feed", "scheme"),
        ("file:///etc/passwd", "scheme"),
    ],
)
def test_guard_refuses_before_any_request(
    web: FakeWeb, ingest_config: Config, url: str, reason: str
) -> None:
    f = make_fetcher(web, ingest_config)
    with pytest.raises(RequestBlocked) as e:
        f.get(url)
    assert e.value.reason == reason
    assert web.requests == []  # not even robots.txt
    assert f.stats.blocked[reason] == 1


def test_blocklist_wins_over_registry(ingest_config: Config) -> None:
    guard = RequestGuard({"news.example", "www.dn.se"}, ingest_config.blocklist)
    with pytest.raises(RequestBlocked, match="blocklist"):
        guard.check("https://www.dn.se/")


def test_redirect_to_blocked_host_is_not_followed(web: FakeWeb, ingest_config: Config) -> None:
    web.route(
        "https://news.example/feed.xml",
        httpx.Response(301, headers={"location": "https://www.dn.se/rss"}),
    )
    f = make_fetcher(web, ingest_config)
    with pytest.raises(RequestBlocked, match="blocklist"):
        f.get("https://news.example/feed.xml")
    assert "www.dn.se" not in web.hosts()


def test_redirects_within_registry_are_followed(web: FakeWeb, ingest_config: Config) -> None:
    web.route(
        "https://news.example/old",
        httpx.Response(302, headers={"location": "/feed.xml"}),
    )
    resp = make_fetcher(web, ingest_config).get("https://news.example/old")
    assert resp.status == 200
    assert resp.url == "https://news.example/feed.xml"


def test_redirect_loop_gives_up(web: FakeWeb, ingest_config: Config) -> None:
    web.route("https://news.example/a", httpx.Response(302, headers={"location": "/a"}))
    with pytest.raises(FetchError, match="too_many_redirects"):
        make_fetcher(web, ingest_config, max_redirects=2).get("https://news.example/a")


# --------------------------------------------------------------------------- robots (L3)


def test_user_agent_and_conditional_headers_are_sent(web: FakeWeb, ingest_config: Config) -> None:
    make_fetcher(web, ingest_config).get(
        "https://news.example/feed.xml", {"If-None-Match": '"abc"'}
    )
    feed_req = web.requests[-1]
    assert feed_req.headers["User-Agent"] == UA
    assert feed_req.headers["If-None-Match"] == '"abc"'
    assert web.requests[0].url.path == "/robots.txt"  # robots first


def test_robots_disallow_blocks_the_request(web: FakeWeb, ingest_config: Config) -> None:
    web.route(
        "https://news.example/robots.txt",
        httpx.Response(200, text="User-agent: DigestBot\nDisallow: /feed\n"),
    )
    f = make_fetcher(web, ingest_config)
    with pytest.raises(RequestBlocked, match="robots"):
        f.get("https://news.example/feed.xml")
    assert web.urls() == ["https://news.example/robots.txt"]
    assert f.robots_verdict("https://news.example/feed.xml") == "disallowed"
    assert f.robots_allows("https://news.example/other")


def test_robots_server_error_keeps_out_and_is_not_cached(
    web: FakeWeb, ingest_config: Config
) -> None:
    web.route("https://news.example/robots.txt", httpx.Response(503))
    policies = MemoryPolicyCache()
    f = make_fetcher(web, ingest_config, policies=policies)
    assert f.robots_verdict("https://news.example/feed.xml") == "unavailable"
    assert policies.get("news.example", "robots") is None


def test_robots_cached_in_state_for_24_hours(
    web: FakeWeb, ingest_config: Config, tmp_path: Path
) -> None:
    path = tmp_path / "s.sqlite"
    db.migrate(path)
    policies = HostPolicyCache(db.make_engine(path))
    web.route("https://news.example/robots.txt", httpx.Response(200, text="User-agent: *\n"))
    make_fetcher(web, ingest_config, policies=policies).get("https://news.example/feed.xml")
    make_fetcher(web, ingest_config, policies=policies, now=T0 + dt.timedelta(hours=23)).get(
        "https://news.example/feed.xml"
    )
    assert web.urls().count("https://news.example/robots.txt") == 1
    make_fetcher(web, ingest_config, policies=policies, now=T0 + dt.timedelta(hours=25)).get(
        "https://news.example/feed.xml"
    )
    assert web.urls().count("https://news.example/robots.txt") == 2


# --------------------------------------------------------------------------- rate limit and size


def test_requests_to_one_host_are_spaced(web: FakeWeb, ingest_config: Config) -> None:
    clock = Clock()
    f = make_fetcher(web, ingest_config, clock=clock)
    f.get("https://news.example/feed.xml")  # robots.txt + feed: one wait between them
    f.get("https://news.example/feed.xml")
    assert clock.slept == [5.0, 5.0]
    assert f.wait_seconds("news.example") == 5.0
    assert f.wait_seconds("radio.example") == 0.0


def test_oversized_response_is_refused(web: FakeWeb, ingest_config: Config) -> None:
    web.route("https://news.example/feed.xml", httpx.Response(200, content=b"x" * 2000))
    with pytest.raises(FetchError, match="too_large"):
        make_fetcher(web, ingest_config, max_response_bytes=1000).get(
            "https://news.example/feed.xml"
        )


def test_network_errors_become_fetch_errors(web: FakeWeb, ingest_config: Config) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    web.route("https://news.example/feed.xml", boom)
    with pytest.raises(FetchError, match="ConnectTimeout"):
        make_fetcher(web, ingest_config).get("https://news.example/feed.xml")
