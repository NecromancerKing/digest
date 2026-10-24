from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest
from numpy.typing import NDArray
from sqlalchemy import Engine, func, select

from digest import db
from digest.config import Config, load_config
from digest.db.schema import cluster_items, clusters, feeds, items, runs
from digest.housekeeping import prune
from digest.nlp.stages import (
    local_day_bounds,
    run_cluster,
    run_embed,
    run_maintenance,
    run_rank,
)
from digest.timeutil import iso
from tests.conftest import TEST_SOURCES
from tests.fake_embed import FakeEmbedder

T0 = dt.datetime(2026, 10, 24, 9, 0, tzinfo=dt.UTC)
ZONE = ZoneInfo("Europe/Stockholm")

EXTRA_SOURCES = """
- id: wire
  name: Wire Example
  homepage: https://wire.example/
  feeds:
    - {url: "https://wire.example/rss", kind: rss}
    - {url: "https://wire.example/sport.rss", kind: rss, category: sport}
  lang: en
  country: GB
  category_hints: [world]
  usage_policy: facts_only
  publisher: news-group
  terms_checked_on: 2026-10-01
  enabled: true
- id: wire2
  name: Wire Two
  homepage: https://wire2.example/
  feeds:
    - {url: "https://wire2.example/rss", kind: rss}
  lang: en
  country: GB
  category_hints: [world]
  usage_policy: facts_only
  publisher: news-group
  terms_checked_on: 2026-10-01
  enabled: true
- id: agency
  name: Agency Example
  homepage: https://agency.example/
  feeds:
    - {url: "https://agency.example/rss", kind: rss}
  lang: sv
  country: SE
  category_hints: [sweden]
  usage_policy: facts_only
  official: true
  terms_checked_on: 2026-10-01
  enabled: true
"""


class Budget:
    def remaining(self) -> float:
        return 600.0


@pytest.fixture
def config(ingest_config_dir: Path) -> Config:
    (ingest_config_dir / "sources.yaml").write_text(TEST_SOURCES + EXTRA_SOURCES, encoding="utf-8")
    return load_config(ingest_config_dir)


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    path = tmp_path / "state.sqlite"
    db.migrate(path)
    return db.make_engine(path)


def add_item(
    engine: Engine,
    source: str,
    title: str,
    *,
    summary: str = "",
    at: dt.datetime = T0,
    feed: str | None = None,
    policy: str = "facts_only",
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
                feed_summary=summary or None,
                published_at=iso(at),
                fetched_at=iso(at),
                usage_policy=policy,
                feed_url=None,
            )
            .returning(items.c.id)
        ).scalar_one()


def group(engine: Engine, config: Config, now: dt.datetime = T0) -> dict[str, int]:
    out = run_embed(engine, FakeEmbedder(), config, Budget())
    out |= run_cluster(engine, config, now)
    return out


def membership(engine: Engine) -> dict[int, int]:
    with engine.connect() as conn:
        return dict(conn.execute(select(cluster_items.c.item_id, cluster_items.c.cluster_id)).all())


QUAKE = "strong earthquake hits southern japan coast"


def test_embed_skips_link_only_and_records_entities(engine: Engine, config: Config) -> None:
    a = add_item(engine, "news", "Flood warning issued in Skåne", summary="SMHI warns of rain.")
    r = add_item(engine, "radio", "Radio item", policy="link_only")
    embedder = FakeEmbedder()
    stats = run_embed(engine, embedder, config, Budget())
    assert stats["items_embedded"] == 1
    assert all("Radio" not in t for call in embedder.calls for t in call)
    with engine.connect() as conn:
        rows = {row.id: row for row in conn.execute(select(items))}
    assert rows[a].status == "embedded" and json.loads(rows[a].entities) == ["skåne", "smhi"]
    assert rows[r].status == "new" and rows[r].embedding is None


def test_same_story_clusters_together_and_counts_independent_sources(
    engine: Engine, config: Config
) -> None:
    a = add_item(engine, "news", QUAKE)
    b = add_item(engine, "wire", QUAKE + " today")
    c = add_item(engine, "wire2", QUAKE + " today")  # same publisher as wire: a near copy
    d = add_item(engine, "agency", "riksbank cuts the policy rate")
    stats = group(engine, config)
    m = membership(engine)
    assert m[a] == m[b] == m[c] != m[d]
    assert stats["new_clusters"] == 2 and stats["items_joined"] == 2
    with engine.connect() as conn:
        quake = conn.execute(select(clusters).where(clusters.c.id == m[a])).one()
    assert quake.item_count == 3 and quake.source_count == 2  # news + news-group


