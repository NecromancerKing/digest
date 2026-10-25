from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from numpy.typing import NDArray

from digest.gates.checks import check_grounding, check_schema, check_sensitivity, is_sensitive
from digest.gates.originality import SourceText, check_originality, shared_runs
from digest.llm.schemas import Claim, Grade, GradeClaims, WriteStory

SOURCE = SourceText(
    title="Central bank raises interest rate to fight stubborn inflation",
    feed_summary=(
        "The central bank on Thursday raised its key interest rate by half a percentage point, "
        "saying inflation remains far above target."
    ),
    full_text=None,
    lang="en",
)

CLEAN_HEADLINE = "Policy rate goes up half a point"
CLEAN_SUMMARY = (
    "Borrowing costs rose on Thursday after the bank lifted its main rate by 0.5 points. "
    "It said prices are still climbing much faster than it wants."
)


def check(headline: str, summary: str, sources: Sequence[SourceText] = (SOURCE,), **kw: object):  # pyright: ignore[reportUnknownParameterType, reportMissingParameterType]
    return check_originality(headline, summary, sources, edition_lang="en", **kw)  # pyright: ignore[reportArgumentType]


def test_clean_text_passes() -> None:
    assert check(CLEAN_HEADLINE, CLEAN_SUMMARY).ok


def test_shared_run_of_eight_words_fails_with_the_phrase() -> None:
    copied = "Officials said the central bank on Thursday raised its key interest rate sharply."
    r = check(CLEAN_HEADLINE, copied)
    assert not r.ok and r.reason == "shared_run"
    assert r.phrases == ["the central bank on thursday raised its key interest rate"]
    # Seven shared words are fine; punctuation and case don't hide a copy of eight.
    assert check(CLEAN_HEADLINE, "The central bank, on Thursday, raised ITS rates again.").ok
    assert not check(
        CLEAN_HEADLINE, "THE CENTRAL BANK, on Thursday, raised its key interest rate."
    ).ok
    assert shared_runs("a b c d e f g", "a b c d e f g h", 8) == []


def test_headline_echo_fails_without_quoting_the_source() -> None:
    r = check("Central bank raises interest rate to fight inflation", CLEAN_SUMMARY)
    assert not r.ok and r.reason == "headline_echo" and r.phrases == []


def test_link_only_wording_fails_but_is_never_returned() -> None:
    radio = SourceText(
        title="Storm closes every road across the northern mountain region tonight",
        feed_summary=None,
        full_text=None,
        lang="en",
        usage_policy="link_only",
    )
    r = check(
        CLEAN_HEADLINE,
        "Storm closes every road across the northern mountain region tonight, police say.",
        [radio],
    )
    assert not r.ok and r.phrases == []


def fake_embed(pairs: dict[str, str]):  # pyright: ignore[reportUnknownParameterType]
    """Texts listed as translations of each other get the same vector."""

    def embed(texts: Sequence[str]) -> NDArray[np.float32]:
        rows: list[NDArray[np.float32]] = []
        for i, t in enumerate(texts):
            key = pairs.get(t, t)
            seed = abs(hash(key)) % (2**32) if key in pairs.values() else 1000 + i
            v = np.random.default_rng(seed).standard_normal(16).astype(np.float32)
            rows.append(v / np.linalg.norm(v))
        return np.vstack(rows)

    return embed


def test_cross_language_near_translation_fails() -> None:
    swedish = SourceText(
        title="Riksbanken höjer styrräntan med en halv procentenhet på torsdagen",
        feed_summary=None,
        full_text=None,
        lang="sv",
    )
    summary = "The Riksbank raised its policy rate by half a percentage point on Thursday."
    embed = fake_embed({summary: "same", swedish.title: "same"})
    r = check(CLEAN_HEADLINE, summary, [swedish], embed=embed)
    assert not r.ok and r.reason == "near_translation" and r.cross_lang_checked
    other = fake_embed({})
    assert check(CLEAN_HEADLINE, summary, [swedish], embed=other).ok
    # Without embeddings (tfidf mode) the check is skipped and says so.
    assert not check(CLEAN_HEADLINE, summary, [swedish]).cross_lang_checked


def test_link_only_text_is_never_embedded() -> None:
    radio = SourceText(
        "Radio title with enough words to be a paragraph here", None, None, "sv", "link_only"
    )
    seen: list[str] = []

    def embed(texts: Sequence[str]) -> NDArray[np.float32]:
        seen.extend(texts)
        return np.eye(len(texts), dtype=np.float32)

    check(CLEAN_HEADLINE, CLEAN_SUMMARY, [radio], embed=embed)
    assert radio.title not in seen


def story(**kw: object) -> WriteStory:
    base: dict[str, object] = {
        "headline": "Policy rate goes up",
        "summary": " ".join(["word"] * 40),
        "why_it_matters": None,
        "claims": [Claim(text="rate up", fact_ids=["1:f1"])],
        "names_private_individual": False,
        "confidence": "high",
    }
    base.update(kw)
    return WriteStory.model_validate(base)


def test_g1_lengths_and_citations() -> None:
    kw = {"words_min": 30, "words_max": 60, "headline_max": 70, "why_max_words": 25}
    assert check_schema(story(), {"1:f1"}, **kw).ok
    assert not check_schema(story(headline="x" * 71), {"1:f1"}, **kw).ok
    short = check_schema(story(summary="too short"), {"1:f1"}, **kw)
    assert not short.ok and "2 words" in (short.feedback or "")
    assert not check_schema(story(claims=[Claim(text="x", fact_ids=["9:f9"])]), {"1:f1"}, **kw).ok
    assert not check_schema(story(why_it_matters=" ".join(["w"] * 26)), {"1:f1"}, **kw).ok
    both = check_schema(story(headline="x" * 71, summary="too short"), {"9:f9"}, **kw)
    assert both.codes == ("headline_long", "summary_short", "uncited_claim")


def test_g3_catches_unsupported_and_skipped_claims() -> None:
    s = story(claims=[Claim(text="a", fact_ids=["1:f1"]), Claim(text="b", fact_ids=["1:f1"])])
    ok = GradeClaims(results=[Grade(claim_index=0, verdict="supported", reason=""),
                              Grade(claim_index=1, verdict="partial", reason="")])  # fmt: skip
    assert check_grounding(s, ok).ok
    bad = GradeClaims(results=[Grade(claim_index=0, verdict="unsupported", reason="")])
    out = check_grounding(s, bad)
    assert not out.ok and out.detail["unsupported"] == 2  # claim 1 was skipped


def test_g4_rules() -> None:
    assert not check_sensitivity(names_private_individual=True).ok
    assert check_sensitivity(names_private_individual=False).ok
    assert is_sensitive(["crime"]) and is_sensitive(["allegation_against_named_person"])
    assert not is_sensitive([])
