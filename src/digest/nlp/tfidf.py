"""TF-IDF vectors without a model (``CLUSTER_METHOD=tfidf``, spec section 8, no-ML variant).

Stage 2 builds a vector per item from its title and feed summary: lower-cased words minus
stopwords, plus adjacent word pairs, hashed into ``dim`` buckets with a sign bit (so
collisions cancel rather than add up), weighted by ``1 + log(tf)`` and by inverse document
frequency, then normalised. The hash is ``blake2b``, so vectors are the same on every
machine and run.

Document frequencies are counted per bucket over every item seen so far and saved in the
state (``kv``), so IDF improves as the pipeline runs. Vectors made earlier keep their old
weights; with a 48-hour clustering window that drift is small.

It needs no download and no ML, at the cost of cross-language matching: a Swedish and an
English report of one event share few words.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Sequence
from itertools import pairwise

import numpy as np
from numpy.typing import NDArray

from digest.nlp.embed import EmbedInput, check_embeddable
from digest.nlp.entities import STOPWORDS
from digest.nlp.vectors import normalize_rows

DEFAULT_DIM = 1024
_WORD = re.compile(r"[^\W_]{2,}", re.UNICODE)


def tokens(text: str) -> list[str]:
    """Words and adjacent word pairs, lower case, stopwords and digits-only words removed."""
    words = [w for w in _WORD.findall(text.lower()) if w not in STOPWORDS and not w.isdigit()]
    return words + [f"{a} {b}" for a, b in pairwise(words)]


def _bucket(token: str, dim: int) -> tuple[int, float]:
    h = int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest(), "little")
    return h % dim, (1.0 if (h >> 63) & 1 else -1.0)


class TfidfEmbedder:
    """An ``Embedder`` that learns document frequencies as it goes. Never sees link_only text."""

    def __init__(self, dim: int = DEFAULT_DIM) -> None:
        self.dim = dim
        self.docs = 0
        self.df = np.zeros(dim, dtype=np.int64)

    # --- state kept between runs

    def dump_state(self) -> str:
        return json.dumps({"dim": self.dim, "docs": self.docs, "df": self.df.tolist()})

    def load_state(self, raw: str | None) -> None:
        """Restore counts saved by ``dump_state``; counts for another ``dim`` are dropped."""
        if not raw:
            return
        data = json.loads(raw)
        if data.get("dim") != self.dim:
            return
        self.docs = int(data["docs"])
        self.df = np.asarray(data["df"], dtype=np.int64)

    # --- vectors

    def _counts(self, text: str) -> Counter[int]:
        return Counter(_bucket(t, self.dim)[0] for t in tokens(text))

    def fit(self, texts: Sequence[str]) -> None:
        """Count document frequencies without making vectors (``cluster-eval``)."""
        for text in texts:
            for b in self._counts(text):
                self.df[b] += 1
            self.docs += 1

    def embed(self, inputs: Sequence[EmbedInput]) -> NDArray[np.float32]:
        check_embeddable(inputs)
        if not inputs:
            return np.zeros((0, self.dim), dtype=np.float32)
        self.fit([i.text for i in inputs])  # this batch counts toward its own IDF
        idf = np.log((1 + self.docs) / (1 + self.df)) + 1.0
        out = np.zeros((len(inputs), self.dim), dtype=np.float32)
        for row, i in enumerate(inputs):
            tf = Counter[tuple[int, float]](_bucket(t, self.dim) for t in tokens(i.text))
            for (b, sign), n in tf.items():
                out[row, b] += sign * (1.0 + math.log(n)) * float(idf[b])
        return normalize_rows(out)
