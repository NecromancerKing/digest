from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from sqlalchemy import Engine

from digest import db
from digest.config import Config, Settings, load_config
from digest.evals import (
    Cache,
    EvalOptions,
    Evaluator,
    GoldCluster,
    load_golden,
    mentions,
    regressions,
    render,
    score_grades,
)
from digest.llm.adapters import RateLimited, Reply, Request
from digest.llm.client import LLMClient
from digest.llm.schemas import GradeClaims
from tests.fake_llm import FakeProvider, providers, reply
from tests.test_llm_client import Clock

GOLDEN = Path(__file__).resolve().parents[1] / "evals" / "golden"

CLUSTER = {
    "id": "x01-quake",
    "category": "world",
    "docs": [
        {"id": 1, "source": "Wire", "lang": "en", "title": "Quake strikes Japan coast",
         "summary": "A magnitude 6.8 earthquake struck southern Japan."},
        {"id": 2, "source": "Radio", "lang": "sv", "title": "Jordskalv i Japan",
         "summary": "Ingen tsunamirisk enligt myndigheterna."},
    ],
    "facts": [
        {"id": "1:f1", "statement": "A magnitude 6.8 earthquake struck southern Japan.",
         "attribution": "agency"},
        {"id": "2:f1", "statement": "There is no tsunami risk.", "attribution": "authorities"},
    ],
    "must_contain": ["6.8", "Japan"],
    "must_not": ["killed"],
    "claims": [
        {"text": "A 6.8 quake hit Japan.", "fact_ids": ["1:f1"], "label": "supported"},
        {"text": "The quake killed ten people.", "fact_ids": ["1:f1"], "label": "unsupported"},
    ],
}  # fmt: skip


def test_shipped_golden_set_is_valid_and_complete() -> None:
    clusters = load_golden(GOLDEN)
    assert len(clusters) == 50
    assert {c.category for c in clusters} == {
        "world", "sweden", "economy", "tech_science", "climate", "sport",
    }  # fmt: skip
    assert sum(cl.label == "unsupported" for c in clusters for cl in c.claims) >= 50


def test_golden_set_rejects_unknown_fact_ids(tmp_path: Path) -> None:
    bad = {**CLUSTER, "claims": [{"text": "x", "fact_ids": ["9:f9"], "label": "supported"}] * 2}
    (tmp_path / "a.yaml").write_text(yaml.safe_dump([bad]), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown ids"):
        load_golden(tmp_path)


def test_mentions_tolerates_number_and_dash_formats() -> None:
    assert mentions("About 2000 people left", "2,000")
    assert mentions("Hammarby won 2" + chr(0x2013) + "0", "2-0")  # en dash
    assert not mentions("magnitude 6.4", "64")


def test_grader_scoring() -> None:
    c = GoldCluster.model_validate(CLUSTER)
    caught = GradeClaims.model_validate(
        {"results": [{"claim_index": 0, "verdict": "supported", "reason": ""},
                     {"claim_index": 1, "verdict": "unsupported", "reason": ""}]}
    )  # fmt: skip
    assert score_grades(c, caught) == {"catch_unsupported": 1.0, "accept_supported": 1.0}


def test_regressions_over_two_points() -> None:
    old = {"results": {"synthesize": {"m": {"coverage": 90.0, "tokens": 100.0}}}}
    new = {"results": {"synthesize": {"m": {"coverage": 87.5, "tokens": 300.0}}}}
    assert regressions(new, old) == ["synthesize m coverage: 90.0 -> 87.5"]
    assert regressions(new, None) == []


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    path = tmp_path / "eval.sqlite"
    db.migrate(path)
    return db.make_engine(path)


@pytest.fixture
def config(config_dir: Path) -> Config:
    return load_config(config_dir)


def evaluator(
    engine: Engine, config: Config, fakes: dict[str, FakeProvider], cache: Cache
) -> Evaluator:
    clock = Clock()
    client = LLMClient(
        Settings(_env_file=None),  # pyright: ignore[reportCallIssue]
        providers(),
        engine,
        adapters=fakes,  # pyright: ignore[reportArgumentType]
        now=clock.now,
        sleep=clock.sleep,
        eval_mode=True,
    )
    return Evaluator(client, config, client.settings, cache)


def test_run_scores_every_task_and_resumes_from_cache(
    engine: Engine, config: Config, tmp_path: Path
) -> None:
    fakes = {"alpha": FakeProvider(), "beta": FakeProvider()}
    for f in fakes.values():
        f.handlers["GradeClaims"] = lambda r: reply(
            {"results": [{"claim_index": 0, "verdict": "supported", "reason": ""},
                         {"claim_index": 1, "verdict": "unsupported", "reason": ""}]}
        )  # fmt: skip
    cache = Cache(tmp_path / "answers.jsonl")
    clusters = [GoldCluster.model_validate(CLUSTER)]
    result = evaluator(engine, config, fakes, cache).run(clusters, EvalOptions())
    res = result["results"]
    assert set(res) == {"extract", "synthesize", "verify"}
    assert res["extract"]["alpha/small"]["valid"] == 100.0
    assert res["verify"]["alpha/big"]["catch_unsupported"] == 100.0
    synth = res["synthesize"]["alpha/big"]
    assert synth["valid"] == 100.0 and "tokens" in synth
    assert synth["grounded"] == 0.0  # the fake grader rejects the story's second claim
    assert result["recommendations"]["quality_order"][0] in ("alpha/big", "beta/mid")
    sent = sum(len(f.requests) for f in fakes.values())
    # A second run answers everything from the cache: no new calls.
    again = evaluator(engine, config, fakes, Cache(tmp_path / "answers.jsonl")).run(
        clusters, EvalOptions()
    )
    assert sum(len(f.requests) for f in fakes.values()) == sent
    assert again["results"] == result["results"]
    assert "quality order" in render(again, [])


def test_out_of_quota_model_is_paused_and_reported_missing(
    engine: Engine, config: Config, tmp_path: Path
) -> None:
    fakes = {"alpha": FakeProvider(), "beta": FakeProvider()}

    def limited(_r: Request) -> Reply:
        raise RateLimited(None)

    fakes["beta"].handlers = dict.fromkeys(fakes["beta"].handlers, limited)
    clusters = [
        GoldCluster.model_validate(CLUSTER),
        GoldCluster.model_validate({**CLUSTER, "id": "x02"}),
    ]
    ev = evaluator(engine, config, fakes, Cache(tmp_path / "a.jsonl"))
    result = ev.run(clusters, EvalOptions(tasks=("synthesize",)))
    assert result["results"]["synthesize"]["beta/mid"]["missing"] == 2
    assert "beta/mid" in ev.paused
    assert len(fakes["beta"].requests) == 1  # paused after the first refusal
