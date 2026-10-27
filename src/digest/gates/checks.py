"""G1 schema and G3 grounding for LLM stories (spec section 8, stage 7).

Each check returns a ``GateOutcome``: pass, or fail with feedback for a retry. Nobody
reviews stories (D19), so nothing is sent to review. There is no content filter (D28):
G4 is gone. G2 lives in ``originality`` and G5 in ``duplicate``.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass, field

from digest.llm.schemas import Claim, GradeClaims, WriteStory

CITATION = re.compile(r"\b\d+:f\d+\b")  # a fact id such as 123:f1 left in the text
# The same, with its brackets and the space before it, for removal: "rose (12:f1, 12:f2)."
_CITATION_RUN = re.compile(r"\s*[(\[]?\b\d+:f\d+\b(?:\s*[,;]\s*\b\d+:f\d+\b)*[)\]]?")


def strip_citations(story: WriteStory) -> WriteStory:
    """The story with fact ids removed from its text, for when G1 doesn't stop it."""

    def clean(text: str) -> str:
        return _CITATION_RUN.sub("", text).strip()

    return story.model_copy(
        update={
            "headline": clean(story.headline),
            "summary": clean(story.summary),
            "why_it_matters": clean(story.why_it_matters) if story.why_it_matters else None,
            "key_facts": [k.model_copy(update={"text": clean(k.text)}) for k in story.key_facts],
        }
    )


@dataclass(frozen=True)
class GateOutcome:
    gate: str
    ok: bool
    feedback: str | None = None  # for the retry prompt; our words only
    detail: dict[str, object] = field(default_factory=lambda: {})
    codes: tuple[str, ...] = ()  # which rules failed, as fixed codes for the public log


def _words(text: str) -> int:
    return len(text.split())


def graded_claims(story: WriteStory) -> list[Claim]:
    """What G3 checks: the summary's claims, then the key facts."""
    return [*story.claims, *story.key_facts]


def check_schema(
    story: WriteStory,
    fact_ids: Collection[str],
    *,
    words_min: int,
    words_max: int,
    headline_max: int,
    why_max_words: int,
    key_facts_max: int = 5,
    key_fact_max_words: int = 20,
) -> GateOutcome:
    """G1: lengths, and every claim and key fact cites at least one known fact of a member item.

    A failure's ``codes`` name the broken rules.
    """
    problems: list[str] = []
    codes: list[str] = []
    if len(story.headline) > headline_max:
        problems.append(f"the headline has {len(story.headline)} characters; max {headline_max}")
        codes.append("headline_long")
    n = _words(story.summary)
    if not words_min <= n <= words_max:
        problems.append(f"the summary has {n} words; it must have {words_min}-{words_max}")
        codes.append("summary_short" if n < words_min else "summary_long")
    if story.why_it_matters and _words(story.why_it_matters) > why_max_words:
        problems.append(f"why it matters is longer than {why_max_words} words")
        codes.append("why_long")
    if not story.claims:
        problems.append("no claims were listed")
        codes.append("no_claims")
    if len(story.key_facts) > key_facts_max:
        problems.append(f"there are {len(story.key_facts)} key facts; max {key_facts_max}")
        codes.append("key_facts_many")
    if any(_words(k.text) > key_fact_max_words for k in story.key_facts):
        problems.append(f"a key fact is longer than {key_fact_max_words} words")
        codes.append("key_fact_long")
    texts = [story.headline, story.summary, *(k.text for k in story.key_facts)]
    if any(CITATION.search(x) for x in texts):
        problems.append("fact ids appear in the text; list them only in fact_ids")
        codes.append("ids_in_text")
    known = set(fact_ids)
    for what, entries in (("claim", story.claims), ("key fact", story.key_facts)):
        for i, c in enumerate(entries):
            if not any(f in known for f in c.fact_ids):
                problems.append(f"{what} {i} cites no known fact id")
                if "uncited_claim" not in codes:
                    codes.append("uncited_claim")
    if problems:
        return GateOutcome("g1", False, "; ".join(problems), codes=tuple(codes))
    return GateOutcome("g1", True)


def check_grounding(story: WriteStory, grades: GradeClaims) -> GateOutcome:
    """G3: any unsupported claim or key fact fails; one the grader skipped counts as
    unsupported. Indexes follow ``graded_claims``."""
    claims = graded_claims(story)
    verdicts = {g.claim_index: g.verdict for g in grades.results}
    unsupported = [i for i in range(len(claims)) if verdicts.get(i, "unsupported") == "unsupported"]
    partial = [i for i in range(len(claims)) if verdicts.get(i) == "partial"]
    detail: dict[str, object] = {
        "supported": sum(v == "supported" for v in verdicts.values()),
        "partial": len(partial),
        "unsupported": len(unsupported),
    }
    if unsupported:
        texts = "; ".join(f'"{claims[i].text}"' for i in unsupported)
        return GateOutcome(
            "g3",
            False,
            f"these claims are not supported by the cited facts: {texts}",
            detail=detail,
        )
    return GateOutcome("g3", True, detail=detail)
