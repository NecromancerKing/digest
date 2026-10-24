from __future__ import annotations

import datetime as dt
import random
from dataclasses import replace
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
    append_review,
    default_grid,
    evaluate,
    read_labels,
    read_reviews,
    recommend,
    review_sample,
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


def test_owner_review_overrides_a_label_and_is_not_asked_twice(tmp_path: Path) -> None:
    items = pool()
    path = tmp_path / "labels" / "pairs.jsonl"
    append_label(path, Pair(items[0], items[1], "same_cluster"), True, T0)
    append_label(path, Pair(items[0], items[3], "random"), False, T0)
    gone = LabelItem(99, "ff".ljust(8, "0"), "src0", "gone", None, T0, None, None, [])
    append_label(path, Pair(items[0], gone, "random"), False, T0)
    originals = read_labels(path, reviewed=False)
    todo = review_sample(originals, items, read_reviews(path), 10, random.Random(1))
    assert len(todo) == 2  # the purged item's pair can't be shown
    first = next(lab for lab, _a, _b in todo if lab.same)
    append_review(path, first, False, T0)
    assert not next(lab for lab in read_labels(path) if lab.stratum == "same_cluster").same
    assert next(
        lab for lab in read_labels(path, reviewed=False) if lab.stratum == "same_cluster"
    ).same
    again = review_sample(originals, items, read_reviews(path), 10, random.Random(1))
    assert [lab.stratum for lab, _a, _b in again] == ["random"]
    assert items[0].title not in (tmp_path / "labels" / "reviews.jsonl").read_text(encoding="utf-8")


def test_review_puts_informative_pairs_first_and_filters_languages() -> None:
    items = [replace(i, lang="sv" if i.id == 2 else "en") for i in pool()]
    k = [i.key for i in items]
    labels = [
        Label(k[0], k[3], False, "random"),
        Label(k[0], k[1], True, "same_cluster"),
        Label(k[1], k[4], False, "near_miss"),
        Label(k[0], k[2], True, "same_cluster"),  # one Swedish item
    ]
    todo = review_sample(labels, items, [], 10, random.Random(3), langs={"en"})
    strata = [lab.stratum for lab, _a, _b in todo]
    assert sorted(strata[:2]) == ["near_miss", "same_cluster"] and strata[2:] == ["random"]
    assert all(k[2] not in (lab.a, lab.b) for lab, _a, _b in todo)


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
