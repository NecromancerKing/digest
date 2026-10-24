from __future__ import annotations

import datetime as dt
import random
from pathlib import Path

import pytest
from typer.testing import CliRunner

from digest.cli import app
from digest.nlp.cluster import Params
from digest.nlp.label import (
    Label,
    LabelItem,
    Pair,
    append_label,
    default_grid,
    evaluate,
    read_labels,
    recommend,
    sample_pairs,
)
from tests.fake_embed import FakeEmbedder, text_vector

T0 = dt.datetime(2026, 10, 24, 9, 0, tzinfo=dt.UTC)

STORIES = [
    "strong earthquake hits southern japan coast",
    "riksbank cuts the policy rate to two percent",
    "flood warning issued for rivers in skane",
]


def pool() -> list[LabelItem]:
    out: list[LabelItem] = []
    for s, story in enumerate(STORIES):
        for k, extra in enumerate(["", " today", " officials say"]):
            text = story + extra
            out.append(
                LabelItem(
                    id=len(out),
                    key=f"{s}{k}".ljust(8, "0"),
                    source_id=f"src{k}",
                    title=text,
                    summary=None,
                    at=T0 + dt.timedelta(minutes=10 * len(out)),
                    cluster_id=s if k < 2 else None,
                    embedding=text_vector(text),
                    entities=[],
                )
            )
    return out


def labels_for(items: list[LabelItem]) -> list[Label]:
    out: list[Label] = []
    for i, a in enumerate(items):
        for b in items[i + 1 :]:
            out.append(Label(a.key, b.key, a.key[0] == b.key[0]))
    return out


def test_sampling_mixes_strata_and_skips_done_pairs() -> None:
    items = pool()
    first = sample_pairs(items, 10, [], random.Random(1))
    assert {p.stratum for p in first} >= {"same_cluster", "random"}
    assert len({p.key for p in first}) == len(first)
    again = sample_pairs(items, 10, [p.key for p in first], random.Random(1))
    assert not {p.key for p in again} & {p.key for p in first}


def test_labels_round_trip(tmp_path: Path) -> None:
    items = pool()
    path = tmp_path / "labels" / "pairs.jsonl"
    append_label(path, Pair(items[1], items[0], "random"), True, T0)
    [lab] = read_labels(path)
    assert (lab.a, lab.b, lab.same, lab.stratum) == (items[0].key, items[1].key, True, "random")
    assert items[0].title not in path.read_text(encoding="utf-8")  # hashes only


def test_evaluate_and_recommend() -> None:
    items = pool()
    labels = labels_for(items)
    strict, loose = Params(join_threshold=0.99), Params(join_threshold=0.6)
    scores, missing = evaluate(items, labels, FakeEmbedder().embed, [strict, loose])
    assert missing == 0
    assert scores[0].recall == 0.0
    assert scores[1].precision == 1.0 and scores[1].recall == 1.0
    best = recommend(scores)
    assert best is not None and best.params.join_threshold == 0.6


def test_labels_for_vanished_items_are_reported() -> None:
    items = pool()
    _, missing = evaluate(
        items, [Label("gone", items[0].key, True)], FakeEmbedder().embed, [Params()]
    )
    assert missing == 1


def test_grid_covers_thresholds_and_weights() -> None:
    grid = default_grid(Params())
    assert len(grid) == 272
    assert {p.cosine_weight for p in grid} == {0.6, 0.75, 0.9, 1.0}
    assert all(abs(p.cosine_weight + p.entity_weight - 1) < 1e-9 for p in grid)


@pytest.mark.parametrize("command", ["clusters", "label", "cluster-eval"])
def test_local_only_commands_refuse_on_github(
    command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    result = CliRunner().invoke(app, [command])
    assert result.exit_code == 2
    assert "local only" in result.output