def test_clustering_is_idempotent(engine: Engine, config: Config) -> None:
    add_item(engine, "news", QUAKE)
    add_item(engine, "wire", QUAKE)
    group(engine, config)
    again = group(engine, config)
    assert again.get("items_joined", 0) == 0 and again.get("new_clusters", 0) == 0
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(cluster_items)).scalar_one() == 2
        assert conn.execute(select(func.count()).select_from(clusters)).scalar_one() == 1


def test_later_items_join_existing_clusters(engine: Engine, config: Config) -> None:
    a = add_item(engine, "news", QUAKE)
    group(engine, config)
    b = add_item(engine, "wire", QUAKE + " update", at=T0 + dt.timedelta(hours=3))
    group(engine, config, T0 + dt.timedelta(hours=3))
    m = membership(engine)
    assert m[a] == m[b]


def test_link_only_items_attach_by_title_or_are_skipped_later(
    engine: Engine, config: Config
) -> None:
    add_item(engine, "news", "Strong earthquake hits southern Japan")
    match = add_item(engine, "radio", "Strong earthquake hits Japan", policy="link_only")
    orphan = add_item(engine, "radio", "Local concert cancelled", policy="link_only")
    stats = group(engine, config)
    assert stats["link_only_attached"] == 1 and "link_only_skipped" not in stats
    assert match in membership(engine) and orphan not in membership(engine)
    later = group(engine, config, T0 + dt.timedelta(hours=25))
    assert later["link_only_skipped"] == 1
    with engine.connect() as conn:
        status = conn.execute(select(items.c.status).where(items.c.id == orphan)).scalar_one()
        radio = conn.execute(select(items).where(items.c.id == match)).one()
    assert status == "skipped" and radio.embedding is None and radio.facts is None


def test_maintenance_merges_twins_and_closes_idle_clusters(engine: Engine, config: Config) -> None:
    a = add_item(engine, "news", QUAKE, summary="Tokyo says Japan is ready.")
    old = add_item(
        engine, "agency", "riksbank cuts the policy rate", at=T0 - dt.timedelta(hours=40)
    )
    group(engine, config)
    b = add_item(engine, "wire", QUAKE + " again")
    first = membership(engine)[a]
    with engine.begin() as conn:  # a second cluster for the same story, as if split earlier
        twin = conn.execute(select(clusters).where(clusters.c.id == first)).one()
        new_id = conn.execute(
            clusters.insert()
            .values(**{k: v for k, v in twin._mapping.items() if k != "id"})
            .returning(clusters.c.id)
        ).scalar_one()
        conn.execute(items.update().where(items.c.id == b).values(status="clustered"))
        conn.execute(cluster_items.insert().values(cluster_id=new_id, item_id=b, added_at=iso(T0)))
    stats = run_maintenance(engine, config, T0)
    assert stats == {"clusters_merged": 1, "clusters_closed": 1}
    m = membership(engine)
    assert m[a] == m[b]
    with engine.connect() as conn:
        closed = conn.execute(select(clusters).where(clusters.c.id == m[old])).one()
        emb = conn.execute(select(items.c.embedding).where(items.c.id == old)).scalar_one()
    assert closed.is_open == 0 and closed.centroid is None and emb is None


def test_rank_selects_within_daily_caps_and_keeps_selections(
    engine: Engine, config: Config, ingest_config_dir: Path
) -> None:
    cats = (ingest_config_dir / "categories.yaml").read_text(encoding="utf-8")
    (ingest_config_dir / "categories.yaml").write_text(
        cats.replace("daily_cap: 12", "daily_cap: 1"), encoding="utf-8"
    )
    config = load_config(ingest_config_dir)
    add_item(engine, "news", QUAKE)
    add_item(engine, "wire", QUAKE)
    add_item(engine, "news", "flood warning across europe rivers")
    add_item(engine, "wire", "flood warning across europe rivers")
    add_item(engine, "agency", "riksbank cuts the policy rate")  # official: one source is enough
    add_item(engine, "news", "single source story nobody else has")
    group(engine, config)
    stats = run_rank(engine, config, ZONE, T0)
    assert stats["selected_new"] == 2  # one world (cap 1) + one sweden
    assert stats["eligible"] == 3
    again = run_rank(engine, config, ZONE, T0 + dt.timedelta(minutes=30))
    assert again["selected_new"] == 0 and again["selected_today"] == 2
    tomorrow = run_rank(engine, config, ZONE, T0 + dt.timedelta(days=1))
    assert tomorrow["selected_new"] == 1  # the second world story, under a new day's cap


