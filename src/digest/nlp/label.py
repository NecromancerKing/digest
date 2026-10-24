"""Clustering labels and evaluation (spec section 14, clustering evaluation).

The owner labels item pairs as "same event" or not with ``digest label`` on a local copy of
the state; labels go to ``labels/pairs.jsonl`` in the content repository. Each line holds
the two items' URL hashes (no third-party text), the answer and how the pair was sampled.

``digest label --review`` shows a sample of existing labels without their answers, for the
owner to check. The owner's answers go to ``labels/reviews.jsonl`` beside the labels and
replace the original answer wherever they differ.

``digest cluster-eval`` re-runs the clustering code over the labelled period with a grid
of thresholds and reports pair precision and recall, so thresholds are tuned on exactly
the algorithm that runs in production.
"""

from __future__ import annotations

import datetime as dt
import json
import random
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray
from sqlalchemy import Engine, select

from digest.db.schema import cluster_items, items
from digest.nlp.cluster import Clusterer, Params, absorb, merge_groups
from digest.nlp.embed import EmbedInput
from digest.nlp.entities import extract_entities
from digest.nlp.stages import EMBED_TEXT_CHARS, item_time
from digest.nlp.vectors import from_blob
from digest.timeutil import iso


@dataclass(frozen=True)
class LabelItem:
    id: int
    key: str  # hex url_hash: stable across state resets
    source_id: str
    title: str
    summary: str | None
    at: dt.datetime
    cluster_id: int | None
    embedding: NDArray[np.float32] | None
    entities: list[str]
    lang: str | None = None

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.summary or ''}"[:EMBED_TEXT_CHARS]


@dataclass(frozen=True)
class Pair:
    a: LabelItem
    b: LabelItem
    stratum: str  # same_cluster | near_miss | random

    @property
    def key(self) -> tuple[str, str]:
        return pair_key(self.a.key, self.b.key)


@dataclass(frozen=True)
class Label:
    a: str
    b: str
    same: bool
    stratum: str = ""


def pair_key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


# --------------------------------------------------------------------------- items and labels


def load_items(engine: Engine, since: dt.datetime | None = None) -> list[LabelItem]:
    """Embeddable items (never ``link_only``) with their current cluster, oldest first."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(items, cluster_items.c.cluster_id)
            .outerjoin(cluster_items, cluster_items.c.item_id == items.c.id)
            .where(items.c.usage_policy != "link_only")
        ).all()
    out = [
        LabelItem(
            id=r.id,
            key=bytes(r.url_hash).hex(),
            source_id=r.source_id,
            title=r.title,
            summary=r.feed_summary,
            at=item_time(r.published_at, r.fetched_at),
            cluster_id=r.cluster_id,
            embedding=from_blob(r.embedding) if r.embedding is not None else None,
            entities=json.loads(r.entities)
            if r.entities
            else extract_entities(r.title, r.feed_summary),
            lang=r.lang,
        )
        for r in rows
    ]
    out = [i for i in out if since is None or i.at >= since]
    return sorted(out, key=lambda i: (i.at, i.id))


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def reviews_path(labels: Path) -> Path:
    return labels.with_name("reviews.jsonl")


def read_reviews(labels: Path) -> dict[tuple[str, str], bool]:
    """The owner's answers to reviewed pairs; the last answer for a pair counts."""
    return {pair_key(d["a"], d["b"]): bool(d["same"]) for d in _jsonl(reviews_path(labels))}


def read_labels(path: Path, *, reviewed: bool = True) -> list[Label]:
    """The labels, with the owner's review answers in place of the originals unless
    ``reviewed`` is false."""
    overrides = read_reviews(path) if reviewed else {}
    out: list[Label] = []
    for d in _jsonl(path):
        same = overrides.get(pair_key(d["a"], d["b"]), bool(d["same"]))
        out.append(Label(d["a"], d["b"], same, d.get("stratum", "")))
    return out


def append_review(labels: Path, label: Label, same: bool, now: dt.datetime) -> None:
    a, b = pair_key(label.a, label.b)
    line = {"a": a, "b": b, "same": same, "reviewed_at": iso(now), "by": "owner"}
    with reviews_path(labels).open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(line) + "\n")


def review_sample(
    labels: Sequence[Label],
    pool: Sequence[LabelItem],
    done: Iterable[tuple[str, str]],
    n: int,
    rng: random.Random,
    langs: Collection[str] | None = None,
) -> list[tuple[Label, LabelItem, LabelItem]]:
    """Up to ``n`` labels not reviewed yet whose two items are still in the state (and in
    ``langs``, if given). Same-cluster pairs and near misses come first: random pairs are
    nearly always plainly different and tell little about the labeller."""
    by_key = {i.key: i for i in pool if langs is None or i.lang in langs}
    seen = set(done)
    out = [
        (lab, by_key[lab.a], by_key[lab.b])
        for lab in labels
        if lab.a in by_key and lab.b in by_key and pair_key(lab.a, lab.b) not in seen
    ]
    rng.shuffle(out)
    out.sort(key=lambda t: t[0].stratum == "random")  # stable: shuffled within each group
    return out[:n]


def append_label(path: Path, pair: Pair, same: bool, now: dt.datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    a, b = pair.key
    line = {"a": a, "b": b, "same": same, "stratum": pair.stratum, "labelled_at": iso(now)}
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(line) + "\n")


