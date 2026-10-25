from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest
from sqlalchemy import Engine, func, select

from digest import db
from digest.config import Config, Settings, load_config
from digest.db.schema import cluster_items, clusters, items, stories, story_revisions
from digest.gates.duplicate import find_duplicate
from digest.gates.sensitivity import keyword_flags
from digest.links.cards import Member, build_card, source_lines
from digest.links.places import place_name
from digest.links.stage import run_links
from digest.nlp.vectors import to_blob
from digest.stories import blurb_revision, find_story, slugify
from digest.timeutil import iso
from tests.conftest import TEST_SOURCES

T0 = dt.datetime(2026, 10, 24, 9, 0, tzinfo=dt.UTC)
ZONE = ZoneInfo("Europe/Stockholm")

LINK_SOURCES = """
- id: headlines
  name: Headline Radio
  homepage: https://headlines.example/
  feeds:
    - {url: "https://headlines.example/rss", kind: rss}
  lang: en
  country: FI
  usage_policy: link_only
  display_headlines: true
  terms_checked_on: 2026-10-01
  enabled: true
- id: agency
  name: Agency Example
  homepage: https://agency.example/
  feeds:
    - {url: "https://agency.example/rss", kind: rss}
  lang: sv
  country: SE
  category_hints: [world]
  usage_policy: facts_only
  official: true
  terms_checked_on: 2026-10-01
  enabled: true
- id: weather
  name: SMHI
  homepage: https://weather.example/warnings
  feeds:
    - {url: "https://data.weather.example/warning.json", kind: data_api}
  lang: sv
  country: SE
  category_hints: [climate]
  usage_policy: open_license
  template: smhi_warning
  official: true
  terms_checked_on: 2026-10-01
  enabled: true
"""

CANARY = "CANARY-FEED-DESCRIPTION-7731"


# --------------------------------------------------------------------------- pure parts


def member(
    item_id: int,
    source: str,
    title: str,
    *,
    summary: str | None = None,
    minutes: int = 0,
    policy: str = "facts_only",
    headlines: bool = False,
    official: bool = False,
    lang: str = "en",
) -> Member:
    return Member(
        item_id=item_id,
        source_id=source,
        source_name=source.title(),
        url=f"https://{source}.example/{item_id}",
        title=title,
        feed_summary=summary,
        published_at=T0 + dt.timedelta(minutes=minutes),
        lang=lang,
        usage_policy=policy,
        display_headlines=headlines,
        official=official,
    )


def test_headline_only_from_display_headlines_sources() -> None:
    ms = [
        member(1, "news", "Riksbank cuts rate to 1.5 percent", summary=CANARY),
        member(2, "yle", "Swedish central bank lowers rate", policy="link_only", headlines=True),
    ]
    card = build_card(ms, "Economy", "en")
    assert card.label == "Yle: Swedish central bank lowers rate"
    assert card.label_kind == "headline"
    lines = {s.source_id: s for s in card.sources}
    assert lines["news"].headline is None
    assert lines["yle"].headline == "Swedish central bank lowers rate"
    assert CANARY not in json.dumps([s.as_dict() for s in card.sources]) + card.label
    assert "Swedish central bank" not in card.slug_basis  # slugs never use source headlines


def test_official_source_headline_wins_over_earlier_one() -> None:
    ms = [
        member(1, "a", "First headline", headlines=True, minutes=0),
        member(2, "b", "Official headline", headlines=True, official=True, minutes=30),
    ]
    assert build_card(ms, "World", "en").label == "B: Official headline"


def test_entity_label_without_headline_sources() -> None:
    ms = [
        member(1, "news", "Riksbank cuts policy rate", summary="The Riksbank in Stockholm acts."),
        member(2, "wire", "Rate cut by Riksbank", summary="Stockholm markets react to Riksbank."),
    ]
    card = build_card(ms, "Economy", "en")
    assert card.label_kind == "entities"
    assert card.label.startswith("Economy · Riksbank")
    assert "Stockholm" in card.label
    assert CANARY not in card.label


