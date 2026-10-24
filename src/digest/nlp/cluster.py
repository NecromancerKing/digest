"""Incremental clustering (stage 3), as pure in-memory code.

The database stage loads open clusters into a ``Clusterer``, feeds it items in time order
and writes the result back. ``cluster-eval`` runs the same code with other thresholds over
labelled pairs, so what is tuned is exactly what runs.

An item joins the best candidate cluster when
``cosine_weight * cos(item, centroid) + entity_weight * jaccard(entities)`` reaches
``join_threshold``; otherwise it opens a new cluster. Joining a cluster that already holds
an item from the same source needs ``same_source_threshold``: one outlet's notices and
features look alike to the model even when they are about different events. Candidates are
open clusters whose last item is inside the window, best ``top_k`` by cosine.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray

from digest.config import ClusterSettings
from digest.nlp.entities import jaccard
from digest.nlp.vectors import Vector, normalize


@dataclass(frozen=True)
class Params:
    cosine_weight: float = 0.75
    entity_weight: float = 0.25
    join_threshold: float = 0.78
    same_source_threshold: float = 0.88
    top_k: int = 10
    window: dt.timedelta = dt.timedelta(hours=48)
    merge_cosine: float = 0.90
    merge_entity_jaccard: float = 0.4
    entity_set_max: int = 40

    @classmethod
    def from_settings(cls, s: ClusterSettings) -> Params:
        return cls(
            cosine_weight=s.cosine_weight,
            entity_weight=s.entity_weight,
            join_threshold=s.join_threshold,
            same_source_threshold=s.same_source_threshold,
            top_k=s.candidate_top_k,
            window=dt.timedelta(hours=s.candidate_window_hours),
            merge_cosine=s.merge_cosine,
            merge_entity_jaccard=s.merge_entity_jaccard,
            entity_set_max=s.entity_set_max,
        )


@dataclass
class ClusterAcc:
    """One cluster while clustering. ``key`` is the database id, or negative for new ones."""

    key: int
    mean: Vector  # running mean of member vectors (not normalised)
    count: int
    last_item_at: dt.datetime
    created_at: dt.datetime
    entities: Counter[str] = field(default_factory=lambda: Counter[str]())
    sources: set[str] = field(default_factory=lambda: set[str]())
    dirty: bool = False

    @property
    def is_new(self) -> bool:
        return self.key < 0

    def entity_set(self, limit: int) -> list[str]:
        return [e for e, _ in self.entities.most_common(limit)]

    def add(self, vec: Vector | None, ents: Iterable[str], at: dt.datetime, source: str) -> None:
        if vec is not None:
            self.mean = (self.mean + (vec - self.mean) / (self.count + 1)).astype(np.float32)
        self.count += 1
        self.entities.update(set(ents))
        self.sources.add(source)
        self.last_item_at = max(self.last_item_at, at)
        self.dirty = True


@dataclass(frozen=True)
class Assignment:
    cluster: ClusterAcc
    score: float
    opened: bool


class Clusterer:
    def __init__(self, params: Params, clusters: Sequence[ClusterAcc] = (), dim: int = 384):
        self.params = params
        self.clusters: list[ClusterAcc] = list(clusters)
        self._next_key = -1
        units = [normalize(c.mean) for c in self.clusters]
        self._units: NDArray[np.float32] = (
            np.vstack(units).astype(np.float32) if units else np.zeros((0, dim), np.float32)
        )

    def _candidates(self, vec: Vector, at: dt.datetime) -> list[tuple[int, float]]:
        if not self.clusters:
            return []
        cos = self._units @ vec
        oldest = at - self.params.window
        recent = np.array([c.last_item_at >= oldest for c in self.clusters])
        cos = np.where(recent, cos, -np.inf)
        k = min(self.params.top_k, len(self.clusters))
        top = np.argpartition(-cos, k - 1)[:k]
        return [(int(i), float(cos[i])) for i in top if np.isfinite(cos[i])]

    def score(self, cos: float, ents: Iterable[str], cluster: ClusterAcc) -> float:
        """Weighted cosine and entity overlap; cosine alone when either side has no entities.

        Without the fallback, items whose titles name no one could never reach a threshold
        above ``cosine_weight``.
        """
        p = self.params
        mine, theirs = set(ents), cluster.entity_set(p.entity_set_max)
        if not mine or not theirs:
            return cos
        return p.cosine_weight * cos + p.entity_weight * jaccard(mine, theirs)

    def assign(self, vec: Vector, ents: Sequence[str], at: dt.datetime, source: str) -> Assignment:
        best: tuple[int, float] | None = None
        for i, cos in self._candidates(vec, at):
            c = self.clusters[i]
            s = self.score(cos, ents, c)
            needed = (
                self.params.same_source_threshold
                if source in c.sources
                else self.params.join_threshold
            )
            if s >= needed and (best is None or s > best[1]):
                best = (i, s)
        if best is not None:
            i, s = best
            c = self.clusters[i]
            c.add(vec, ents, at, source)
            self._units[i] = normalize(c.mean)
            return Assignment(c, s, opened=False)
        c = ClusterAcc(self._next_key, vec.copy(), 0, at, at)
        self._next_key -= 1
        c.add(vec, ents, at, source)
        self.clusters.append(c)
        self._units = np.vstack([self._units, normalize(c.mean)[None, :]])
        return Assignment(c, 1.0, opened=True)


def merge_groups(clusters: Sequence[ClusterAcc], params: Params) -> list[list[ClusterAcc]]:
    """Groups of clusters to merge: centroid cosine and entity overlap both high enough.

    Each group is ordered largest first; the first one absorbs the rest.
    """
    n = len(clusters)
    if n < 2:
        return []
    units = np.vstack([normalize(c.mean) for c in clusters])
    sims = units @ units.T
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    rows, cols = np.nonzero(np.triu(sims >= params.merge_cosine, k=1))
    for i, j in zip(rows.tolist(), cols.tolist(), strict=True):
        a, b = clusters[i], clusters[j]
        overlap = jaccard(a.entity_set(params.entity_set_max), b.entity_set(params.entity_set_max))
        if overlap >= params.merge_entity_jaccard:
            parent[find(i)] = find(j)
    groups: dict[int, list[ClusterAcc]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(clusters[i])
    return [sorted(g, key=lambda c: (-c.count, c.key)) for g in groups.values() if len(g) > 1]


def absorb(winner: ClusterAcc, losers: Sequence[ClusterAcc]) -> None:
    """Fold ``losers`` into ``winner``: weighted mean, union of entities and sources."""
    total = winner.count + sum(c.count for c in losers)
    mean = winner.mean * winner.count
    for c in losers:
        mean = mean + c.mean * c.count
        winner.entities.update(c.entities)
        winner.sources |= c.sources
        winner.last_item_at = max(winner.last_item_at, c.last_item_at)
        winner.created_at = min(winner.created_at, c.created_at)
    winner.mean = (mean / max(total, 1)).astype(np.float32)
    winner.count = total
    winner.dirty = True