# --------------------------------------------------------------------------- sampling


def sample_pairs(
    pool: Sequence[LabelItem],
    n: int,
    done: Iterable[tuple[str, str]],
    rng: random.Random,
    near_miss_cosine: float = 0.45,
) -> list[Pair]:
    """Pairs to label: 40% from one cluster, 40% near misses from different clusters
    (similar but not joined), 20% random. Near misses are what decide recall."""
    seen = set(done)
    out: list[Pair] = []

    def take(a: LabelItem, b: LabelItem, stratum: str) -> None:
        k = pair_key(a.key, b.key)
        if a.key != b.key and k not in seen:
            seen.add(k)
            out.append(Pair(a, b, stratum))

    by_cluster: dict[int, list[LabelItem]] = {}
    for i in pool:
        if i.cluster_id is not None:
            by_cluster.setdefault(i.cluster_id, []).append(i)
    multi = [m for m in by_cluster.values() if len(m) > 1]
    for _ in range(n * 3):
        if len(out) >= n * 2 // 5 or not multi:
            break
        a, b = rng.sample(rng.choice(multi), 2)
        take(a, b, "same_cluster")

    with_vec = [i for i in pool if i.embedding is not None]
    if len(with_vec) > 1:
        m = np.vstack([i.embedding for i in with_vec if i.embedding is not None])
        sims = m @ m.T
        idx = np.argwhere(np.triu(sims >= near_miss_cosine, k=1))
        rng.shuffle(idx := idx.tolist())  # pyright: ignore[reportUnknownArgumentType]
        target = len(out) + n * 2 // 5
        for i, j in idx:
            if len(out) >= target:
                break
            a, b = with_vec[i], with_vec[j]
            if a.cluster_id != b.cluster_id and abs(a.at - b.at) <= dt.timedelta(hours=48):
                take(a, b, "near_miss")

    for _ in range(n * 3):
        if len(out) >= n or len(pool) < 2:
            break
        a, b = rng.sample(list(pool), 2)
        if a.cluster_id != b.cluster_id:
            take(a, b, "random")
    rng.shuffle(out)
    return out[:n]


# --------------------------------------------------------------------------- evaluation


@dataclass(frozen=True)
class Score:
    params: Params
    tp: int
    fp: int
    fn: int
    tn: int

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 1.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 0.0


def cluster_offline(
    pool: Sequence[LabelItem], vectors: NDArray[np.float32], params: Params
) -> dict[str, int]:
    """Run clustering and one merge pass over ``pool`` from scratch: item key -> cluster."""
    c = Clusterer(params, dim=vectors.shape[1] if len(vectors) else 384)
    assigned: dict[str, int] = {}
    for item, v in zip(pool, vectors, strict=True):
        assigned[item.key] = c.assign(v, item.entities, item.at, item.source_id).cluster.key
    parent: dict[int, int] = {}
    for group in merge_groups(c.clusters, params):
        absorb(group[0], group[1:])
        for loser in group[1:]:
            parent[loser.key] = group[0].key
    return {k: parent.get(v, v) for k, v in assigned.items()}


def evaluate(
    pool: Sequence[LabelItem],
    labels: Sequence[Label],
    embed: Callable[[Sequence[EmbedInput]], NDArray[np.float32]],
    grid: Iterable[Params],
) -> tuple[list[Score], int]:
    """Pair precision and recall per parameter set. Also returns labels whose items are gone."""
    keys = {i.key for i in pool}
    usable = [lab for lab in labels if lab.a in keys and lab.b in keys]
    vectors = embed([EmbedInput(i.text, "facts_only") for i in pool])
    scores: list[Score] = []
    for params in grid:
        assigned = cluster_offline(pool, vectors, params)
        tp = fp = fn = tn = 0
        for lab in usable:
            predicted = assigned[lab.a] == assigned[lab.b]
            if predicted and lab.same:
                tp += 1
            elif predicted:
                fp += 1
            elif lab.same:
                fn += 1
            else:
                tn += 1
        scores.append(Score(params, tp, fp, fn, tn))
    return scores, len(labels) - len(usable)


def default_grid(base: Params, method: str = "embeddings") -> list[Params]:
    if method == "tfidf":  # TF-IDF cosines run lower than embedding cosines
        thresholds = [round(0.05 + 0.025 * i, 3) for i in range(23)]  # 0.05 .. 0.60
        same_source = (0.5, 0.6, 0.7, 0.8, 1.01)
    else:
        thresholds = [round(0.5 + 0.025 * i, 3) for i in range(17)]  # 0.50 .. 0.90
        same_source = (0.8, 0.85, 0.9, 1.01)
    return [
        replace(
            base,
            join_threshold=t,
            same_source_threshold=max(t, ss),
            cosine_weight=w,
            entity_weight=round(1 - w, 2),
        )
        for w in (0.6, 0.75, 0.9, 1.0)  # 1.0: cosine only, entities ignored
        for t in thresholds
        for ss in same_source  # 1.01: never join a cluster with the same source
    ]


def recommend(scores: Sequence[Score], min_precision: float = 0.95) -> Score | None:
    """Highest recall among parameter sets that reach ``min_precision``."""
    ok = [s for s in scores if s.precision >= min_precision and s.tp > 0]
    return max(ok, key=lambda s: (s.recall, s.precision, s.params.join_threshold), default=None)
