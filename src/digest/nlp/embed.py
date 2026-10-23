"""Local embeddings (stage 2). Text never leaves the runner.

Every embedder takes ``EmbedInput`` objects that carry the item's usage policy, and
refuses ``link_only`` text before anything is computed (rule L6).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from numpy.typing import NDArray

from digest.nlp.vectors import normalize_rows


class LinkOnlyViolation(Exception):
    """``link_only`` text was about to be embedded (rule L6)."""


@dataclass(frozen=True)
class EmbedInput:
    text: str
    usage_policy: str


def check_embeddable(inputs: Sequence[EmbedInput]) -> None:
    if any(i.usage_policy == "link_only" for i in inputs):
        raise LinkOnlyViolation("link_only text may not be embedded")


class Embedder(Protocol):
    dim: int

    def embed(self, inputs: Sequence[EmbedInput]) -> NDArray[np.float32]:
        """Unit-length rows, one per input. Raises ``LinkOnlyViolation``."""
        ...


class FastEmbedder:
    """A multilingual sentence model run on CPU with fastembed; files cached on disk."""

    def __init__(self, model: str, cache_dir: Path, dim: int = 384) -> None:
        self.model_name = model
        self.cache_dir = cache_dir
        self.dim = dim
        self._model: Any = None

    def _load(self) -> Any:
        if self._model is None:
            from fastembed import TextEmbedding  # slow import; only when needed

            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._model = TextEmbedding(self.model_name, cache_dir=str(self.cache_dir))
        return self._model

    def embed(self, inputs: Sequence[EmbedInput]) -> NDArray[np.float32]:
        check_embeddable(inputs)  # before the model even loads
        if not inputs:
            return np.zeros((0, self.dim), dtype=np.float32)
        rows = list(self._load().embed([i.text for i in inputs]))
        return normalize_rows(np.asarray(rows, dtype=np.float32))
