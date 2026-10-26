"""``uv run poe sources-review``: write ``docs/SOURCES_REVIEW.md`` (spec section 4).

For every source in the registry, enabled or not: robots.txt verdict for our user agent,
TDM-reservation status, feed health and a suggested policy. Results are kept per
environment (``local`` and ``github``) in ``docs/sources_review.json``, so a run on a GitHub
runner shows which feeds fail from GitHub's IP ranges next to a local run.

Both files are public: they hold verdicts, counts and registry URLs, never feed content.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from digest.config import Config, Source
from digest.ingest import tdm
from digest.ingest.feeds import Entry, FeedParseError, parse_feed
from digest.ingest.fetcher import Fetcher, FetchError
from digest.ingest.guard import RequestBlocked
from digest.ingest.hostpolicy import Cached
from digest.timeutil import iso, utcnow

ENVIRONMENTS = ("local", "github")


class MemoryPolicyCache:
    """Policy cache for one review run; the review never touches the state file."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], Cached] = {}

    def get(self, host: str, kind: str) -> Cached | None:
        return self._data.get((host, kind))

    def put(self, host: str, kind: str, text: str, at: dt.datetime) -> None:
        self._data[(host, kind)] = Cached(text, at)


def current_environment() -> str:
    return "github" if os.environ.get("GITHUB_ACTIONS") == "true" else "local"


# --------------------------------------------------------------------------- checks


def check_feed(
    fetcher: Fetcher, source: Source, url: str, kind: str, now: dt.datetime
) -> tuple[dict[str, Any], list[Entry]]:
    out: dict[str, Any] = {"url": url, "kind": kind, "robots": fetcher.robots_verdict(url)}
    if kind == "data_api":
        out["note"] = "data_api: checked in Phase 3 (template stories)"
        return out, []
    if out["robots"] != "allowed":
        return out, []
    try:
        resp = fetcher.get(url)
    except RequestBlocked as e:
        out["error"] = f"blocked_{e.reason}"
        return out, []
    except FetchError as e:
        out["error"] = e.reason
        return out, []
    out["status"] = resp.status
    if not resp.ok:
        return out, []
    try:
        entries = parse_feed(resp.content, kind, resp.url)
    except FeedParseError:
        out["error"] = "parse_error"
        return out, []
    out["entries"] = len(entries)
    dates = [e.published_at for e in entries if e.published_at]
    if dates:
        out["newest_age_hours"] = round((now - max(dates)).total_seconds() / 3600, 1)
    out["tdm_header"] = tdm.reserved_by_headers(resp.headers)
    return out, entries


def check_article(fetcher: Fetcher, url: str) -> dict[str, Any]:
    """TDM and robots status of one sample article page. The page is read, never kept."""
    out: dict[str, Any] = {}
    try:
        host = fetcher.guard.check(url)
    except RequestBlocked:
        out["host_registered"] = False
        return out
    out["host_registered"] = True
    out["host"] = host
    out["robots"] = fetcher.robots_verdict(url)
    out["site_tdm"] = fetcher.site_tdm(url)
    if out["robots"] != "allowed" or out["site_tdm"] == "reserved":
        return out
    try:
        resp = fetcher.get(url, headers={"Accept": "text/html"})
    except (RequestBlocked, FetchError) as e:
        out["error"] = e.reason
        return out
    out["status"] = resp.status
    if resp.ok:
        out["page_tdm"] = (
            "reserved"
            if tdm.reserved_by_headers(resp.headers) or tdm.reserved_by_html(resp.content)
            else "not_reserved"
        )
    return out


def review_source(fetcher: Fetcher, source: Source, now: dt.datetime) -> dict[str, Any]:
    feeds: list[dict[str, Any]] = []
    sample: str | None = None
    for f in source.feeds:
        result, entries = check_feed(fetcher, source, str(f.url), f.kind, now)
        feeds.append(result)
        if sample is None and entries:
            sample = entries[0].link
    out: dict[str, Any] = {
        "checked_at": iso(now),
        "homepage_robots": fetcher.robots_verdict(str(source.homepage)),
        "site_tdm": fetcher.site_tdm(str(source.homepage)),
        "feeds": feeds,
    }
    if sample is not None:
        out["sample_article"] = check_article(fetcher, sample)
    return out


