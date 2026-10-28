"""Static HTML from stories: Jinja2 templates with autoescape, written to a build directory.

The build directory is replaced as a whole, so a page that no longer exists never lingers.
It must be new, empty, or a directory an earlier build wrote (it carries a marker file),
so a mistyped path can't wipe anything else.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import math
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from digest.config import Category, Config, Settings
from digest.datastories.render import join_list
from digest.i18n import strings, t
from digest.publish import feeds, indexnow
from digest.publish import pages as pages_mod
from digest.publish.model import Correction, SiteStory
from digest.publish.select import (
    by_day,
    category_stories,
    front_page,
    region_stories,
    today_stories,
)
from digest.publish.warnings import WeatherWarning
from digest.timeutil import iso

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE / "templates"
STATIC = HERE / "static"
SERVICE_WORKER = HERE / "sw.js"
MARKER = ".digest-site"
THEME_LIGHT, THEME_DARK = "#F5F1EB", "#1A1714"  # --bg in site.css
LOCAL_URL = "http://127.0.0.1:8000"  # without SITE_URL: links for local previews

# Line icons drawn for the Digest (24 by 24 viewBox, stroked with currentColor).
ICONS = {
    "pin": (
        "M12 21s-7-6.1-7-11.5a7 7 0 0 1 14 0C19 14.9 12 21 12 21z"
        "M12 12a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5z"
    ),
    "globe": (
        "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM3 12h18"
        "M12 3c2.5 2.7 3.8 5.7 3.8 9s-1.3 6.3-3.8 9c-2.5-2.7-3.8-5.7-3.8-9S9.5 5.7 12 3z"
    ),
    "trend": "M3 17l6-6 4 4 8-8M15 7h6v6",
    "flask": "M9 3h6M10 3v6l-5.2 9.1A2 2 0 0 0 6.5 21h11a2 2 0 0 0 1.7-2.9L14 9V3M7.5 15h9",
    "leaf": "M4 20c1-9 6-15 16-16-1 10-7 15-16 16zM4 20c4-5 7-8 11-10",
    "trophy": "M8 4h8v5a4 4 0 0 1-8 0zM8 6H5a3 3 0 0 0 3 4M16 6h3a3 3 0 0 1-3 4M12 13v4M8.5 20h7",
    "search": "M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14zM20 20l-3.5-3.5",
    "sun": (
        "M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM12 2v2M12 20v2M4.9 4.9l1.4 1.4"
        "M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"
    ),
    "moon": "M20.5 13.5A8.5 8.5 0 1 1 10.5 3.5a6.5 6.5 0 0 0 10 10z",
    "arrow": "M5 12h14M13 6l6 6-6 6",
    "back": "M15 6l-6 6 6 6",
    "chev-right": "M9 6l6 6-6 6",
    "chev-down": "M6 9l6 6 6-6",
    "star": "M12 3l1.9 5.6L19.5 10.5l-5.6 1.9L12 18l-1.9-5.6L4.5 10.5l5.6-1.9z",
    "layers": "M12 3l9 5-9 5-9-5 9-5zM3 13l9 5 9-5",
    "bulb": (
        "M9 18h6M10 21h4"
        "M12 3a6 6 0 0 0-3.5 10.9c.6.5 1 1.2 1 2.1h5c0-.9.4-1.6 1-2.1A6 6 0 0 0 12 3z"
    ),
    "warning": "M12 4l9 16H3L12 4zM12 10v4M12 17h.01",
    "external": "M7 17L17 7M9 7h8v8",
    "check": "M5 12.5l4.5 4.5L19 7.5",
    "home": "M4 11l8-7 8 7v8a1 1 0 0 1-1 1h-4v-6h-6v6H5a1 1 0 0 1-1-1z",
    "grid": "M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z",
    "link": (
        "M10 14a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1M14 10a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1"
    ),
    "share": "M12 3v12M7 8l5-5 5 5M5 14v5a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-5",
    "install": "M12 4v11M7 10l5 5 5-5M5 20h14",
    "close": "M6 6l12 12M18 6L6 18",
}
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


class BuildError(Exception):
    pass


@dataclass(frozen=True)
class BuildResult:
    pages: int
    stories: int


class Renderer:
    def __init__(self, settings: Settings, config: Config, now: dt.datetime) -> None:
        self.settings = settings
        self.config = config
        self.now = now
        self.zone = settings.zone
        self.lang = settings.edition_lang
        self.cats = {c.id: c for c in config.categories}
        self.base = (settings.site_url or LOCAL_URL).rstrip("/")
        env = Environment(
            loader=FileSystemLoader(TEMPLATES),
            autoescape=select_autoescape(default=True, default_for_string=True),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        env.globals.update(
            t=self.text,
            icon_path=ICONS.__getitem__,
            cat=self.cat,
            cat_class=lambda cid: f"c-{self.cat(cid).color}",
            cat_name=lambda cid: self.cat(cid).name(self.lang),
            cat_short=lambda cid: self.cat(cid).short_name(self.lang),
            cat_url=lambda cid: f"/c/{self.cat(cid).slug}/",
            categories=config.categories,
            product=settings.product_name,
            lang=self.lang,
            now=now,
            join=lambda names: join_list(list(names), self.lang),
            sources_n=self.sources_n,
            tz=settings.tz,
            abs_url=self.abs_url,
            contact_email=settings.contact_email,
            noindex=settings.site_noindex,
            theme_light=THEME_LIGHT,
            theme_dark=THEME_DARK,
            seconds_per_story=config.settings.publish.briefing_seconds_per_story,
            js_strings={
                k.removeprefix("site.js."): v
                for k, v in strings(self.lang).items()
                if k.startswith("site.js.")
            },
        )
        env.filters.update(iso=iso, clock=self.clock, when=self.when, short_when=self.short_when)
        env.globals.update(warning_when=self.warning_when, warning_areas=self.warning_areas)
        self.env = env

    # ------------------------------------------------------------- helpers
    def text(self, key: str, **kw: Any) -> str:
        """An interface string; ``<key>_one`` wins when ``n`` is 1 and the file has one."""
        s = None
        if kw.get("n") == 1:
            with contextlib.suppress(KeyError):
                s = t(self.lang, key + "_one")
        s = s if s is not None else t(self.lang, key)
        return s.format(**kw) if kw else s

    def abs_url(self, path: str) -> str:
        return self.base + path

    def feed_text(self, s: SiteStory) -> str:
        """A feed item's text: our summary, or who reported a links card. Never source text."""
        if s.summary:
            return s.summary
        return self.text(
            "site.reported_by", sources=join_list([x.name for x in s.sources[:5]], self.lang)
        )

    def cat(self, cid: str) -> Category:
        try:
            return self.cats[cid]
        except KeyError as e:
            raise BuildError(f"story in unknown category {cid!r}") from e

    def sources_n(self, n: int) -> str:
        return self.text("site.source_1") if n == 1 else self.text("site.sources_n", n=n)

    def _local(self, at: dt.datetime) -> dt.datetime:
        return at.astimezone(self.zone)

    def clock(self, at: dt.datetime) -> str:
        return f"{self._local(at):%H:%M}"

    def _days_ago(self, at: dt.datetime) -> int:
        return (self._local(self.now).date() - self._local(at).date()).days

    def _date(self, at: dt.datetime) -> str:
        local = self._local(at)
        return f"{local.day} {self.text('site.month.' + MONTHS[local.month - 1])}"

    def when(self, at: dt.datetime) -> str:
        """Cards: ``13:50`` today, ``Fri 13:50`` this week, ``23 Oct`` before that."""
        days = self._days_ago(at)
        if days <= 0:
            return self.clock(at)
        if days < 7:
            day = self.text("site.weekday." + WEEKDAYS[self._local(at).weekday()])
            return f"{day} {self.clock(at)}"
        return self._date(at)

    def short_when(self, at: dt.datetime) -> str:
        """Rows: ``13:50`` today, ``Fri`` this week, ``23 Oct`` before that."""
        days = self._days_ago(at)
        if days <= 0:
            return self.clock(at)
        if days < 7:
            return self.text("site.weekday." + WEEKDAYS[self._local(at).weekday()])
        return self._date(at)

    def warning_when(self, starts: dt.datetime | None, ends: dt.datetime | None) -> str:
        """SMHI's times, in our words: ``Sat 18:00 to Sun 09:00`` or ``Until Sun 12:00``."""
        if starts is not None and starts > self.now and ends is not None:
            return self.text("site.warn_from_to", start=self.when(starts), end=self.when(ends))
        if starts is not None and starts > self.now:
            return self.text("site.warn_from", start=self.when(starts))
        if ends is not None:
            return self.text("site.warn_until", end=self.when(ends))
        return self.text("site.warn_now")

    def warning_areas(self, areas: Sequence[str], limit: int = 3) -> str:
        shown = list(areas[:limit])
        if len(areas) > limit:
            shown.append(t(self.lang, "words.more_areas").format(n=len(areas) - limit))
        return join_list(shown, self.lang)

    def greeting(self) -> str:
        hour = self._local(self.now).hour
        part = "morning" if 5 <= hour < 12 else "afternoon" if 12 <= hour < 18 else "evening"
        return self.text("site.greeting." + part)

    def long_date(self) -> str:
        local = self._local(self.now)
        weekday = self.text("site.weekday_long." + WEEKDAYS[local.weekday()])
        month = self.text("site.month_long." + MONTHS[local.month - 1])
        return f"{weekday} {local.day} {month}"

    def static_page(self, name: str) -> str:
        """``pages/<lang>/<name>.html.j2``, falling back to English."""
        for lang in (self.lang, "en"):
            if (TEMPLATES / "pages" / lang / f"{name}.html.j2").is_file():
                return f"pages/{lang}/{name}.html.j2"
        raise BuildError(f"no template for the {name} page")

    def page(self, template: str, **ctx: Any) -> str:
        return self.env.get_template(template).render(**ctx)


