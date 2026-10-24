from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import Engine, select

from digest import db
from digest.config import Config, load_config
from digest.db.schema import clusters, items
from digest.nlp.cluster import Params
from digest.nlp.embed import EmbedInput, LinkOnlyViolation
from digest.nlp.stages import reset_vectors, run_cluster, run_embed
from digest.nlp.tfidf import TfidfEmbedder, tokens
from tests.conftest import TEST_SOURCES
from tests.test_nlp_stages import EXTRA_SOURCES, Budget, add_item, membership

T0 = dt.datetime(2026, 10, 24, 9, 0, tzinfo=dt.UTC)


def inputs(*texts: str) -> list[EmbedInput]:
    return [EmbedInput(t, "facts_only") for t in texts]


def test_tokens_drop_stopwords_and_add_pairs() -> None:
    assert tokens("The Riksbank cuts the rate in 2026") == [
        "riksbank", "cuts", "rate", "riksbank cuts", "cuts rate",
    ]  # fmt: skip


def test_vectors_are_deterministic_and_similar_for_shared_words() -> None:
    a, b = TfidfEmbedder(), TfidfEmbedder()
    texts = inputs(
        "Earthquake hits southern Japan", "Strong earthquake hits Japan", "Riksbank cuts rate"
    )
    va, vb = a.embed(texts), b.embed(texts)
    assert np.array_equal(va, vb)
    assert va.shape == (3, 1024)
    assert np.allclose(np.linalg.norm(va, axis=1), 1.0)
    assert float(va[0] @ va[1]) > 0.4 > float(va[0] @ va[2])


def test_refuses_link_only_text() -> None:
    with pytest.raises(LinkOnlyViolation):
        TfidfEmbedder().embed([EmbedInput("x", "link_only")])


def test_document_frequencies_round_trip_and_ignore_other_dims() -> None:
    e = TfidfEmbedder(128)
    e.embed(inputs("one two", "two three"))
    restored = TfidfEmbedder(128)
    restored.load_state(e.dump_state())
    assert restored.docs == 2 and np.array_equal(restored.df, e.df)
    other = TfidfEmbedder(256)
    other.load_state(e.dump_state())
    assert other.docs == 0
    assert json.loads(e.dump_state())["dim"] == 128


def test_params_use_tfidf_thresholds(config: Config) -> None:
    cs = config.settings.cluster
    assert Params.from_settings(cs, "tfidf").join_threshold == cs.tfidf_join_threshold
    assert Params.from_settings(cs).join_threshold == cs.join_threshold


@pytest.fixture
def config(ingest_config_dir: Path) -> Config:
    (ingest_config_dir / "sources.yaml").write_text(TEST_SOURCES + EXTRA_SOURCES, encoding="utf-8")
    return load_config(ingest_config_dir)


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    path = tmp_path / "state.sqlite"
    db.migrate(path)
    return db.make_engine(path)


def test_tfidf_clusters_same_language_reports(engine: Engine, config: Config) -> None:
    a = add_item(engine, "news", "Strong earthquake hits southern Japan coast")
    b = add_item(engine, "wire", "Earthquake hits Japan coast, tsunami warning issued")
    c = add_item(engine, "agency", "Riksbanken sänker styrräntan")
    run_embed(engine, TfidfEmbedder(), config, Budget())
    run_cluster(engine, config, T0, "tfidf")
    m = membership(engine)
    assert m[a] == m[b] != m[c]


def test_reset_vectors_closes_clusters_and_requeues(engine: Engine, config: Config) -> None:
    a = add_item(engine, "news", "Strong earthquake hits southern Japan coast")
    run_embed(engine, TfidfEmbedder(), config, Budget())
    run_cluster(engine, config, T0, "tfidf")
    b = add_item(engine, "wire", "Riksbank cuts rate")
    run_embed(engine, TfidfEmbedder(), config, Budget())
    stats = reset_vectors(engine)
    assert stats == {"clusters_closed": 1, "items_requeued": 1}
    with engine.connect() as conn:
        rows = {r.id: r for r in conn.execute(select(items))}
        assert conn.execute(select(clusters.c.is_open)).scalars().all() == [0]
    assert rows[a].embedding is None and rows[a].status == "clustered"
    assert rows[b].embedding is None and rows[b].status == "new"
