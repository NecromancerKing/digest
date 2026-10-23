"""A fake internet for ingest tests: fixed routes, and a log of every request that was sent.

Any request to a URL without a route is recorded and answered with 599, so a test can
assert that nothing unexpected was ever requested.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

Route = httpx.Response | Callable[[httpx.Request], httpx.Response]


def rss(*items: str, lang: str = "en") -> bytes:
    """An RSS 2.0 document around ``<item>`` snippets."""
    body = "".join(items)
    return (
        f'<?xml version="1.0"?><rss version="2.0"><channel><title>Fixture</title>'
        f"<link>https://news.example/</link><language>{lang}</language>{body}</channel></rss>"
    ).encode()


def item(title: str, link: str, *, guid: str | None = None, desc: str = "", date: str = "") -> str:
    parts = [f"<title>{title}</title>", f"<link>{link}</link>"]
    if guid:
        parts.append(f"<guid>{guid}</guid>")
    if desc:
        parts.append(f"<description><![CDATA[{desc}]]></description>")
    if date:
        parts.append(f"<pubDate>{date}</pubDate>")
    return "<item>" + "".join(parts) + "</item>"


@dataclass
class FakeWeb:
    routes: dict[str, Route] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)

    def route(self, url: str, response: Route) -> None:
        self.routes[url] = response

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get(str(request.url))
        if route is None:
            return httpx.Response(599)
        return route(request) if callable(route) else route

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))

    def hosts(self) -> set[str]:
        return {r.url.host for r in self.requests}

    def urls(self) -> list[str]:
        return [str(r.url) for r in self.requests]

    def unrouted(self) -> list[str]:
        return [u for u in self.urls() if u not in self.routes]