def _prepare(out: Path) -> None:
    if out.exists():
        if not out.is_dir():
            raise BuildError(f"{out} is not a directory")
        if any(out.iterdir()) and not (out / MARKER).exists():
            raise BuildError(f"{out} is not empty and was not written by a site build")
        shutil.rmtree(out)
    out.mkdir(parents=True)
    (out / MARKER).write_text("written by digest site build\n", encoding="utf-8")


def _write(out: Path, rel: str, html: str) -> None:
    path = out / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


def build_site(
    out: Path,
    stories: Sequence[SiteStory],
    *,
    settings: Settings,
    config: Config,
    now: dt.datetime,
    warnings: Sequence[WeatherWarning] = (),
    corrections: Sequence[Correction] = (),
    search: bool = False,
) -> BuildResult:
    """Render every page into ``out``, replacing what an earlier build left there."""
    r = Renderer(settings, config, now)
    app = config.settings
    for s in stories:
        r.cat(s.category)  # fail early on an unknown category
    _prepare(out)
    pages = 0

    fp = front_page(
        stories,
        config.categories,
        now,
        r.zone,
        app,
        llm_mode=settings.summary_mode == "llm",
    )
    briefing = today_stories(stories, now, app)
    minutes = max(1, math.ceil(len(briefing) * app.publish.briefing_seconds_per_story / 60))
    days = by_day(stories)
    common: dict[str, Any] = {"briefing_minutes": minutes, "archive_day": max(days, default=None)}
    hours = app.publish.category_page_hours
    by_region = {g.id: region_stories(stories, g.id, now, hours) for g in config.regions}
    region_zones = {g.id: g.timezones for g in config.regions}

    _write(
        out,
        "index.html",
        r.page(
            "home.html.j2",
            fp=fp,
            briefing=briefing,
            greeting=r.greeting(),
            long_date=r.long_date(),
            max_cover=max((s.independent_sources for s in fp.most_covered), default=1),
            warnings=warnings,
            regions=config.regions,
            region_lists={k: v[: app.publish.region_box_stories] for k, v in by_region.items()},
            region_zones=region_zones,
            nav="home",
            **common,
        ),
    )
    pages += 1
    _write(out, "today.html", r.page("today.html.j2", briefing=briefing, nav="today", **common))
    pages += 1

    for s in stories:
        more = [
            x
            for x in category_stories(stories, s.category, now, app.publish.category_page_hours)
            if x.id != s.id
        ][:3]
        _write(
            out,
            f"story/{s.day}/{s.slug}.html",
            r.page(
                "story.html.j2",
                s=s,
                more=more,
                nav="story",
                **common,
            ),
        )
        pages += 1

    for c in config.categories:
        items = category_stories(stories, c.id, now, app.publish.category_page_hours)
        _write(
            out,
            f"c/{c.slug}/index.html",
            r.page("category.html.j2", c=c, items=items, nav=c.id, **common),
        )
        pages += 1

    for g in config.regions:
        _write(
            out,
            f"r/{g.slug}/index.html",
            r.page("region.html.j2", g=g, items=by_region[g.id], nav="region", **common),
        )
        pages += 1

    for day, items in days.items():
        groups = [(c, [s for s in items if s.category == c.id]) for c in config.categories]
        _write(
            out,
            f"{day}.html",
            r.page(
                "day.html.j2",
                day=day,
                groups=[g for g in groups if g[1]],
                nav="day",
                **common,
            ),
        )
        pages += 1

    _write(out, "404.html", r.page("404.html.j2", nav="404", **common))
    pages += 1
    _write(out, "search.html", r.page("search.html.j2", nav="search", **common))
    pages += 1
    _write(out, "offline.html", r.page("offline.html.j2", nav="offline", noindex=True, **common))
    pages += 1
    facts = {
        "source_rows": pages_mod.source_rows(config),
        "source_count": len(config.enabled_sources),
        "country_count": pages_mod.country_count(config),
        "model_rows": pages_mod.model_rows(config),
        "corrections": corrections,
    }
    for name in pages_mod.PAGES:
        _write(out, f"{name}.html", r.page(r.static_page(name), nav=name, **facts, **common))
        pages += 1
    _feeds(out, r, stories, config)
    shutil.copytree(STATIC, out / "assets")
    _app(out, r)
    broken = broken_links(out)
    if broken:
        raise BuildError("broken internal links: " + ", ".join(broken[:10]))
    if search:
        _index(out)
    return BuildResult(pages=pages, stories=len(stories))


