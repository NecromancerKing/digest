"""Vectors as stored in the state file: little-endian float32 blobs (spec section 7)."""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

Vector = NDArray[np.float32]


def to_blob(v: Vector) -> bytes:
    return np.asarray(v, dtype="<f4").tobytes()


def from_blob(b: bytes) -> Vector:
    return np.frombuffer(b, dtype="<f4").astype(np.float32)


def normalize(v: Vector) -> Vector:
    n = float(np.linalg.norm(v))
    return (v / n).astype(np.float32) if n > 0 else v


def normalize_rows(m: NDArray[np.float32]) -> NDArray[np.float32]:
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (m / norms).astype(np.float32)
