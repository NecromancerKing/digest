from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from digest import db
from digest.config import Config, Settings, load_config
from digest.db.schema import clusters, stories, story_revisions
from digest.publish.model import SiteStory, SourceRef
from digest.publish.render import MARKER, BuildError, build_site
from digest.publish.select import capped, front_page, today_stories
from digest.publish.source import StateStorySource
from digest.timeutil import iso

from .conftest import CONFIG_DIR

NOW = dt.datetime(2026, 10, 24, 12, 0, tzinfo=dt.UTC)  # 14:00 in Stockholm


@pytest.fixture(scope="module")
def config() -> Config:
    return load_config(CONFIG_DIR)


def src(name: str, minutes_ago: int = 60, headline: str | None = None) -> SourceRef:
    return SourceRef(
        name=name,
        url=f"https://{name.lower()}.example/a",
        published_at=NOW - dt.timedelta(minutes=minutes_ago),
        kind="news",
        lang="en",
        headline=headline,
    )


def story(
    sid: int,
    *,
    category: str = "world",
    kind: str = "llm",
    score: float = 1.0,
    hours_ago: float = 1,
    sources: int = 2,
    summary: str | None = "A summary in our own words.",
    headline: str | None = None,
) -> SiteStory:
    at = NOW - dt.timedelta(hours=hours_ago)
    return SiteStory(
        id=sid,
        slug=f"story-{sid}",
        day="2026-10-24",
        category=category,
        kind=kind,
        headline=headline or f"Headline {sid}",
        summary=None if kind == "links" else summary,
        why_it_matters=None,
        sources=tuple(src(f"S{sid}x{i}") for i in range(sources)),
        independent_sources=sources,
        rank_score=score,
        first_item_at=at,
        published_at=at,
        updated_at=None,
    )


# --------------------------------------------------------------------------- selection


def test_capped_limits_each_category() -> None:
    items = [story(1), story(2), story(3), story(4, category="sport")]
    assert [s.id for s in capped(items, 3, 2)] == [1, 2, 4]


def test_front_page_lead_prefers_a_summary_and_caps_categories(config: Config) -> None:
    items = [
        story(1, kind="links", score=9),
        story(2, score=8),
        story(3, score=7),
        story(4, score=6),  # third world story: over top_stories_max_per_category
        story(5, category="economy", score=5),
    ]
    fp = front_page(items, config.categories, NOW, Settings().zone, config.settings, llm_mode=True)
    assert fp.lead is not None and fp.lead.id == 2
    assert [s.id for s in fp.top] == [1, 5]
    world = next(s for s in fp.sections if s.category.id == "world")
    assert world.lead is not None and world.lead.id == 4  # not on top: shown in its section


def test_developing_only_young_links_cards_in_llm_mode(config: Config) -> None:
    items = [story(1, kind="links", hours_ago=2), story(2, kind="links", hours_ago=10), story(3)]
    zone = Settings().zone
    fp = front_page(items, config.categories, NOW, zone, config.settings, llm_mode=True)
    assert [s.id for s in fp.developing] == [1]
    fp = front_page(items, config.categories, NOW, zone, config.settings, llm_mode=False)
    assert fp.developing == []


def test_most_covered_orders_by_independent_sources(config: Config) -> None:
    items = [story(1, sources=2, score=9), story(2, sources=5, score=1), story(3, sources=3)]
    fp = front_page(items, config.categories, NOW, Settings().zone, config.settings, llm_mode=True)
    assert [s.id for s in fp.most_covered] == [2, 3, 1]


def test_today_window_and_category_cap(config: Config) -> None:
    items = [story(i, score=10 - i) for i in range(1, 5)] + [story(9, hours_ago=30)]
    ids = [s.id for s in today_stories(items, NOW, config.settings)]
    assert ids == [1, 2, 3]  # at most 3 per category; the 30-hour-old story is out


# --------------------------------------------------------------------------- build


def build(tmp_path: Path, config: Config, items: list[SiteStory]) -> Path:
    out = tmp_path / "site"
    build_site(out, items, settings=Settings(), config=config, now=NOW)
    return out


def test_build_writes_every_page(tmp_path: Path, config: Config) -> None:
    out = build(tmp_path, config, [story(1), story(2, category="sport")])
    for rel in ("index.html", "today.html", "404.html", "2026-10-24.html", "c/sport.html"):
        assert (out / rel).is_file(), rel
    assert (out / "story/2026-10-24/story-1.html").is_file()
    assert (out / "assets/css/site.css").is_file()
    assert (out / MARKER).is_file()


def test_ai_label_only_on_ai_written_stories(tmp_path: Path, config: Config) -> None:
    items = [story(1), story(2, kind="links"), story(3, kind="template")]
    out = build(tmp_path, config, items)
    page = {i: (out / f"story/2026-10-24/story-{i}.html").read_text("utf-8") for i in (1, 2, 3)}
    assert "Summarised with AI from 2 sources" in page[1] and 'href="/ai"' in page[1]
    assert "Summarised with AI" not in page[2]
    assert "Summarised with AI" not in page[3]
    home = (out / "index.html").read_text("utf-8")
    assert 'class="ai-chip lift" href="/ai"' in home


