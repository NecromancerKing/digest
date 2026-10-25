"""Taking stories down (D19): there is no review step, so this is the owner's one lever.

The owner lists stories in ``takedowns.yaml`` on the content repository's ``main``, which
GitHub's web and mobile apps can edit:

    - story: https://digest-dev.pages.dev/story/2026-10-25/some-story
      note: Taken down because it gave the wrong date for the vote.

Every run reads the file before publishing: a listed story is unpublished, and one no
longer listed is shown again. The corrections page lists each takedown with its note.
The file's history in the content repository is the record of every editorial action.

Logs hold counts only: story addresses can contain words from source headlines.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast

import httpx
import yaml
from sqlalchemy import Engine, and_, select

from digest.db.schema import stories, story_revisions
from digest.publish.model import Correction
from digest.timeutil import iso, parse_iso

log = logging.getLogger(__name__)

API = "https://api.github.com"
FILE = "takedowns.yaml"
KV_KEY = "takedowns"
ADDRESS = re.compile(r"story/(\d{4}-\d{2}-\d{2})/([a-z0-9-]+)")
OUR_HEADLINES = ("llm", "template", "owner_blurb")  # links-card labels may quote a source


class TakedownError(Exception):
    pass


@dataclass(frozen=True)
class Takedown:
    day: str
    slug: str
    note: str

    @property
    def path(self) -> str:
        return f"stories/{self.day}/{self.slug}.yaml"


def fetch(repo: str, token: str, client: httpx.Client | None = None) -> str | None:
    """The file's text from the content repository, or None if it does not exist."""
    own = client is None
    http = client or httpx.Client(timeout=30, transport=httpx.HTTPTransport(retries=3))
    try:
        r = http.get(
            f"{API}/repos/{repo}/contents/{FILE}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.raw+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "digest-pipeline",
            },
        )
    except httpx.HTTPError as e:
        raise TakedownError(type(e).__name__) from e
    finally:
        if own:
            http.close()
    if r.status_code == 404:
        return None
    if r.is_error:
        raise TakedownError(f"HTTP {r.status_code}")
    return r.text


def parse(text: str | None) -> tuple[list[Takedown], int]:
    """Entries and the number of entries that could not be read."""
    if not text:
        return [], 0
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise TakedownError("not valid YAML") from e
    if data is None:
        return [], 0
    if not isinstance(data, list):
        raise TakedownError("not a list")
    out: list[Takedown] = []
    bad = 0
    for entry in cast(list[Any], data):
        if not isinstance(entry, dict):
            bad += 1
            continue
        e = cast(dict[str, Any], entry)
        m = ADDRESS.search(str(e.get("story") or ""))
        note = " ".join(str(e.get("note") or "").split())
        if m is None or not note:
            bad += 1
            continue
        out.append(Takedown(m.group(1), m.group(2), note))
    return out, bad


def apply(
    engine: Engine, takedowns: Sequence[Takedown], lang: str, now: dt.datetime
) -> dict[str, int]:
    """Unpublish listed stories, show unlisted ones again, and record the corrections."""
    from digest.runner import get_kv, set_kv

    wanted = {t.path: t for t in takedowns}
    before: dict[str, dict[str, Any]] = json.loads(get_kv(engine, KV_KEY) or "{}")
    record: dict[str, dict[str, Any]] = {}
    stats = {"down": 0, "restored": 0, "unmatched": 0}
    with engine.begin() as conn:
        rows = {
            r.path: r
            for r in conn.execute(
                select(
                    stories.c.id,
                    stories.c.path,
                    stories.c.status,
                    story_revisions.c.kind,
                    story_revisions.c.headline,
                )
                .outerjoin(
                    story_revisions,
                    and_(
                        story_revisions.c.story_id == stories.c.id,
                        story_revisions.c.rev_no == stories.c.current_rev,
                    ),
                )
                .where(
                    stories.c.edition_lang == lang,
                    (stories.c.path.in_(list(wanted))) | (stories.c.status == "unpublished"),
                )
            )
        }
        for path, t in wanted.items():
            row = rows.get(path)
            if row is None:
                stats["unmatched"] += 1
                continue
            if row.status != "unpublished":
                conn.execute(
                    stories.update()
                    .where(stories.c.id == row.id)
                    .values(status="unpublished", updated_at=iso(now))
                )
                stats["down"] += 1
            key = str(row.id)
            record[key] = {
                "day": t.day,
                "headline": row.headline if row.kind in OUR_HEADLINES else None,
                "note": t.note,
                "at": before.get(key, {}).get("at") or iso(now),
            }
        for path, row in rows.items():
            if row.status == "unpublished" and path not in wanted:
                conn.execute(
                    stories.update()
                    .where(stories.c.id == row.id)
                    .values(status="approved", updated_at=iso(now))
                )
                stats["restored"] += 1
    set_kv(engine, KV_KEY, json.dumps(record, sort_keys=True, ensure_ascii=False))
    return stats


def corrections(engine: Engine) -> list[Correction]:
    """What the corrections page lists, newest first."""
    from digest.runner import get_kv

    record: dict[str, dict[str, Any]] = json.loads(get_kv(engine, KV_KEY) or "{}")
    out = [
        Correction(at=parse_iso(c["at"]), day=c["day"], headline=c["headline"], note=c["note"])
        for c in record.values()
    ]
    return sorted(out, key=lambda c: c.at, reverse=True)


def run_takedowns(
    engine: Engine, repo: str | None, token: str | None, lang: str, now: dt.datetime
) -> dict[str, int | str]:
    """Read the file and apply it. A file that can't be read changes nothing."""
    if not repo or not token:
        return {"skipped": "not_configured"}
    try:
        entries, bad = parse(fetch(repo, token))
    except TakedownError as e:
        log.warning("takedowns_unreadable", extra={"reason": str(e)})  # our fixed messages
        return {"failed": 1}
    stats: dict[str, int | str] = {**apply(engine, entries, lang, now), "invalid": bad}
    if bad or stats["unmatched"]:
        log.warning(
            "takedowns_not_applied", extra={"invalid": bad, "unmatched": stats["unmatched"]}
        )
    return stats