def run_review(
    fetcher: Fetcher,
    config: Config,
    *,
    now: Callable[[], dt.datetime] = utcnow,
    only: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    return {
        s.id: review_source(fetcher, s, now())
        for s in config.sources
        if only is None or s.id in only
    }


# --------------------------------------------------------------------------- suggestions


def _feed_ok(feed: dict[str, Any]) -> bool:
    return feed.get("status") == 200 and feed.get("entries", 0) > 0


def suggest(source: Source, result: dict[str, Any] | None) -> tuple[str, str]:
    """(suggested usage policy, article pages) for one source."""
    if result is None:
        return "not checked", "not checked"
    feeds = [f for f in result.get("feeds", []) if f.get("kind") != "data_api"]
    if any(f.get("robots") == "disallowed" for f in feeds):
        return "do not enable: robots.txt disallows our bot", "no"
    if feeds and not any(_feed_ok(f) for f in feeds):
        return "do not enable yet: no working feed", "no"
    if source.usage_policy == "link_only":
        policy = "link_only (per its terms)"
    elif result.get("site_tdm") == "reserved":
        policy = "facts_only, feed text only (TDM reserved site-wide)"
    else:
        policy = source.usage_policy
    article = cast(dict[str, Any], result.get("sample_article") or {})
    if source.usage_policy == "link_only":
        pages = "no (link_only)"
    elif not article:
        pages = "unknown (no sample article)"
    elif not article.get("host_registered"):
        pages = "no: article host not in registry (add to extra_hosts after review)"
    elif article.get("robots") != "allowed":
        pages = f"no: robots {article.get('robots')}"
    elif "reserved" in (article.get("site_tdm"), article.get("page_tdm")):
        pages = "no: TDM reserved"
    elif article.get("site_tdm") == "unknown" and article.get("page_tdm") == "not_reserved":
        pages = "allowed by robots; page not reserved, site file unreadable (D28)"
    elif article.get("page_tdm") == "not_reserved":
        pages = "allowed by robots and TDM; terms decide"
    else:
        pages = "unknown"
    return policy, pages


# --------------------------------------------------------------------------- files


def load_results(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"environments": {}}
    return cast(dict[str, Any], data) if isinstance(data, dict) else {"environments": {}}


def merge_results(
    existing: dict[str, Any], env: str, results: dict[str, dict[str, Any]], now: dt.datetime
) -> dict[str, Any]:
    envs = cast(dict[str, Any], existing.setdefault("environments", {}))
    entry = cast(dict[str, Any], envs.setdefault(env, {"sources": {}}))
    entry["updated_at"] = iso(now)
    cast(dict[str, Any], entry.setdefault("sources", {})).update(results)
    return existing


def _health(result: dict[str, Any] | None) -> str:
    if result is None:
        return "—"
    parts: list[str] = []
    for f in result.get("feeds", []):
        if f.get("kind") == "data_api":
            parts.append("data API (later)")
        elif f.get("robots") != "allowed":
            parts.append(f"robots {f.get('robots')}")
        elif "error" in f:
            parts.append(str(f["error"]))
        elif f.get("status") != 200:
            parts.append(f"HTTP {f.get('status')}")
        else:
            parts.append(f"ok ({f.get('entries', 0)})")
    return ", ".join(parts) or "no feeds"


def _cell(v: object) -> str:
    return str(v).replace("|", "\\|") if v not in (None, "") else "—"


def render_markdown(config: Config, data: dict[str, Any]) -> str:
    envs = cast(dict[str, Any], data.get("environments", {}))

    def res(env: str, sid: str) -> dict[str, Any] | None:
        return cast(dict[str, Any] | None, envs.get(env, {}).get("sources", {}).get(sid))

    lines = [
        "# Sources review",
        "",
        "Generated by `uv run poe sources-review` (and the `sources-review` workflow on a GitHub",
        "runner). Verdicts and URLs only, never feed content. The owner reads each source's",
        "terms, then sets `usage_policy`, `terms_checked_on` and `enabled` in",
        "`config/sources.yaml` (spec section 4).",
        "",
    ]
    for env in ENVIRONMENTS:
        updated = envs.get(env, {}).get("updated_at")
        lines.append(f"- Last checked from **{env}**: {updated or 'never'}")
    lines += [
        "",
        "Feed health shows each feed's result: `ok (n)` with n entries, an HTTP status, an",
        "error type, or the robots.txt verdict for our user agent.",
        "",
        "| Source | Enabled | Policy now | Feeds (local) | Feeds (GitHub) | Robots | Site TDM "
        "| Suggested policy | Article pages |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for s in config.sources:
        local, gh = res("local", s.id), res("github", s.id)
        latest = gh or local
        policy, pages = suggest(s, latest)
        lines.append(
            "| "
            + " | ".join(
                _cell(v)
                for v in (
                    f"`{s.id}` {s.name}",
                    "yes" if s.enabled else "no",
                    s.usage_policy,
                    _health(local),
                    _health(gh),
                    latest.get("homepage_robots") if latest else None,
                    latest.get("site_tdm") if latest else None,
                    policy,
                    pages,
                )
            )
            + " |"
        )
    lines += ["", "## Terms pages", "", "| Source | Terms | Checked on |", "| --- | --- | --- |"]
    for s in config.sources:
        terms = f"<{s.terms_url}>" if s.terms_url else None
        lines.append(f"| `{s.id}` | {_cell(terms)} | {_cell(s.terms_checked_on)} |")
    lines.append("")
    return "\n".join(lines)
