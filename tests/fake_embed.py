"""A deterministic stand-in for the sentence model: texts sharing words get similar vectors.

Each word maps to a fixed random vector (seeded by its hash); a text is the normalised sum.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence

import numpy as np
from numpy.typing import NDArray

from digest.nlp.embed import EmbedInput, check_embeddable

DIM = 384


def word_vector(word: str) -> NDArray[np.float32]:
    seed = int.from_bytes(hashlib.sha256(word.encode()).digest()[:8], "little")
    return np.random.default_rng(seed).standard_normal(DIM).astype(np.float32)


def text_vector(text: str) -> NDArray[np.float32]:
    words = re.findall(r"\w+", text.lower())
    v = np.sum([word_vector(w) for w in words], axis=0) if words else np.ones(DIM)
    return (v / np.linalg.norm(v)).astype(np.float32)


class FakeEmbedder:
    dim = DIM

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, inputs: Sequence[EmbedInput]) -> NDArray[np.float32]:
        check_embeddable(inputs)
        self.calls.append([i.text for i in inputs])
        if not inputs:
            return np.zeros((0, DIM), np.float32)
        return np.vstack([text_vector(i.text) for i in inputs])
