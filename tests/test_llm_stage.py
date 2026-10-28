from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select

from digest import db
from digest.config import Config, Settings, load_config
from digest.db.schema import clusters, items, stories, story_revisions
from digest.links.stage import run_links
from digest.llm.adapters import RateLimited, Reply, Request, Usage
from digest.llm.client import LLMClient
from digest.llm.stage import Target, run_llm, take_turns
from tests.conftest import TEST_SOURCES
from tests.fake_llm import FakeProvider, grades_for, providers, story_for
from tests.test_links import CANARY, LINK_SOURCES, T0, ZONE, add_cluster, add_item

QUAKE = "Strong earthquake hits southern Japan, no tsunami warning issued by the agency"


@pytest.fixture
def config(ingest_config_dir: Path) -> Config:
    (ingest_config_dir / "sources.yaml").write_text(TEST_SOURCES + LINK_SOURCES, encoding="utf-8")
    # The gate tests run with every gate enforced; the shipped config enforces none.
    path = ingest_config_dir / "settings.yaml"
    text = path.read_text(encoding="utf-8")
    assert "  enforce: []" in text
    path.write_text(text.replace("  enforce: []", "  enforce: [g1, g2, g3]"), encoding="utf-8")
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
    assert row.provider == "alpha" and row.model == "big" and row.prompt_version == "synth-v3"
    assert json.loads(row.key_facts) == ["Officials saw no tsunami risk"]
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


def test_no_quota_leaves_no_story(engine: Engine, config: Config, settings: Settings) -> None:
    quake_cluster(engine)
    f = fakes()

    def limited(_req: Request) -> Reply:
        raise RateLimited(None)

    for p in f.values():
        p.handlers = dict.fromkeys(p.handlers, limited)
    stats = run(engine, config, settings, f)
    assert stats["extract_no_quota"] == 1 and "llm_revisions" not in stats
    run_links(engine, config, llm_settings(settings), T0, ZONE)
    assert current(engine) == []  # no links card in llm mode (D28)
    run_links(engine, config, llm_settings(settings, fallback_to_links=True), T0, ZONE)
    (row,) = current(engine)
    assert row.kind == "links" and row.status == "approved"


def test_originality_failure_is_never_approved(
    engine: Engine, config: Config, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="digest")
    quake_cluster(engine)
    f = fakes()
    copied = "Officials say: " + QUAKE + ". " + " ".join(["More"] * 60)
    for p in f.values():
        p.handlers["WriteStory"] = lambda r: story_for(r, summary=copied)
    stats = run(engine, config, settings, f)
    tries = 1 + config.settings.synthesis.max_retries
    assert stats["story_dropped"] == 1 and stats["synthesis_attempts"] == tries
    assert current(engine) == []
    (dropped,) = [r for r in caplog.records if r.msg == "story_dropped"]
    assert (dropped.gate, dropped.reason, dropped.attempts) == ("g2", "shared_run", tries)  # pyright: ignore[reportAttributeAccessIssue]
    retries = [r.user for p in f.values() for r in p.requests if r.schema_name == "WriteStory"][1:]
    assert all("Do not use these phrases" in u for u in retries)


def test_gates_not_enforced_only_record_and_skip_the_grader(
    engine: Engine, config: Config, settings: Settings
) -> None:
    config = config.model_copy(
        update={
            "settings": config.settings.model_copy(
                update={"gates": config.settings.gates.model_copy(update={"enforce": []})}
            )
        }
    )
    quake_cluster(engine)
    f = fakes()
    copied = "Officials say (1:f1): " + QUAKE + ". " + " ".join(["More"] * 200)  # G1 + G2
    for p in f.values():
        p.handlers["WriteStory"] = lambda r: story_for(r, summary=copied)
    stats = run(engine, config, settings, f)
    assert stats["story_approved"] == 1 and stats["synthesis_attempts"] == 1
    (row,) = current(engine)
    assert "1:f1" not in row.summary and "Officials say: " in row.summary
    gates = json.loads(row.verification)
    assert gates["g1"] == "fail (off)" and gates["g2"].endswith("(off)") and gates["g3"] == "off"
    assert not any(r.schema_name == "GradeClaims" for p in f.values() for r in p.requests)


def test_dropped_cluster_waits_for_new_facts(
    engine: Engine, config: Config, settings: Settings
) -> None:
    cid = quake_cluster(engine)
    f = fakes()
    for p in f.values():
        p.handlers["WriteStory"] = lambda r: story_for(r, summary="too short")
    assert run(engine, config, settings, f)["story_dropped"] == 1

    def writes() -> int:
        return sum(r.schema_name == "WriteStory" for p in f.values() for r in p.requests)

    before = writes()
    assert "story_dropped" not in run(engine, config, settings, f)
    assert writes() == before  # same facts, same prompt: not tried again
    with engine.begin() as conn:  # as if the facts had changed
        conn.execute(clusters.update().where(clusters.c.id == cid).values(dropped_on="old"))
    assert run(engine, config, settings, f)["story_dropped"] == 1


def test_unsupported_claims_are_never_published(
    engine: Engine, config: Config, settings: Settings
) -> None:
    quake_cluster(engine)
    f = fakes()
    for p in f.values():
        p.handlers["GradeClaims"] = lambda r: grades_for(r, "unsupported")
    stats = run(engine, config, settings, f)
    tries = 1 + config.settings.synthesis.max_retries
    assert stats["story_dropped"] == 1 and stats["synthesis_attempts"] == tries
    run_links(engine, config, llm_settings(settings), T0, ZONE)
    assert current(engine) == []


