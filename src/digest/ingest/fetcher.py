"""The only way the pipeline makes HTTP requests to sources (rules L1-L4).

Every request, including robots.txt, ``tdmrep.json`` and each redirect hop, goes through
``Fetcher._send``, which first asks the ``RequestGuard`` (registry and blocklist) and then
waits for the per-host interval. Page requests also check robots.txt for our user agent.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol
from urllib.parse import urljoin, urlsplit

import httpx
from protego import Protego

from digest.config import FetchSettings
from digest.ingest import tdm
from digest.ingest.guard import RequestBlocked, RequestGuard
from digest.ingest.hostpolicy import Cached
from digest.timeutil import utcnow

log = logging.getLogger(__name__)

ROBOTS_MAX_BYTES = 500_000  # what Google reads; the rest is ignored
DISALLOW_ALL = "User-agent: *\nDisallow: /\n"
REDIRECTS = frozenset({301, 302, 303, 307, 308})

TdmVerdict = Literal["reserved", "not_reserved", "unknown"]


class PolicyCache(Protocol):
    def get(self, host: str, kind: str) -> Cached | None: ...
    def put(self, host: str, kind: str, text: str, at: dt.datetime) -> None: ...


class FetchError(Exception):
    """A request was sent but did not produce a usable response."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason  # error type or "http_<status>": safe to log


@dataclass(frozen=True)
class FetchResponse:
    url: str  # after redirects
    status: int
    headers: Mapping[str, str]
    content: bytes

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


@dataclass
class FetchStats:
    requests: Counter[str] = field(default_factory=lambda: Counter[str]())  # per host
    blocked: Counter[str] = field(default_factory=lambda: Counter[str]())  # per reason
    waited_seconds: float = 0.0

    def as_dict(self) -> dict[str, int | float]:
        return {
            "requests": sum(self.requests.values()),
            "hosts": len(self.requests),
            "blocked_blocklist": self.blocked["blocklist"],
            "blocked_unregistered": self.blocked["unregistered"],
            "blocked_scheme": self.blocked["scheme"],
            "blocked_robots": self.blocked["robots"],
            "rate_limit_wait_s": round(self.waited_seconds, 1),
        }