def _app(out: Path, r: Renderer) -> None:
    """The installable app (issue #19): manifest, service worker, and the story pages it
    saves for offline reading, which are the ones the front page and Today link to."""
    icons = [
        {"src": "/assets/img/icon.svg", "sizes": "any", "type": "image/svg+xml"},
        {"src": "/assets/img/icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "/assets/img/icon-512.png", "sizes": "512x512", "type": "image/png"},
        {
            "src": "/assets/img/icon-maskable.png",
            "sizes": "512x512",
            "type": "image/png",
            "purpose": "maskable",
        },
    ]
    manifest = {
        "id": "/",
        "name": r.settings.product_name,
        "short_name": r.settings.product_name,
        "description": r.text("site.meta_description"),
        "lang": r.lang,
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": THEME_LIGHT,
        "theme_color": THEME_LIGHT,
        "icons": icons,
    }
    _write(out, "manifest.webmanifest", json.dumps(manifest, ensure_ascii=False, indent=1))
    linked = {
        target
        for name in ("index.html", "today.html")
        for target in _LINK.findall((out / name).read_text(encoding="utf-8"))
        if target.startswith("/story/")
    }
    _write(out, "offline.json", json.dumps({"pages": sorted(linked)}))
    shutil.copyfile(SERVICE_WORKER, out / "sw.js")


def _feeds(out: Path, r: Renderer, stories: Sequence[SiteStory], config: Config) -> None:
    """RSS and JSON feeds (overall and per category), the sitemap and robots.txt."""
    title = r.settings.product_name
    description = r.text("site.meta_description")
    common = {
        "home": r.abs_url("/"),
        "lang": r.lang,
        "abs_url": r.abs_url,
        "text": r.feed_text,
        "category": lambda cid: r.cat(cid).name(r.lang),
    }
    _write(
        out,
        "feed.xml",
        feeds.rss(
            stories,
            title=title,
            description=description,
            self_url=r.abs_url("/feed.xml"),
            now=r.now,
            **common,
        ),
    )
    _write(
        out,
        "feed.json",
        feeds.json_feed(
            stories,
            title=title,
            description=description,
            self_url=r.abs_url("/feed.json"),
            **common,
        ),
    )
    for c in config.categories:
        mine = [s for s in stories if s.category == c.id]
        _write(
            out,
            f"c/{c.slug}/feed.xml",
            feeds.rss(
                mine,
                title=f"{title} · {c.name(r.lang)}",
                description=description,
                self_url=r.abs_url(f"/c/{c.slug}/feed.xml"),
                now=r.now,
                **common,
            ),
        )
    urls: list[tuple[str, dt.datetime | None]] = [
        (r.abs_url("/"), r.now),
        (r.abs_url("/today"), r.now),
    ]
    urls += [(r.abs_url(f"/c/{c.slug}/"), r.now) for c in config.categories]
    urls += [(r.abs_url(f"/r/{g.slug}/"), r.now) for g in config.regions]
    urls += [(r.abs_url(f"/{name}"), None) for name in pages_mod.PAGES]
    urls += [(r.abs_url(s.url), s.last_change_at) for s in stories]
    urls += [(r.abs_url(f"/{day}"), None) for day in sorted(by_day(stories))]
    _write(out, "sitemap.xml", feeds.sitemap(urls))
    _write(
        out,
        "robots.txt",
        feeds.robots(r.abs_url("/sitemap.xml"), index=not r.settings.site_noindex),
    )
    if indexnow.enabled(r.settings) and r.settings.site_url:
        k = indexnow.key(r.settings.site_url)
        _write(out, f"{k}.txt", k)


def _index(out: Path) -> None:
    """Pagefind's static search index, from the story pages just written (FR-14)."""
    done = subprocess.run(  # noqa: S603 - our own interpreter and a fixed module
        [sys.executable, "-m", "pagefind", "--site", str(out), "--silent"],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        raise BuildError(f"pagefind failed: {done.stderr.strip()[-500:]}")


_LINK = re.compile(r'(?:href|src)="(/[^"#?]*)')


def broken_links(out: Path) -> list[str]:
    """Internal links in the built pages that point at nothing (spec section 8, stage 8)."""
    bad: set[str] = set()
    for page in out.rglob("*.html"):
        for target in _LINK.findall(page.read_text(encoding="utf-8")):
            if target.startswith("/pagefind/"):
                continue  # written by the search step, after this check
            rel = target.lstrip("/")
            path = out / rel
            ok = (
                rel == ""
                or path.is_file()
                or path.with_name(path.name + ".html").is_file()
                or (path / "index.html").is_file()
            )
            if not ok:
                bad.add(f"{target} (in {page.relative_to(out).as_posix()})")
    return sorted(bad)
