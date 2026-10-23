from __future__ import annotations

import datetime as dt
from collections import Counter
from pathlib import Path

import numpy as np
import pytest

from digest.config import RankSettings
from digest.nlp import rank
from digest.nlp.cluster import ClusterAcc, Clusterer, Params, absorb, merge_groups
from digest.nlp.embed import EmbedInput, FastEmbedder, LinkOnlyViolation
from digest.nlp.entities import extract_entities, jaccard
from digest.nlp.trigram import similarity, trigrams
from digest.nlp.vectors import from_blob, normalize, to_blob
from tests.fake_embed import text_vector

T0 = dt.datetime(2026, 10, 24, 9, 0, tzinfo=dt.UTC)


# --------------------------------------------------------------------------- small helpers


def test_trigrams_match_pg_trgm() -> None:
    assert trigrams("cat") == {"  c", " ca", "cat", "at "}
    assert similarity("cat", "cats") == pytest.approx(0.5)  # pg_trgm: 0.5
    assert similarity("Word", "word!") == 1.0
    assert similarity("", "x") == 0.0


def test_vector_blobs_round_trip() -> None:
    v = normalize(np.array([3.0, 4.0], dtype=np.float32))
    assert to_blob(v) == np.array([0.6, 0.8], dtype="<f4").tobytes()
    assert np.allclose(from_blob(to_blob(v)), v)


@pytest.mark.parametrize(
    ("title", "summary", "expected"),
    [
        ("Riksbanken sänker räntan, säger Erik Thedéen", None, ["erik thedéen"]),
        ("NATO leaders meet in Oslo", None, ["nato", "oslo"]),
        (
            "Earthquake Hits Southern Japan As Warning Issued",
            "A quake struck Kyushu in Japan on Friday.",
            ["kyushu", "japan"],
        ),
    ],
)
def test_entities(title: str, summary: str | None, expected: list[str]) -> None:
    assert extract_entities(title, summary) == expected


def test_jaccard() -> None:
    assert jaccard(["a", "b"], ["b", "c"]) == pytest.approx(1 / 3)
    assert jaccard([], ["a"]) == 0.0


# --------------------------------------------------------------------------- embed guard (L6)


def test_link_only_text_never_reaches_the_model(tmp_path: Path) -> None:
    embedder = FastEmbedder("no/such-model", tmp_path / "models")
    with pytest.raises(LinkOnlyViolation):
        embedder.embed([EmbedInput("ok", "facts_only"), EmbedInput("radio", "link_only")])
    assert embedder._model is None  # pyright: ignore[reportPrivateUsage]
    assert not (tmp_path / "models").exists()


# --------------------------------------------------------------------------- clustering


def test_similar_items_join_and_others_open_clusters() -> None:
    c = Clusterer(Params(join_threshold=0.6))
    a = c.assign(text_vector("earthquake hits japan coast"), ["japan"], T0, "nrk")
    b = c.assign(text_vector("earthquake hits japan coast today"), ["japan"], T0, "svt")
    d = c.assign(text_vector("riksbank cuts interest rate"), ["riksbank"], T0, "svt")
    assert a.opened and not b.opened and d.opened
    assert b.cluster is a.cluster
    assert a.cluster.count == 2 and a.cluster.sources == {"nrk", "svt"}
    assert len(c.clusters) == 2


def test_entity_overlap_counts_toward_the_score() -> None:
    c = Clusterer(Params(join_threshold=0.75))
    c.assign(text_vector("minister resigns after scandal"), ["anna berg"], T0, "a")
    with_entity = c.score(0.8, ["anna berg"], c.clusters[0])
    without = c.score(0.8, ["someone else"], c.clusters[0])
    assert with_entity == pytest.approx(0.75 * 0.8 + 0.25) and without == pytest.approx(0.6)


def test_clusters_outside_the_window_are_not_candidates() -> None:
    c = Clusterer(Params(join_threshold=0.6))
    c.assign(text_vector("earthquake hits japan coast"), [], T0, "a")
    later = c.assign(
        text_vector("earthquake hits japan coast"), [], T0 + dt.timedelta(hours=49), "b"
    )
    assert later.opened


def test_merge_groups_and_absorb() -> None:
    v = text_vector("flood warning in skane")
    a = ClusterAcc(1, v, 3, T0, T0, entities=Counter({"skåne": 3}))
    b = ClusterAcc(2, v, 1, T0 + dt.timedelta(hours=1), T0, sources={"x"})
    b.entities.update({"skåne": 1})
    other = ClusterAcc(3, text_vector("football results"), 2, T0, T0)
    groups = merge_groups([a, b, other], Params())
    assert [[c.key for c in g] for g in groups] == [[1, 2]]
    absorb(a, [b])
    assert a.count == 4 and a.sources == {"x"} and a.last_item_at == T0 + dt.timedelta(hours=1)


# --------------------------------------------------------------------------- ranking (stage 4)

R = RankSettings()
WEIGHTS = {"sweden": 0.15, "world": 0.3, "sport": 0.0}


def view(
    i: int,
    cat: str = "world",
    n: int = 2,
    official: bool = False,
    age_h: float = 0,
    ol: bool = False,
) -> rank.ClusterView:
    return rank.ClusterView(i, cat, n, official, ol, T0 - dt.timedelta(hours=age_h))


def test_score_formula() -> None:
    s = rank.score(view(1, n=3, official=True, age_h=8), T0, R, WEIGHTS)
    assert s == pytest.approx(np.log(4) + 0.5 + 0.4 * 0.5 + 0.3)
    single = rank.score(view(2, cat="sport", n=1), T0, R, WEIGHTS)
    assert single == pytest.approx(np.log(2) + 0.4 - 0.6)
    assert rank.score(view(3, cat="sport", n=1, official=True), T0, R, WEIGHTS) > single


def test_single_source_rule() -> None:
    assert rank.eligible(view(1, n=2), R)
    assert not rank.eligible(view(2, n=1), R)
    assert rank.eligible(view(3, n=1, official=True), R)
    assert rank.eligible(view(4, n=1, ol=True), R)
    assert not rank.eligible(rank.ClusterView(5, None, 3, False, False, T0), R)


def test_select_respects_daily_caps() -> None:
    cands = [rank.Ranked(i, "sweden", 10 - i, T0) for i in range(5)] + [
        rank.Ranked(9, "world", 0.1, T0)
    ]
    chosen = rank.select(cands, already_today={"sweden": 3}, caps={"sweden": 5, "world": 12})
    assert [c.id for c in chosen] == [0, 1, 9]


def test_top_stories_are_diverse() -> None:
    stories = [rank.Ranked(i, "world", 10 - i, T0) for i in range(4)] + [
        rank.Ranked(10, "sweden", 1, T0),
        rank.Ranked(11, "sport", 0.5, T0),
    ]
    assert [s.id for s in rank.top_stories(stories, R)] == [0, 1, 10, 11]


def test_today_page_window_and_caps() -> None:
    stories = [rank.Ranked(i, "world", 20 - i, T0 - dt.timedelta(hours=i)) for i in range(6)] + [
        rank.Ranked(50, "sweden", 100, T0 - dt.timedelta(hours=30)),  # too old
        rank.Ranked(51, "sweden", 1, T0 - dt.timedelta(hours=2)),
    ]
    assert [s.id for s in rank.today(stories, T0, R)] == [0, 1, 2, 51]