def test_sensitive_label_never_names_people() -> None:
    ms = [
        member(
            1,
            "news",
            "Anna Lindqvist arrested after shooting in Malmö",
            summary="Police say Anna Lindqvist was arrested in Malmö, Sweden.",
        ),
        member(2, "wire", "Shooting in Malmö: Anna Lindqvist detained"),
    ]
    card = build_card(ms, "Sweden", "en")
    assert card.flags == ["crime"]
    assert card.label_kind == "places"
    assert "Anna" not in card.label and "Lindqvist" not in card.label
    assert card.label == "Sweden · Malmö · police report"


def test_source_lines_one_per_source_and_capped_keeping_officials() -> None:
    ms = [member(i, f"s{i}", f"t{i}", minutes=i) for i in range(1, 8)]
    ms.append(member(20, "s1", "later item from s1", minutes=50))
    ms.append(member(30, "gov", "official", official=True, minutes=60))
    lines = source_lines(ms)
    assert len(lines) == 5
    assert len({line.source_id for line in lines}) == 5
    assert "gov" in {line.source_id for line in lines}
    assert [line.published_at for line in lines] == sorted(line.published_at for line in lines)


def test_keyword_flags_by_language() -> None:
    assert keyword_flags([("Tåget från Malmö är försenat", "sv")]) == []  # "rån" in "från"
    assert keyword_flags([("Rånet i Malmö utreds", "sv")]) == ["crime"]
    assert keyword_flags([("Trafikolyckan på E4", "sv")]) == ["accident"]
    assert keyword_flags([("Man sentenced after shooting", "en")]) == ["court", "crime"]
    assert keyword_flags([("Ny brann i Bergen", "nb")]) == ["accident"]
    assert keyword_flags([("Brand new phone launched", "en")]) == []


def test_place_names() -> None:
    assert place_name("Sverige") == "Sweden"
    assert place_name("sweden", "sv") == "Sverige"
    assert place_name("Skåne") == "Skåne"
    assert place_name("Malmö") == "Malmö"
    assert place_name("Anna Lindqvist") is None


def test_duplicate_gate() -> None:
    v = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    near = np.array([0.95, 0.05, 0.0], dtype=np.float32)
    far = np.array([0.0, 1.0, 0.0], dtype=np.float32)
    assert find_duplicate(v, [(7, far), (9, near)], 0.9) == 9
    assert find_duplicate(v, [(7, far)], 0.9) is None
    assert find_duplicate(None, [(9, near)], 0.9) is None


def test_slugify_and_blurb_limits() -> None:
    assert slugify("Economy · Riksbank · Göteborg") == "economy-riksbank-goteborg"
    assert slugify("···") == "story"
    assert len(slugify("word " * 40)) <= 60
    base = build_card([member(1, "news", "Riksbank cuts rate")], "Economy", "en")
    from digest.links.stage import links_revision

    rev = blurb_revision(links_revision(base), "  The Riksbank cut its rate.  ", 60)
    assert rev.kind == "owner_blurb" and rev.summary == "The Riksbank cut its rate."
    with pytest.raises(ValueError, match="at most 60"):
        blurb_revision(links_revision(base), "word " * 61, 60)


# --------------------------------------------------------------------------- the stage


@pytest.fixture
def config(ingest_config_dir: Path) -> Config:
    (ingest_config_dir / "sources.yaml").write_text(TEST_SOURCES + LINK_SOURCES, encoding="utf-8")
    return load_config(ingest_config_dir)


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    path = tmp_path / "state.sqlite"
    db.migrate(path)
    return db.make_engine(path)


def links_settings(settings: Settings, **overrides: object) -> Settings:
    return settings.model_copy(update={"summary_mode": "links", **overrides})


def add_item(
    engine: Engine,
    source: str,
    title: str,
    *,
    summary: str | None = None,
    policy: str = "facts_only",
    at: dt.datetime = T0,
) -> int:
    url = f"https://{source}.example/{hashlib.md5(title.encode()).hexdigest()}"  # noqa: S324
    with engine.begin() as conn:
        return conn.execute(
            items.insert()
            .values(
                source_id=source,
                url=url,
                url_hash=hashlib.sha256(url.encode()).digest(),
                title=title,
                feed_summary=summary,
                published_at=iso(at),
                fetched_at=iso(at),
                usage_policy=policy,
                status="clustered",
            )
            .returning(items.c.id)
        ).scalar_one()