def test_feed_category_overrides_source_hint(engine: Engine, config: Config) -> None:
    with engine.begin() as conn:
        conn.execute(
            feeds.insert().values(
                url="https://wire.example/sport.rss", source_id="wire", kind="rss"
            )
        )
        sport = "https://wire.example/sport.rss"
        for n, (sid, url) in enumerate((("wire", sport), ("wire", sport), ("news", None))):
            conn.execute(
                items.insert().values(
                    source_id=sid,
                    url=f"https://{sid}.example/match{n}",
                    url_hash=hashlib.sha256(f"{sid}{n}".encode()).digest(),
                    title="cup final ends in penalty shootout",
                    published_at=iso(T0),
                    fetched_at=iso(T0),
                    usage_policy="facts_only",
                    feed_url=url,
                )
            )
    group(engine, config)
    run_rank(engine, config, ZONE, T0)
    with engine.connect() as conn:
        cat = conn.execute(select(clusters.c.category)).scalar_one()
    assert cat == "sport"  # two items from the sport feed outvote one world item


def test_day_bounds_follow_stockholm_time() -> None:
    late = dt.datetime(2026, 10, 24, 22, 30, tzinfo=dt.UTC)  # 00:30 on the 25th in Stockholm
    # Summer time ends on 25 Oct 2026, so that day is 25 hours long.
    assert local_day_bounds(late, ZONE) == ("2026-10-24T22:00:00Z", "2026-10-25T23:00:00Z")


def test_prune_deletes_old_items_and_runs(engine: Engine, config: Config) -> None:
    old = add_item(engine, "news", QUAKE, at=T0 - dt.timedelta(days=40))
    fresh = add_item(engine, "news", "fresh unclustered item")
    with engine.begin() as conn:
        conn.execute(runs.insert().values(started_at=iso(T0 - dt.timedelta(days=100))))
        conn.execute(runs.insert().values(started_at=iso(T0)))
    stats = prune(engine, T0, item_days=30, runs_days=90, llm_calls_days=90)
    assert stats == {"items_deleted": 1, "llm_calls_deleted": 0, "runs_deleted": 1}
    with engine.connect() as conn:
        left = {r.id for r in conn.execute(select(items.c.id))}
    assert left == {fresh} and old not in left


# --------------------------------------------------------------------------- D14 event merge


def _extract(engine: Engine, cluster_id: int, event: str, flags: list[str]) -> None:
    with engine.begin() as conn:
        conn.execute(
            clusters.update()
            .where(clusters.c.id == cluster_id)
            .values(extraction=json.dumps({"event": event, "sensitivity": flags}))
        )


def test_event_merge_joins_clusters_with_matching_event_lines(
    engine: Engine, config: Config
) -> None:
    from digest.nlp.stages import run_event_merge
    from tests.fake_embed import text_vector

    sv = add_item(engine, "agency", "Kraftigt jordskalv i södra Japan")
    en = add_item(engine, "news", QUAKE)
    other = add_item(engine, "wire", "riksbank cuts the policy rate")
    group(engine, config)
    m = membership(engine)
    assert m[sv] != m[en]  # titles in two languages: not clustered together
    _extract(engine, m[sv], "A strong earthquake struck southern Japan.", ["death"])
    _extract(engine, m[en], "A strong earthquake struck southern Japan.", [])
    _extract(engine, m[other], "The central bank lowered its policy rate.", [])

    def embed(texts: Sequence[str]) -> NDArray[np.float32]:
        return np.vstack([text_vector(t) for t in texts])

    stats = run_event_merge(engine, config, embed, T0)
    assert stats["clusters_merged"] == 1
    after = membership(engine)
    assert after[sv] == after[en] != after[other]
    with engine.connect() as conn:
        winner = conn.execute(select(clusters).where(clusters.c.id == after[sv])).one()
        loser_id = m[en] if after[sv] == m[sv] else m[sv]
        loser = conn.execute(select(clusters).where(clusters.c.id == loser_id)).one()
    assert json.loads(winner.extraction)["sensitivity"] == ["death"]
    assert loser.is_open == 0 and loser.merged_into == winner.id
    assert run_event_merge(engine, config, embed, T0).get("clusters_merged", 0) == 0


def test_event_merge_respects_the_time_window(engine: Engine, config: Config) -> None:
    from digest.nlp.stages import run_event_merge
    from tests.fake_embed import text_vector

    a = add_item(engine, "agency", "Kraftigt jordskalv i södra Japan")
    b = add_item(engine, "news", QUAKE, at=T0 + dt.timedelta(hours=60))
    group(engine, config, T0 + dt.timedelta(hours=60))
    m = membership(engine)
    for item in (a, b):
        _extract(engine, m[item], "A strong earthquake struck southern Japan.", [])

    def embed(texts: Sequence[str]) -> NDArray[np.float32]:
        return np.vstack([text_vector(t) for t in texts])

    assert run_event_merge(engine, config, embed, T0).get("clusters_merged", 0) == 0
