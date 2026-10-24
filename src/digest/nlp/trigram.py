"""Trigram similarity with the same definition as Postgres ``pg_trgm`` (spec section 6).

Used to match ``link_only`` items by title, without embeddings (rule L6).
"""

from __future__ import annotations

import re

_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def trigrams(text: str) -> set[str]:
    """pg_trgm: lowercase, split into words, pad each with two spaces before, one after."""
    out: set[str] = set()
    for word in _WORD.findall(text.lower()):
        padded = f"  {word} "
        out.update(padded[i : i + 3] for i in range(len(padded) - 2))
    return out


def similarity(a: str, b: str) -> float:
    ta, tb = trigrams(a), trigrams(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)