def test_no_grader_waits_for_quota(engine: Engine, config: Config, settings: Settings) -> None:
    quake_cluster(engine)
    f = fakes()

    def limited(_req: Request) -> Reply:
        raise RateLimited(None)

    for p in f.values():
        p.handlers["GradeClaims"] = limited
    stats = run(engine, config, settings, f)
    assert stats["story_no_grader"] == 1 and current(engine) == []
    with engine.connect() as conn:  # not marked dropped: tried again next run
        assert conn.execute(select(clusters.c.dropped_on)).scalar_one() is None


def test_sensitive_cluster_is_summarised(
    engine: Engine, config: Config, settings: Settings
) -> None:
    from tests.fake_llm import facts_for

    quake_cluster(engine)
    f = fakes()
    for p in f.values():
        p.handlers["RecordFacts"] = lambda r: facts_for(r, crime=True)
    run(engine, config, settings, f)
    (row,) = current(engine)
    assert row.kind == "llm" and row.status == "approved"


def test_story_that_becomes_sensitive_keeps_its_summary(
    engine: Engine, config: Config, settings: Settings
) -> None:
    cid = quake_cluster(engine)
    run(engine, config, settings, fakes())
    assert current(engine)[0].kind == "llm"
    with engine.begin() as conn:  # a new report flags it
        conn.execute(
            clusters.update()
            .where(clusters.c.id == cid)
            .values(extraction=json.dumps({"sensitivity": ["court"]}))
        )
    run_links(engine, config, llm_settings(settings), T0, ZONE)
    (row,) = current(engine)
    assert row.kind == "llm"


def test_time_budget_stops_llm_work(engine: Engine, config: Config, settings: Settings) -> None:
    quake_cluster(engine)
    f = fakes()
    stats = run(engine, config, settings, f, budget=Budget(10))
    assert stats["skipped_for_budget"] == 1
    assert not any(p.requests for p in f.values())


def test_time_budget_stops_a_big_cluster_between_calls(
    engine: Engine, config: Config, settings: Settings
) -> None:
    ids = [add_item(engine, "news", f"{QUAKE} {n}") for n in range(9)]  # three calls' worth
    add_cluster(engine, ids)
    f = fakes()

    left = [600.0, 600.0]  # the cluster check, then the first call; then 10 s

    class Draining(Budget):
        def remaining(self) -> float:
            return left.pop(0) if left else 10.0

    stats = run(engine, config, settings, f, budget=Draining())
    assert stats["extract_no_budget"] == 1 and stats["skipped_for_budget"] == 1
    extract = [r for p in f.values() for r in p.requests if r.schema_name == "RecordFacts"]
    assert len(extract) == 1 and not any(
        r.schema_name == "WriteStory" for p in f.values() for r in p.requests
    )


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


def test_article_text_is_fetched_just_before_extraction(
    engine: Engine, config: Config, settings: Settings
) -> None:
    quake_cluster(engine)
    f = fakes()
    asked: list[list[int]] = []

    def articles(item_ids: Sequence[int]) -> Counter[str]:
        asked.append(list(item_ids))
        with engine.begin() as conn:
            conn.execute(
                items.update()
                .where(items.c.id.in_(list(item_ids)), items.c.usage_policy != "link_only")
                .values(full_text="Body text ARTICLEMARK of the report.")
            )
        return Counter({"stored": 2})

    stats = run(engine, config, settings, f, articles=articles)
    assert len(asked) == 1 and stats["articles_stored"] == 2
    extract = [r.user for p in f.values() for r in p.requests if r.schema_name == "RecordFacts"]
    assert extract and "ARTICLEMARK" in extract[0]


def test_cut_off_extraction_is_retried_in_halves(
    engine: Engine, config: Config, settings: Settings
) -> None:
    from tests.fake_llm import facts_for, item_ids

    quake_cluster(engine)
    f = fakes()

    def one_at_a_time(r: Request) -> Reply:
        if len(item_ids(r)) > 1:
            return Reply('{"event": "cut', Usage(10, None, 2800), truncated=True)
        return facts_for(r)

    for p in f.values():
        p.handlers["RecordFacts"] = one_at_a_time
    stats = run(engine, config, settings, f)
    assert stats["extract_ok"] == 1 and stats["story_approved"] == 1
    with engine.connect() as conn:
        failed = conn.execute(select(items.c.id).where(items.c.status == "failed")).all()
    assert failed == []


def test_big_cluster_keeps_its_newest_facts_under_the_input_cap(
    engine: Engine, config: Config, settings: Settings
) -> None:
    import datetime as dt

    from tests.fake_llm import fact_ids

    ids = [
        add_item(engine, "news", f"{QUAKE} {n}", at=T0 - dt.timedelta(minutes=n)) for n in range(80)
    ]
    add_cluster(engine, ids)
    f = fakes()
    stats = run(engine, config, settings, f)  # 160 facts: far over 5000 tokens
    assert stats["story_approved"] == 1
    (synth,) = [r for p in f.values() for r in p.requests if r.schema_name == "WriteStory"]
    sent = {int(i.split(":")[0]) for i in fact_ids(synth)}
    assert ids[0] in sent and ids[-1] not in sent  # the oldest went first


def test_categories_take_turns_for_the_quota() -> None:
    def t(id: int, category: str, score: float) -> Target:
        return Target(id=id, category=category, score=score, centroid=None, extraction=None)

    order = take_turns(
        [
            t(1, "world", 4.0),
            t(2, "world", 3.9),
            t(3, "world", 3.8),
            t(4, "sport", 1.0),
            t(5, "economy", 2.0),
            t(6, "economy", 1.5),
        ]
    )
    assert [x.id for x in order] == [1, 5, 4, 2, 6, 3]