def add_cluster(
    engine: Engine,
    item_ids: list[int],
    *,
    category: str = "world",
    selected: bool = True,
    centroid: list[float] | None = None,
) -> int:
    vec = np.array(centroid or [1.0, 0.0, 0.0], dtype=np.float32)
    with engine.begin() as conn:
        cid = conn.execute(
            clusters.insert()
            .values(
                created_at=iso(T0),
                last_item_at=iso(T0),
                category=category,
                centroid=to_blob(vec),
                item_count=len(item_ids),
                source_count=len(item_ids),
                score=1.0,
                selected_at=iso(T0) if selected else None,
            )
            .returning(clusters.c.id)
        ).scalar_one()
        for i in item_ids:
            conn.execute(cluster_items.insert().values(cluster_id=cid, item_id=i, added_at=iso(T0)))
    return cid


def revisions(engine: Engine) -> list[dict[str, object]]:
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(select(story_revisions))]  # pyright: ignore[reportPrivateUsage]


def test_every_selected_cluster_gets_a_links_card(
    engine: Engine, config: Config, settings: Settings
) -> None:
    a = add_item(engine, "news", "Earthquake hits Japan", summary=CANARY)
    b = add_item(engine, "headlines", "Quake shakes Japan", policy="link_only")
    c = add_item(engine, "news", "Unselected story")
    sel = add_cluster(engine, [a, b])
    add_cluster(engine, [c], selected=False, centroid=[0.0, 1.0, 0.0])
    stats = run_links(engine, config, links_settings(settings), T0, ZONE)
    assert stats["stories_created"] == 1 and stats["links_revisions"] == 1
    with engine.connect() as conn:
        story = conn.execute(select(stories)).one()
    assert story.cluster_id == sel and story.status == "approved"
    assert story.path == f"stories/2026-10-24/{story.slug}.yaml"
    (rev,) = revisions(engine)
    assert rev["kind"] == "links" and rev["summary"] is None
    assert rev["headline"] == "Headline Radio: Quake shakes Japan"
    assert CANARY not in json.dumps(rev)


def test_links_stage_is_idempotent_and_revises_on_new_sources(
    engine: Engine, config: Config, settings: Settings
) -> None:
    a = add_item(engine, "news", "Earthquake hits Japan")
    cid = add_cluster(engine, [a])
    s = links_settings(settings)
    run_links(engine, config, s, T0, ZONE)
    assert run_links(engine, config, s, T0, ZONE).get("links_revisions", 0) == 0
    b = add_item(engine, "agency", "Jordbävning i Japan")
    with engine.begin() as conn:
        conn.execute(cluster_items.insert().values(cluster_id=cid, item_id=b, added_at=iso(T0)))
    assert run_links(engine, config, s, T0, ZONE)["links_revisions"] == 1
    assert len(revisions(engine)) == 2


def test_frozen_stories_are_left_alone(engine: Engine, config: Config, settings: Settings) -> None:
    a = add_item(engine, "news", "Earthquake hits Japan")
    cid = add_cluster(engine, [a])
    s = links_settings(settings)
    run_links(engine, config, s, T0, ZONE)
    b = add_item(engine, "agency", "Jordbävning i Japan")
    with engine.begin() as conn:
        conn.execute(stories.update().values(status="rejected"))
        conn.execute(cluster_items.insert().values(cluster_id=cid, item_id=b, added_at=iso(T0)))
    assert run_links(engine, config, s, T0, ZONE)["frozen"] == 1
    assert len(revisions(engine)) == 1


def test_llm_mode_uses_fallback_only_when_enabled(
    engine: Engine, config: Config, settings: Settings
) -> None:
    add_cluster(engine, [add_item(engine, "news", "Earthquake hits Japan")])
    off = settings.model_copy(update={"summary_mode": "llm", "fallback_to_links": False})
    assert run_links(engine, config, off, T0, ZONE).get("stories_created", 0) == 0
    on = settings.model_copy(update={"summary_mode": "llm", "fallback_to_links": True})
    assert run_links(engine, config, on, T0, ZONE)["links_revisions"] == 1


