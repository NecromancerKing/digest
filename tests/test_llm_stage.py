from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select

from digest import db
from digest.config import Config, Settings, load_config
from digest.db.schema import clusters, items, stories, story_revisions
from digest.links.stage import run_links
from digest.llm.adapters import RateLimited, Reply, Request
from digest.llm.client import LLMClient
from digest.llm.stage import run_llm
from tests.conftest import TEST_SOURCES
from tests.fake_llm import FakeProvider, grades_for, providers, story_for
from tests.test_links import CANARY, LINK_SOURCES, T0, ZONE, add_cluster, add_item

QUAKE = "Strong earthquake hits southern Japan, no tsunami warning issued by the agency"


@pytest.fixture
def config(ingest_config_dir: Path) -> Config:
    (ingest_config_dir / "sources.yaml").write_text(TEST_SOURCES + LINK_SOURCES, encoding="utf-8")
    return load_config(ingest_config_dir)


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    path = tmp_path / "state.sqlite"
    db.migrate(path)
    return db.make_engine(path)


class Budget:
    def __init__(self, seconds: float = 600) -> None:
        self.seconds = seconds

    def remaining(self) -> float:
        return self.seconds


def llm_settings(settings: Settings, **kw: Any) -> Settings:
    return settings.model_copy(update={"summary_mode": "llm", **kw})


def client_for(engine: Engine, settings: Settings, fakes: dict[str, FakeProvider]) -> LLMClient:
    return LLMClient(
        settings,
        providers(),
        engine,
        adapters=fakes,  # pyright: ignore[reportArgumentType]
        now=lambda: T0,
        sleep=lambda _s: None,
        jitter=lambda: 0.5,
    )


def fakes() -> dict[str, FakeProvider]:
    return {"alpha": FakeProvider(), "beta": FakeProvider()}


def quake_cluster(engine: Engine) -> int:
    a = add_item(engine, "news", QUAKE, summary="The agency measured magnitude 6.8 offshore.")
    b = add_item(
        engine, "agency", "Kraftig jordbävning i södra Japan", summary="Ingen tsunamivarning."
    )
    c = add_item(engine, "headlines", f"Japan quake {CANARY}", policy="link_only")
    return add_cluster(engine, [a, b, c])


def run(
    engine: Engine, config: Config, settings: Settings, f: dict[str, FakeProvider], **kw: Any
) -> dict[str, int]:
    s = llm_settings(settings)
    return run_llm(
        engine, client_for(engine, s, f), config, s, kw.pop("budget", Budget()), T0, ZONE, **kw
    )


def current(engine: Engine) -> list[Any]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(stories.c.status, story_revisions)
            .join(story_revisions, story_revisions.c.story_id == stories.c.id)
            .where(story_revisions.c.rev_no == stories.c.current_rev)
        ).all()
    return list(rows)


def test_selected_cluster_becomes_an_approved_llm_story(
    engine: Engine, config: Config, settings: Settings
) -> None:
    cid = quake_cluster(engine)
    f = fakes()
    stats = run(engine, config, settings, f)
    assert stats["extract_ok"] == 1 and stats["story_approved"] == 1
    (row,) = current(engine)
    assert row.status == "approved" and row.kind == "llm"
    assert row.headline == "Powerful quake shakes southern Japan"
    assert row.provider == "alpha" and row.model == "big" and row.prompt_version == "synth-v1"
    sources = json.loads(row.sources)
    assert {s["source_id"] for s in sources} == {"news", "agency", "headlines"}
    assert all(s["headline"] is None for s in sources)  # never source headlines in llm mode
    gates = json.loads(row.verification)
    assert gates["g1"] == "pass" and gates["g2"] == "pass" and gates["g3"]["result"] == "pass"
    assert gates["g3"]["model"] != "big"  # graded by another model
    # Rule L6: link_only text reached no provider.
    sent = [r.user for p in f.values() for r in p.requests]
    assert sent and not any(CANARY in u for u in sent)
    with engine.connect() as conn:
        statuses = dict(conn.execute(select(items.c.usage_policy, items.c.status)).all())
        ex = json.loads(
            conn.execute(select(clusters.c.extraction).where(clusters.c.id == cid)).scalar_one()
        )
    assert statuses == {"facts_only": "extracted", "link_only": "clustered"}
    assert ex["event"].startswith("A strong earthquake") and ex["model"] == "small"


