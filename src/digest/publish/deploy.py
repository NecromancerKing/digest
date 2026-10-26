"""Build the site at the end of a run and deploy it to Cloudflare Pages when it changed.

Deploy-on-change (spec section 8, stage 8): a fingerprint of everything the pages show
(stories, warnings, corrections and the code version) is kept in ``kv``. A run deploys
when it differs from the last deployed one, or when the last deploy is older than
``publish.rebuild_if_older_than_hours`` (time-based pages such as Today), but never sooner
than ``MIN_MINUTES_BETWEEN_DEPLOYS`` after the last, except for a takedown. A failed build or
upload changes nothing: Cloudflare keeps serving the previous deployment. After a deploy,
search engines that take IndexNow pings hear which pages changed.

The upload uses Cloudflare's own ``wrangler`` CLI (``npx``, Node is on GitHub's runners);
its output lists file names, so it is captured and never logged.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import logging
import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

import httpx
from sqlalchemy import Engine

from digest.config import Config, Settings
from digest.publish import indexnow
from digest.publish.model import SiteStory
from digest.publish.render import BuildError, build_site
from digest.publish.source import StateStorySource
from digest.publish.warnings import WeatherWarning, active_warnings
from digest.takedowns import corrections as load_corrections
from digest.timeutil import iso, parse_iso

log = logging.getLogger(__name__)

API = "https://api.cloudflare.com/client/v4"
WRANGLER = "wrangler@4"
KV_FINGERPRINT = "publish.fingerprint"
KV_DEPLOYED_AT = "publish.deployed_at"
KV_CORRECTIONS = "publish.corrections"


class DeployError(Exception):
    pass


class Deployer(Protocol):
    def deploy(self, site: Path, commit: str) -> str:
        """Upload ``site``; returns the deployment's address."""
        ...


class CloudflarePages:
    """Direct upload to one Pages project, created on first use."""

    def __init__(self, token: str, account_id: str, project: str) -> None:
        self.token = token
        self.account_id = account_id
        self.project = project

    def ensure_project(self) -> str:
        """Create the project if missing; returns its ``<sub>.pages.dev`` subdomain."""
        url = f"{API}/accounts/{self.account_id}/pages/projects"
        headers = {"Authorization": f"Bearer {self.token}"}
        with httpx.Client(timeout=30, headers=headers) as client:
            r = client.get(f"{url}/{self.project}")
            if r.status_code == 404:
                r = client.post(url, json={"name": self.project, "production_branch": "main"})
            if r.status_code >= 400:
                raise DeployError(f"Cloudflare API: HTTP {r.status_code}")
            return str(r.json()["result"]["subdomain"])

    def deploy(self, site: Path, commit: str) -> str:
        subdomain = self.ensure_project()
        npx = shutil.which("npx")
        if npx is None:
            raise DeployError("npx not found: Node.js is needed for wrangler")
        env = {
            **os.environ,
            "CLOUDFLARE_API_TOKEN": self.token,
            "CLOUDFLARE_ACCOUNT_ID": self.account_id,
            "WRANGLER_SEND_METRICS": "false",
        }
        done = subprocess.run(  # noqa: S603 - fixed command, our own arguments
            [
                npx, "--yes", WRANGLER, "pages", "deploy", str(site),
                "--project-name", self.project, "--branch", "main",
                "--commit-hash", commit, "--commit-message", "digest publish",
                "--commit-dirty=true",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )  # fmt: skip
        if done.returncode != 0:
            raise DeployError(f"wrangler exited with {done.returncode}")
        return f"https://{subdomain}"


def fingerprint(stories: Sequence[SiteStory], warnings: Sequence[WeatherWarning], code: str) -> str:
    payload = json.dumps(
        [code, [dataclasses.asdict(s) for s in stories], [dataclasses.asdict(w) for w in warnings]],
        default=str,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _kv(engine: Engine, key: str) -> str | None:
    from digest.runner import get_kv

    return get_kv(engine, key)


def publish(
    engine: Engine,
    settings: Settings,
    config: Config,
    now: dt.datetime,
    *,
    deployer: Deployer | None,
    out: Path = Path("site"),
) -> dict[str, int | str]:
    """Build and, when due, deploy. Returns counts for the run's stats (no third-party text)."""
    from digest.runner import set_kv

    if deployer is None:
        return {"skipped": "not_configured"}
    stories = StateStorySource(
        engine, config, settings.edition_lang, show_links=settings.shows_links
    ).stories()
    warnings = active_warnings(engine, config, now)
    corrections = load_corrections(engine)
    code = os.environ.get("GITHUB_SHA", "local")
    # Settings that change the pages count as a change too (SITE_URL, noindex, contact, ...).
    shown = [
        settings.site_url, settings.site_noindex, settings.contact_email,
        settings.product_name, settings.edition_lang, settings.summary_mode,
    ]  # fmt: skip
    fixes = json.dumps([dataclasses.asdict(c) for c in corrections], default=str)
    fp = fingerprint(stories, warnings, json.dumps([code, shown, fixes]))
    # A takedown goes live at once, without waiting for MIN_MINUTES_BETWEEN_DEPLOYS.
    takedown = hashlib.sha256(fixes.encode()).hexdigest() != (_kv(engine, KV_CORRECTIONS) or "")
    last = _kv(engine, KV_DEPLOYED_AT)
    age = (now - parse_iso(last)) if last else None
    too_soon = age is not None and age < dt.timedelta(minutes=settings.min_minutes_between_deploys)
    if too_soon and not takedown:
        return {"skipped": "too_soon"}
    stale = age is None or age >= dt.timedelta(
        hours=config.settings.publish.rebuild_if_older_than_hours
    )
    if fp == _kv(engine, KV_FINGERPRINT) and not stale:
        return {"skipped": "unchanged"}
    result = build_site(
        out,
        stories,
        settings=settings,
        config=config,
        now=now,
        warnings=warnings,
        corrections=corrections,
        search=True,
    )
    deployer.deploy(out, code)
    set_kv(engine, KV_FINGERPRINT, fp)
    set_kv(engine, KV_CORRECTIONS, hashlib.sha256(fixes.encode()).hexdigest())
    set_kv(engine, KV_DEPLOYED_AT, iso(now))
    counts: dict[str, int | str] = {
        "deployed": 1, "pages": result.pages, "stories": result.stories,
    }  # fmt: skip
    if indexnow.enabled(settings) and settings.site_url:
        since = parse_iso(last) if last else None
        urls = indexnow.changed_urls(settings.site_url, stories, since)
        counts["indexnow"] = indexnow.ping(settings.site_url, urls)
    return counts


def make_deployer(settings: Settings) -> Deployer | None:
    """Cloudflare Pages when its token, account and project are set; else no deploys."""
    token = settings.cloudflare_api_token
    if token is None or not settings.cloudflare_account_id or not settings.cf_pages_project:
        return None
    return CloudflarePages(
        token.get_secret_value(), settings.cloudflare_account_id, settings.cf_pages_project
    )


__all__ = ["BuildError", "DeployError", "make_deployer", "publish"]