class Fetcher:
    def __init__(
        self,
        guard: RequestGuard,
        policies: PolicyCache,
        settings: FetchSettings,
        *,
        user_agent: str,
        robots_token: str,
        client: httpx.Client | None = None,
        now: Callable[[], dt.datetime] = utcnow,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.guard = guard
        self.policies = policies
        self.settings = settings
        self.user_agent = user_agent
        self.robots_token = robots_token
        self._client = client or httpx.Client(timeout=settings.timeout_seconds)
        self._now = now
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or time.sleep
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, Protego] = {}
        self._robots_unavailable: set[str] = set()
        self._tdmrep: dict[str, list[dict[str, object]] | None] = {}
        self.stats = FetchStats()

    def close(self) -> None:
        self._client.close()

    # -- rate limit

    def wait_seconds(self, host: str) -> float:
        """How long until a request to ``host`` is allowed (0 if now)."""
        last = self._last_request.get(host)
        if last is None:
            return 0.0
        interval = self.settings.min_seconds_between_requests_per_host
        return max(0.0, last + interval - self._monotonic())

    def _wait_for(self, host: str) -> None:
        delay = self.wait_seconds(host)
        if delay > 0:
            self.stats.waited_seconds += delay
            self._sleep(delay)

    # -- the single send path

    def _check(self, url: str) -> str:
        try:
            return self.guard.check(url)
        except RequestBlocked as e:
            self.stats.blocked[e.reason] += 1
            log.warning("request_blocked", extra={"reason": e.reason, "host": e.host})
            raise

    def _send(self, url: str, headers: Mapping[str, str] | None = None) -> FetchResponse:
        host = self._check(url)
        self._wait_for(host)
        self._last_request[host] = self._monotonic()
        self.stats.requests[host] += 1
        request = self._client.build_request(
            "GET", url, headers={"User-Agent": self.user_agent, **(headers or {})}
        )
        limit = self.settings.max_response_bytes
        try:
            resp = self._client.send(request, stream=True, follow_redirects=False)
            try:
                body = bytearray()
                if resp.status_code not in REDIRECTS:
                    for chunk in resp.iter_bytes():
                        body += chunk
                        if len(body) > limit:
                            raise FetchError("too_large")
            finally:
                resp.close()
        except httpx.HTTPError as e:
            raise FetchError(type(e).__name__) from e
        return FetchResponse(str(resp.url), resp.status_code, resp.headers, bytes(body))

    def _follow(
        self, url: str, headers: Mapping[str, str] | None, *, robots: bool
    ) -> FetchResponse:
        for _ in range(self.settings.max_redirects + 1):
            self._check(url)  # before robots.txt is requested from that host
            if robots and not self.robots_allows(url):
                self.stats.blocked["robots"] += 1
                raise RequestBlocked("robots", (urlsplit(url).hostname or "").lower())
            resp = self._send(url, headers)
            location = resp.headers.get("location")
            if resp.status not in REDIRECTS or not location:
                return resp
            url = urljoin(url, location)
        raise FetchError("too_many_redirects")

    def get(self, url: str, headers: Mapping[str, str] | None = None) -> FetchResponse:
        """GET a page or feed: guard, robots.txt and rate limit on every hop.

        Raises ``RequestBlocked`` (nothing was sent for that hop) or ``FetchError``.
        Non-2xx responses are returned, not raised; the caller decides.
        """
        return self._follow(url, headers, robots=True)

    # -- robots.txt

    def _fresh(self, cached: Cached | None, hours: float) -> bool:
        return cached is not None and self._now() - cached.fetched_at < dt.timedelta(hours=hours)

    def _robots_for(self, url: str) -> Protego:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if host in self._robots:
            return self._robots[host]
        cached = self.policies.get(host, "robots")
        if cached is not None and self._fresh(cached, self.settings.robots_cache_hours):
            text = cached.text
        else:
            text = self._download_robots(f"{parts.scheme}://{parts.netloc}/robots.txt", host)
        parser = Protego.parse(text)
        self._robots[host] = parser
        return parser

    def _download_robots(self, robots_url: str, host: str) -> str:
        """robots.txt text to apply. 4xx means no rules; 5xx, 429 or no answer means keep out."""
        try:
            resp = self._follow(robots_url, None, robots=False)
        except (RequestBlocked, FetchError) as e:
            reason = e.reason
            log.warning("robots_unavailable", extra={"host": host, "reason": reason})
            self._robots_unavailable.add(host)
            return DISALLOW_ALL  # not cached: try again next run
        if resp.ok:
            text = resp.content[:ROBOTS_MAX_BYTES].decode("utf-8", errors="replace")
        elif 400 <= resp.status < 500 and resp.status != 429:
            text = ""
        else:
            log.warning("robots_unavailable", extra={"host": host, "status": str(resp.status)})
            self._robots_unavailable.add(host)
            return DISALLOW_ALL
        self.policies.put(host, "robots", text, self._now())
        return text

    def robots_allows(self, url: str) -> bool:
        return self._robots_for(url).can_fetch(url, self.robots_token)

    def robots_verdict(self, url: str) -> Literal["allowed", "disallowed", "unavailable"]:
        """Like ``robots_allows``, but tells rules apart from an unreachable robots.txt."""
        if self.robots_allows(url):
            return "allowed"
        host = (urlsplit(url).hostname or "").lower()
        return "unavailable" if host in self._robots_unavailable else "disallowed"

    # -- site-wide TDM reservation

    def site_tdm(self, url: str) -> TdmVerdict:
        """Whether ``/.well-known/tdmrep.json`` on the URL's host reserves TDM for its path."""
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if host not in self._tdmrep:
            self._tdmrep[host] = self._load_tdmrep(parts.scheme, parts.netloc, host)
        rules = self._tdmrep[host]
        if rules is None:
            return "unknown"
        return "reserved" if tdm.reserved_by_tdmrep(rules, parts.path) else "not_reserved"

    def _load_tdmrep(self, scheme: str, netloc: str, host: str) -> list[dict[str, object]] | None:
        cached = self.policies.get(host, "tdmrep")
        if cached is not None and self._fresh(cached, self.settings.tdm_cache_hours):
            text: str | None = cached.text
        else:
            text = None
            try:
                resp = self.get(f"{scheme}://{netloc}/.well-known/tdmrep.json")
            except (RequestBlocked, FetchError) as e:
                log.warning("tdmrep_unavailable", extra={"host": host, "reason": e.reason})
                return None
            if resp.ok and resp.content.lstrip().startswith(b"["):
                text = resp.content.decode("utf-8", errors="replace")
            elif resp.ok or resp.status in (404, 410):
                text = "[]"  # no file; a 200 that is not a JSON list is a soft 404 page
            if text is None:
                log.warning("tdmrep_unavailable", extra={"host": host, "status": str(resp.status)})
                return None
            self.policies.put(host, "tdmrep", text, self._now())
        try:
            return tdm.parse_tdmrep(text)
        except tdm.TdmRepError:
            log.warning("tdmrep_invalid", extra={"host": host})
            return None
