"""G1 schema, G3 grounding and G4 sensitivity for LLM stories (spec section 8, stage 7).

Each check returns a ``GateOutcome``: pass, or fail with feedback for a retry. Nobody
reviews stories (D19), so nothing is sent to review. Sensitive subjects are summarised
(D28); G4 only keeps private individuals' names out of the summary. G2 lives in
``originality`` and G5 in ``duplicate``.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass, field

from digest.llm.schemas import GradeClaims, WriteStory

CITATION = re.compile(r"\b\d+:f\d+\b")  # a fact id such as 123:f1 left in the text


@dataclass(frozen=True)
class GateOutcome:
    gate: str
    ok: bool
    feedback: str | None = None  # for the retry prompt; our words only
    detail: dict[str, object] = field(default_factory=lambda: {})
    codes: tuple[str, ...] = ()  # which rules failed, as fixed codes for the public log


def _words(text: str) -> int:
    return len(text.split())


def check_schema(
    story: WriteStory,
    fact_ids: Collection[str],
    *,
    words_min: int,
    words_max: int,
    headline_max: int,
    why_max_words: int,
) -> GateOutcome:
    """G1: lengths, and every claim cites at least one known fact of a member item.

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
    if CITATION.search(story.headline) or CITATION.search(story.summary):
        problems.append("fact ids appear in the text; list them only in claims")
        codes.append("ids_in_text")
    known = set(fact_ids)
    for i, c in enumerate(story.claims):
        cited = [f for f in c.fact_ids if f in known]
        if not cited:
            problems.append(f"claim {i} cites no known fact id")
            if "uncited_claim" not in codes:
                codes.append("uncited_claim")
    if problems:
        return GateOutcome("g1", False, "; ".join(problems), codes=tuple(codes))
    return GateOutcome("g1", True)


def check_grounding(story: WriteStory, grades: GradeClaims) -> GateOutcome:
    """G3: any unsupported claim fails; a claim the grader skipped counts as unsupported."""
    verdicts = {g.claim_index: g.verdict for g in grades.results}
    unsupported = [
        i for i in range(len(story.claims)) if verdicts.get(i, "unsupported") == "unsupported"
    ]
    partial = [i for i in range(len(story.claims)) if verdicts.get(i) == "partial"]
    detail: dict[str, object] = {
        "supported": sum(v == "supported" for v in verdicts.values()),
        "partial": len(partial),
        "unsupported": len(unsupported),
    }
    if unsupported:
        texts = "; ".join(f'"{story.claims[i].text}"' for i in unsupported)
        return GateOutcome(
            "g3",
            False,
            f"these claims are not supported by the cited facts: {texts}",
            detail=detail,
        )
    return GateOutcome("g3", True, detail=detail)


def check_sensitivity(names_private_individual: bool) -> GateOutcome:
    """G4 after writing: a summary never names a private individual."""
    if names_private_individual:
        return GateOutcome(
            "g4",
            False,
            "remove the names of private individuals; describe them by role or age instead",
        )
    return GateOutcome("g4", True)