def test_rerun_is_idempotent(engine: Engine, config: Config, settings: Settings) -> None:
    quake_cluster(engine)
    f = fakes()
    run(engine, config, settings, f)
    n = sum(len(p.requests) for p in f.values())
    again = run(engine, config, settings, f)
    assert again.get("llm_revisions", 0) == 0
    assert sum(len(p.requests) for p in f.values()) == n


def test_no_quota_leaves_a_links_card(engine: Engine, config: Config, settings: Settings) -> None:
    quake_cluster(engine)
    f = fakes()

    def limited(_req: Request) -> Reply:
        raise RateLimited(None)

    for p in f.values():
        p.handlers = dict.fromkeys(p.handlers, limited)
    stats = run(engine, config, settings, f)
    assert stats["extract_no_quota"] == 1 and "llm_revisions" not in stats
    run_links(engine, config, llm_settings(settings), T0, ZONE)
    (row,) = current(engine)
    assert row.kind == "links" and row.status == "approved"


def test_originality_failure_is_never_approved(
    engine: Engine, config: Config, settings: Settings
) -> None:
    quake_cluster(engine)
    f = fakes()
    copied = "Officials say: " + QUAKE + ". " + " ".join(["More"] * 30)
    for p in f.values():
        p.handlers["WriteStory"] = lambda r: story_for(r, summary=copied)
    stats = run(engine, config, settings, f)
    assert stats["story_dropped"] == 1 and stats["synthesis_attempts"] == 3
    assert current(engine) == []
    retries = [r.user for p in f.values() for r in p.requests if r.schema_name == "WriteStory"][1:]
    assert all("Do not use these phrases" in u for u in retries)


def test_unsupported_claims_go_to_review(
    engine: Engine, config: Config, settings: Settings
) -> None:
    quake_cluster(engine)
    f = fakes()
    for p in f.values():
        p.handlers["GradeClaims"] = lambda r: grades_for(r, "unsupported")
    stats = run(engine, config, settings, f)
    assert stats["story_review"] == 1
    (row,) = current(engine)
    assert row.status == "needs_review" and row.kind == "llm"


def test_private_name_in_crime_story_is_dropped(
    engine: Engine, config: Config, settings: Settings
) -> None:
    from tests.fake_llm import facts_for

    quake_cluster(engine)
    f = fakes()
    for p in f.values():
        p.handlers["RecordFacts"] = lambda r: facts_for(r, crime=True)
        p.handlers["WriteStory"] = lambda r: story_for(r, names_private_individual=True)
    stats = run(engine, config, settings, f)
    assert stats["story_dropped"] == 1 and current(engine) == []


def test_time_budget_stops_llm_work(engine: Engine, config: Config, settings: Settings) -> None:
    quake_cluster(engine)
    f = fakes()
    stats = run(engine, config, settings, f, budget=Budget(10))
    assert stats["skipped_for_budget"] == 1
    assert not any(p.requests for p in f.values())


@pytest.mark.parametrize("mode", ["tfidf", "embeddings"])
def test_cross_language_check_runs_only_with_embeddings(
    engine: Engine, config: Config, settings: Settings, mode: str
) -> None:
    import numpy as np

    quake_cluster(engine)

    def embed(texts: Any) -> Any:
        return np.eye(len(texts), 8, dtype=np.float32)

    run(engine, config, settings, fakes(), embed=embed if mode == "embeddings" else None)
    (row,) = current(engine)
    expected = "checked" if mode == "embeddings" else "skipped"
    assert json.loads(row.verification)["g2_cross_lang"] == expected