def test_llm_stories_are_not_replaced_by_cards(
    engine: Engine, config: Config, settings: Settings
) -> None:
    cid = add_cluster(engine, [add_item(engine, "news", "Earthquake hits Japan")])
    s = settings.model_copy(update={"summary_mode": "llm"})
    run_links(engine, config, s, T0, ZONE)
    with engine.begin() as conn:
        conn.execute(story_revisions.update().values(kind="llm"))
    b = add_item(engine, "agency", "Jordbävning i Japan")
    with engine.begin() as conn:
        conn.execute(cluster_items.insert().values(cluster_id=cid, item_id=b, added_at=iso(T0)))
    run_links(engine, config, s, T0, ZONE)
    assert len(revisions(engine)) == 1


def test_template_item_makes_a_template_story_with_credit(
    engine: Engine, config: Config, settings: Settings
) -> None:
    w = add_item(
        engine,
        "weather",
        "SMHI issues yellow warning for wind in Skåne",
        summary="SMHI has issued a yellow warning for wind in Skåne.",
        policy="open_license",
    )
    n = add_item(engine, "news", "Storm expected in southern Sweden")
    add_cluster(engine, [w, n], category="climate")
    run_links(engine, config, links_settings(settings), T0, ZONE)
    (rev,) = revisions(engine)
    assert rev["kind"] == "template"
    assert rev["headline"] == "SMHI issues yellow warning for wind in Skåne"
    assert str(rev["summary"]).endswith("Source: SMHI weather warnings.")
    assert {s["source_id"] for s in json.loads(str(rev["sources"]))} == {"weather", "news"}
    off = links_settings(settings, template_stories=False)
    with engine.begin() as conn:
        conn.execute(story_revisions.delete())
        conn.execute(stories.update().values(current_rev=None))
    run_links(engine, config, off, T0, ZONE)
    assert revisions(engine)[0]["kind"] == "links"


def test_duplicate_of_a_recent_story_goes_to_review(
    engine: Engine, config: Config, settings: Settings
) -> None:
    add_cluster(engine, [add_item(engine, "news", "Earthquake hits Japan")])
    add_cluster(
        engine, [add_item(engine, "agency", "Jordbävning i Japan")], centroid=[0.99, 0.1, 0]
    )
    s = links_settings(settings)
    run_links(engine, config, s, T0, ZONE)
    with engine.connect() as conn:
        statuses = sorted(conn.execute(select(stories.c.status)).scalars())
    assert statuses == ["approved", "needs_review"]


def test_stories_follow_merged_clusters(engine: Engine, config: Config, settings: Settings) -> None:
    loser = add_cluster(engine, [add_item(engine, "news", "Earthquake hits Japan")])
    s = links_settings(settings)
    run_links(engine, config, s, T0, ZONE)
    winner = add_cluster(engine, [], selected=True, centroid=[0.0, 0.0, 1.0])
    with engine.begin() as conn:
        conn.execute(
            cluster_items.update()
            .where(cluster_items.c.cluster_id == loser)
            .values(cluster_id=winner)
        )
        conn.execute(
            clusters.update().where(clusters.c.id == loser).values(is_open=0, merged_into=winner)
        )
    stats = run_links(engine, config, s, T0, ZONE)
    assert stats["stories_moved_after_merge"] == 1
    with engine.connect() as conn:
        assert find_story(conn, winner) is not None
        assert conn.execute(select(func.count()).select_from(stories)).scalar_one() == 1


def test_entity_label_drops_repeats() -> None:
    ms = [
        member(1, "news", "Tomas Eneroth resigns", summary="Eneroth leaves; Eneroth says why."),
        member(2, "wire", "Eneroth quits in Stockholm", summary="Tomas Eneroth quits."),
    ]
    label = build_card(ms, "Sweden", "en").label
    assert label.count("Eneroth") == 1


def test_link_only_titles_give_names_but_never_wording() -> None:
    ms = [
        member(1, "radio", "Israel closes West Bank crossings before vote", policy="link_only"),
        member(2, "tv", "West Bank crossings shut by Israel on election day", policy="link_only"),
    ]
    card = build_card(ms, "World", "en")
    assert card.label_kind == "entities"
    assert card.label.startswith("World · ") and "Israel" in card.label
    assert "crossings" not in card.label.lower()
    assert all(line.headline is None for line in card.sources)
