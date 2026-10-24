"""G2 originality (spec section 8, rule L8): our text must not reuse a source's wording.

Fails when, after lower-casing and stripping punctuation:

- the headline or summary shares a run of ``shared_run_words`` (8) or more words with any
  member item's title, feed summary or article text;
- the headline's word set has Jaccard similarity >= ``headline_jaccard`` (0.5) with any
  source headline;
- with embeddings available, the summary is a near-translation of one paragraph of a
  source in another language (cosine >= ``cross_lang_cosine``, 0.90). Skipped and
  reported under ``CLUSTER_METHOD=tfidf``.

The overlapping phrases are returned, so the retry can forbid them; never phrases from a
``link_only`` source, whose text may not reach an LLM (rule L6). A headline echo is
reported without quoting the source headline (rule L7).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray

_WORD = re.compile(r"[^\W_]+", re.UNICODE)

EmbedTexts = Callable[[Sequence[str]], NDArray[np.float32]]


@dataclass(frozen=True)
class SourceText:
    title: str
    feed_summary: str | None
    full_text: str | None
    lang: str | None
    usage_policy: str = "facts_only"


@dataclass(frozen=True)
class OriginalityResult:
    ok: bool
    reason: str | None = None  # shared_run | headline_echo | near_translation
    phrases: list[str] = field(default_factory=lambda: [])
    cross_lang_checked: bool = False


def words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _runs(ws: Sequence[str], n: int) -> set[tuple[str, ...]]:
    return {tuple(ws[i : i + n]) for i in range(len(ws) - n + 1)}


def shared_runs(ours: str, theirs: str, n: int) -> list[str]:
    """Phrases of ``n`` words found in both texts, longest overlaps merged."""
    a, b = words(ours), words(theirs)
    common = _runs(a, n) & _runs(b, n)
    if not common:
        return []
    out: list[str] = []
    i = 0
    while i <= len(a) - n:
        if tuple(a[i : i + n]) in common:
            j = i + n
            while j < len(a) and tuple(a[j - n + 1 : j + 1]) in common:
                j += 1
            out.append(" ".join(a[i:j]))
            i = j
        else:
            i += 1
    return out


def headline_jaccard(ours: str, theirs: str) -> float:
    a, b = set(words(ours)), set(words(theirs))
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def paragraphs(s: SourceText) -> list[str]:
    parts = [s.title, s.feed_summary or "", *(s.full_text or "").split("\n")]
    return [p.strip() for p in parts if len(words(p)) >= 8]


def check_originality(
    headline: str,
    summary: str,
    sources: Sequence[SourceText],
    *,
    edition_lang: str,
    shared_run_words: int = 8,
    headline_jaccard_max: float = 0.5,
    cross_lang_cosine: float = 0.90,
    embed: EmbedTexts | None = None,
) -> OriginalityResult:
    phrases: list[str] = []
    failed = False
    for s in sources:
        for text in (s.title, s.feed_summary or "", s.full_text or ""):
            if text:
                found = shared_runs(headline, text, shared_run_words)
                found += shared_runs(summary, text, shared_run_words)
                failed = failed or bool(found)
                if s.usage_policy != "link_only":
                    phrases += found
    if failed:
        return OriginalityResult(False, "shared_run", sorted(set(phrases)))
    for s in sources:
        if headline_jaccard(headline, s.title) >= headline_jaccard_max:
            return OriginalityResult(False, "headline_echo")
    if embed is None:
        return OriginalityResult(True)
    foreign = [  # link_only text is never embedded (rule L6)
        p
        for s in sources
        if (s.lang or edition_lang) != edition_lang and s.usage_policy != "link_only"
        for p in paragraphs(s)
    ]
    if not foreign:
        return OriginalityResult(True, cross_lang_checked=True)
    vecs = embed([summary, *foreign])
    sims = vecs[1:] @ vecs[0]
    if float(np.max(sims)) >= cross_lang_cosine:
        return OriginalityResult(False, "near_translation", cross_lang_checked=True)
    return OriginalityResult(True, cross_lang_checked=True)
