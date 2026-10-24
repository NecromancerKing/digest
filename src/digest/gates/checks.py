"""G1 schema, G3 grounding and G4 sensitivity for LLM stories (spec section 8, stage 7).

Each check returns a ``GateOutcome``: pass, fail with feedback for a retry, or pass but
send to review. G2 lives in ``originality`` and G5 in ``duplicate``.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass, field

from digest.llm.schemas import GradeClaims, WriteStory

DANGEROUS_FOR_NAMES = frozenset({"crime", "accident", "court", "death"})


@dataclass(frozen=True)
class GateOutcome:
    gate: str
    ok: bool
    feedback: str | None = None  # for the retry prompt; our words only
    review: bool = False  # passes, but the owner must look
    detail: dict[str, object] = field(default_factory=lambda: {})


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
    """G1: lengths, and every claim cites at least one known fact of a member item."""
    problems: list[str] = []
    if len(story.headline) > headline_max:
        problems.append(f"the headline has {len(story.headline)} characters; max {headline_max}")
    n = _words(story.summary)
    if not words_min <= n <= words_max:
        problems.append(f"the summary has {n} words; it must have {words_min}-{words_max}")
    if story.why_it_matters and _words(story.why_it_matters) > why_max_words:
        problems.append(f"why it matters is longer than {why_max_words} words")
    if not story.claims:
        problems.append("no claims were listed")
    known = set(fact_ids)
    for i, c in enumerate(story.claims):
        cited = [f for f in c.fact_ids if f in known]
        if not cited:
            problems.append(f"claim {i} cites no known fact id")
    if problems:
        return GateOutcome("g1", False, "; ".join(problems))
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


def check_sensitivity(
    flags: Sequence[str], names_private_individual: bool, independent_sources: int
) -> GateOutcome:
    """G4: no private names in crime, accident, court or death stories; minors and
    single-source allegations against a named person go to review."""
    detail: dict[str, object] = {"flags": sorted(flags)}
    if names_private_individual and DANGEROUS_FOR_NAMES & set(flags):
        return GateOutcome(
            "g4",
            False,
            "remove the names of private individuals; describe them by role or age instead",
            detail=detail,
        )
    if "minors" in flags:
        return GateOutcome("g4", True, review=True, detail=detail)
    if "allegation_against_named_person" in flags and independent_sources < 2:
        return GateOutcome("g4", True, review=True, detail=detail)
    return GateOutcome("g4", True, detail=detail)
