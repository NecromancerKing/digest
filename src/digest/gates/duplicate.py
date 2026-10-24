"""G5 duplicate (spec section 8): cosine >= ``duplicate_cosine`` with another recent story."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from numpy.typing import NDArray

Vector = NDArray[np.float32]


def find_duplicate(
    vector: Vector | None, others: Sequence[tuple[int, Vector]], threshold: float
) -> int | None:
    """The id of the most similar other story at or above ``threshold``, else None."""
    if vector is None or not others:
        return None
    v = vector / (float(np.linalg.norm(vector)) or 1.0)
    best: tuple[float, int] | None = None
    for story_id, other in others:
        o = other / (float(np.linalg.norm(other)) or 1.0)
        sim = float(np.dot(v, o))
        if sim >= threshold and (best is None or sim > best[0]):
            best = (sim, story_id)
    return best[1] if best else None