def test_source_headlines_only_on_links_cards(tmp_path: Path, config: Config) -> None:
    links = story(1, kind="links")
    links = SiteStory(
        **{**links.__dict__, "sources": (src("Radio", headline="Their own headline"),)}
    )
    out = build(tmp_path, config, [links])
    assert "Their own headline" in (out / "story/2026-10-24/story-1.html").read_text("utf-8")


def test_text_is_escaped(tmp_path: Path, config: Config) -> None:
    out = build(tmp_path, config, [story(1, headline="<script>alert(1)</script>")])
    html = (out / "story/2026-10-24/story-1.html").read_text("utf-8")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_build_replaces_its_own_output_only(tmp_path: Path, config: Config) -> None:
    out = build(tmp_path, config, [story(1)])
    (out / "stale.html").write_text("old", encoding="utf-8")
    build_site(out, [story(1)], settings=Settings(), config=config, now=NOW)
    assert not (out / "stale.html").exists()

    foreign = tmp_path / "docs"
    foreign.mkdir()
    (foreign / "keep.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(BuildError):
        build_site(foreign, [story(1)], settings=Settings(), config=config, now=NOW)
    assert (foreign / "keep.txt").exists()


def test_unknown_category_fails_before_writing(tmp_path: Path, config: Config) -> None:
    with pytest.raises(BuildError):
        build_site(
            tmp_path / "site",
            [story(1, category="nope")],
            settings=Settings(),
            config=config,
            now=NOW,
        )
    assert not (tmp_path / "site").exists()


# --------------------------------------------------------------------------- state source


def test_state_source_reads_visible_stories(tmp_path: Path, config: Config) -> None:
    path = tmp_path / "state.sqlite"
    db.migrate(path)
    engine = db.make_engine(path)
    line = {
        "item_id": 1,
        "source_id": "nope",
        "name": "SVT",
        "url": "https://svt.example/1",
        "published_at": iso(NOW),
        "headline": "Not for LLM stories",
    }
    with engine.begin() as conn:
        for cid, status in ((1, "approved"), (2, "needs_review"), (3, "published")):
            conn.execute(
                clusters.insert().values(
                    id=cid, created_at=iso(NOW), last_item_at=iso(NOW), source_count=2
                )
            )
            conn.execute(
                stories.insert().values(
                    id=cid,
                    cluster_id=cid,
                    edition_lang="en",
                    slug=f"s{cid}",
                    path=f"stories/2026-10-24/s{cid}.yaml",
                    category="world",
                    status=status,
                    current_rev=2 if cid == 1 else 1,
                    updated_at=iso(NOW),
                )
            )
        second = {**line, "name": "DR", "url": "https://dr.example/1"}
        for rev, kind, lines in ((1, "links", [line]), (2, "llm", [line, second])):
            conn.execute(
                story_revisions.insert().values(
                    story_id=1,
                    rev_no=rev,
                    headline="Our headline",
                    summary=None if kind == "links" else "Our summary.",
                    claims="[]",
                    sources=json.dumps(lines),
                    verification="{}",
                    created_by="pipeline",
                    kind=kind,
                    created_at=iso(NOW + dt.timedelta(minutes=rev)),
                )
            )
        for sid in (2, 3):
            conn.execute(
                story_revisions.insert().values(
                    story_id=sid,
                    rev_no=1,
                    headline="Other",
                    summary="S.",
                    claims="[]",
                    sources=json.dumps([line]),
                    verification="{}",
                    created_by="pipeline",
                    kind="llm",
                    created_at=iso(NOW),
                )
            )
    got = {s.id: s for s in StateStorySource(engine, config, "en").stories()}
    assert sorted(got) == [1, 3]  # needs_review is never shown
    s = got[1]
    assert s.kind == "llm" and s.url == "/story/2026-10-24/s1"
    assert all(x.headline is None for x in s.sources)  # rule L10
    assert s.updated_at is not None
    assert [c.note for c in s.changes] == ["DR joined as a source", "First published from SVT"]


def test_fonts_stay_within_budget() -> None:
    from digest.publish.fonts import BUDGET_BYTES, OUT

    fonts = list(OUT.glob("*.woff2"))
    assert {f.stem for f in fonts} == {"fraunces", "figtree"}
    assert sum(f.stat().st_size for f in fonts) <= BUDGET_BYTES  # NFR-1
    assert all((OUT / f"{f.stem}-OFL.txt").is_file() for f in fonts)  # the licence travels


def test_scripts_stay_within_budget_and_get_their_strings(tmp_path: Path, config: Config) -> None:
    import gzip
    import re

    js = (Path(__file__).resolve().parents[1] / "src/digest/publish/static/js/site.js").read_bytes()
    assert len(gzip.compress(js)) <= 20 * 1024  # NFR-1
    out = build(tmp_path, config, [story(1)])
    html = (out / "today.html").read_text("utf-8")
    blob = re.search(r'<script type="application/json" id="strings">(.*?)</script>', html, re.S)
    assert blob is not None
    strings = json.loads(blob.group(1))
    assert strings["next"] == "Next story" and "continue" in strings
