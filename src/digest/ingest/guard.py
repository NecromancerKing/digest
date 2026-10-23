"""The check that runs before every HTTP request (rules L1 and L2).

A request is allowed only to an http(s) URL whose host belongs to a source in the registry
and is not on the blocklist. The fetcher calls this before each request and again on every
redirect hop, so a redirect can never lead to a blocked or unregistered host.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal
from urllib.parse import urlsplit

from digest.config import Blocklist, Config, Source

BlockReason = Literal["scheme", "blocklist", "unregistered", "robots"]


class RequestBlocked(Exception):
    """A request was refused before it was sent."""

    def __init__(self, reason: BlockReason, host: str) -> None:
        super().__init__(reason)
        self.reason: BlockReason = reason
        self.host = host


class RequestGuard:
    def __init__(self, allowed_hosts: Iterable[str], blocklist: Blocklist) -> None:
        self.allowed_hosts = frozenset(h.lower() for h in allowed_hosts)
        self.blocklist = blocklist
        self.blocked_count = 0

    @classmethod
    def for_sources(cls, config: Config, sources: Iterable[Source]) -> RequestGuard:
        hosts: set[str] = set()
        for s in sources:
            hosts |= s.hosts()
        return cls(hosts, config.blocklist)

    def check(self, url: str) -> str:
        """Return the URL's host, or raise ``RequestBlocked``."""
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
        reason: BlockReason | None = None
        if parts.scheme not in ("http", "https") or not host:
            reason = "scheme"
        elif self.blocklist.is_blocked(host):  # checked first: the blocklist always wins
            reason = "blocklist"
        elif host not in self.allowed_hosts:
            reason = "unregistered"
        if reason is not None:
            self.blocked_count += 1
            raise RequestBlocked(reason, host)
        return host
