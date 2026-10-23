"""Stage 1: fetch feeds and article pages from registered sources (spec sections 3, 4, 8)."""

from __future__ import annotations

from collections.abc import Iterable

import httpx
from sqlalchemy import Engine

from digest.config import Config, Settings, Source
from digest.ingest.fetcher import Fetcher
from digest.ingest.guard import RequestGuard
from digest.ingest.hostpolicy import HostPolicyCache


def make_fetcher(
    settings: Settings,
    config: Config,
    engine: Engine,
    sources: Iterable[Source],
    *,
    client: httpx.Client | None = None,
) -> Fetcher:
    """A fetcher limited to the hosts of ``sources`` (rule L1) minus the blocklist (L2)."""
    return Fetcher(
        RequestGuard.for_sources(config, sources),
        HostPolicyCache(engine),
        config.settings.fetch,
        user_agent=settings.user_agent,
        robots_token=settings.bot_name,
        client=client,
    )
