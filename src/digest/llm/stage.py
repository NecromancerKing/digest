"""Stages 5-7 in ``SUMMARY_MODE=llm`` (spec section 8): extract, synthesize, verify.

Clusters are handled in rank order, top stories first, and no new cluster is started once
the run's time budget is nearly spent. Facts are extracted lazily: only for selected
clusters, once per item. Nobody reviews stories (D19), so a story is published when
G1-G5 pass and never otherwise; the links card keeps showing in its place. Sensitive
clusters (crime, death, court and the like) are summarised like any other (D28):

- G1, G2, G3 or G4 still failing after the retries: nothing is written. The cluster is
  not tried again until it has new facts or the synthesis prompt changes.
- No quota, or no other model to check the claims (G3): the cluster is retried in the
  next run.
- A G5 duplicate is written as ``needs_review``: held back, never shown.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import numpy as np
from numpy.typing import NDArray
from sqlalchemy import Engine, select

from digest.config import Config, Settings, Source
from digest.db.schema import clusters, items, stories, story_revisions
from digest.gates.checks import (
    GateOutcome,
    check_grounding,
    check_schema,
    graded_claims,
)
from digest.gates.duplicate import find_duplicate
from digest.gates.originality import SourceText, check_originality
from digest.links.cards import Member, source_lines
from digest.links.stage import load_members, recent_story_vectors
from digest.llm.client import InputTooLarge, LLMClient, LLMUnavailable
from digest.llm.prompts import (
    SYNTH_VERSION,
    CategoryHint,
    FactLine,
    SourceDoc,
    SynthInput,
    split_for_extract,
    synthesize_prompt,
    verify_prompt,
)
from digest.llm.schemas import GradeClaims, RecordFacts, WriteStory
from digest.nlp.vectors import Vector, from_blob
from digest.stories import Revision, add_revision, create_story, find_story, is_frozen
from digest.timeutil import iso, parse_iso

log = logging.getLogger(__name__)

EmbedTexts = Callable[[Sequence[str]], NDArray[np.float32]]
# Fetches article text for items about to be read (D28); returns outcomes by name.
FetchArticles = Callable[[Sequence[int]], Counter[str]]
MIN_SECONDS_PER_CLUSTER = 90
ORIGINALITY_FEEDBACK = {  # G2 retry feedback, in our words only (rule L7)
    "shared_run": "it reused wording from a source",
    "headline_echo": "the headline was too close to a source headline; word it differently",
    "near_translation": "the summary was a near-translation of one source",
}  # a cluster can take several calls; don't start one without room


class Budget(Protocol):
    def remaining(self) -> float: ...


@dataclass
class Target:
    id: int
    category: str
    score: float
    centroid: Vector | None
    extraction: dict[str, Any] | None
    members: list[Member] = field(default_factory=lambda: [])
    dropped_on: str | None = None


# --------------------------------------------------------------------------- loading


def _targets(engine: Engine, config: Config) -> list[Target]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(clusters)
            .where(
                clusters.c.is_open == 1,
                clusters.c.selected_at.is_not(None),
                clusters.c.category.is_not(None),
            )
            .order_by(clusters.c.score.desc(), clusters.c.id)
        ).all()
        members = load_members(conn, config, [r.id for r in rows])
    return [
        Target(
            id=r.id,
            category=r.category,
            score=r.score,
            centroid=from_blob(r.centroid) if r.centroid else None,
            extraction=json.loads(r.extraction) if r.extraction else None,
            members=members.get(r.id, []),
            dropped_on=r.dropped_on,
        )
        for r in rows
    ]


def _item_rows(engine: Engine, item_ids: Sequence[int]) -> dict[int, Any]:
    if not item_ids:
        return {}
    with engine.connect() as conn:
        return {
            r.id: r
            for r in conn.execute(
                select(items.c.id, items.c.status, items.c.facts, items.c.full_text).where(
                    items.c.id.in_(item_ids)
                )
            )
        }


# --------------------------------------------------------------------------- stage 5


def category_hints(config: Config) -> list[CategoryHint]:
    return [CategoryHint(c.id, c.kind, c.hint) for c in config.categories]


def _merge_extraction(
    old: dict[str, Any] | None, new: RecordFacts, version: str, model: str
) -> dict[str, Any]:
    out: dict[str, Any] = dict(old or {})
    out.setdefault("event", new.event)
    out.setdefault("category", new.category)
    ents: dict[str, list[Any]] = out.get("entities") or {"people": [], "orgs": [], "places": []}
    for key in ("orgs", "places"):
        ents[key] = sorted(set(ents.get(key, [])) | set(getattr(new.entities, key)))
    known = {p["name"] for p in ents.get("people", [])}
    ents["people"] = [
        *ents.get("people", []),
        *(p.model_dump() for p in new.entities.people if p.name not in known),
    ]
    out["entities"] = ents
    flags = set(out.get("sensitivity", [])) | set(new.sensitivity.flags())
    out["sensitivity"] = sorted(flags)
    out["prompt_version"], out["model"] = version, model
    return out


def extract_cluster(
    engine: Engine, client: LLMClient, config: Config, settings: Settings, t: Target
) -> str:
    """Facts for the cluster's items that have none yet. Returns ok | none | failed | no_quota."""
    rows = _item_rows(engine, [m.item_id for m in t.members])
    todo = [
        m
        for m in t.members
        if m.usage_policy != "link_only" and rows[m.item_id].status == "clustered"
    ]
    if not todo:
        return "none"
    docs = [
        SourceDoc(
            item_id=m.item_id,
            source_name=m.source_name,
            lang=m.lang,
            published_at=iso(m.published_at),
            title=m.title,
            feed_summary=m.feed_summary,
            full_text=rows[m.item_id].full_text,
            usage_policy=m.usage_policy,
        )
        for m in todo
    ]
    prompts = split_for_extract(
        docs,
        lang=settings.edition_lang,
        categories=category_hints(config),
        max_tokens=settings.llm_max_input_tokens,
        full_text_chars=config.settings.fetch.article_text_max_chars,
    )
    for prompt in prompts:
        ids = list(prompt.item_ids)
        try:
            result = client.call(prompt, RecordFacts)
        except LLMUnavailable as e:
            if e.reason in ("invalid_output", "safety_block"):
                with engine.begin() as conn:
                    conn.execute(items.update().where(items.c.id.in_(ids)).values(status="failed"))
                return "failed"
            return "no_quota"
        except InputTooLarge:
            with engine.begin() as conn:
                conn.execute(items.update().where(items.c.id.in_(ids)).values(status="failed"))
            return "failed"
        facts = {f.item_id: f.facts for f in result.value.items if f.item_id in ids}
        t.extraction = _merge_extraction(
            t.extraction, result.value, result.prompt_version, result.model
        )
        values: dict[str, Any] = {"extraction": json.dumps(t.extraction, ensure_ascii=False)}
        if t.extraction.get("category") in {c.id for c in config.categories}:
            values["category"] = t.category = t.extraction["category"]
        with engine.begin() as conn:  # facts and extraction together, batch by batch
            for item_id in ids:
                stored = [
                    {**f.model_dump(), "id": f"{item_id}:{f.id}"} for f in facts.get(item_id, [])
                ]
                conn.execute(
                    items.update()
                    .where(items.c.id == item_id, items.c.usage_policy != "link_only")
                    .values(facts=json.dumps(stored, ensure_ascii=False), status="extracted")
                )
            conn.execute(clusters.update().where(clusters.c.id == t.id).values(**values))
    return "ok"


# --------------------------------------------------------------------------- stage 6-7


def _fact_lines(engine: Engine, members: Sequence[Member]) -> list[FactLine]:
    rows = _item_rows(engine, [m.item_id for m in members])
    names = {m.item_id: m.source_name for m in members}
    out: list[FactLine] = []
    for m in members:
        raw = rows[m.item_id].facts if m.item_id in rows else None
        stored: list[dict[str, str]] = json.loads(raw) if raw else []
        reported = iso(m.published_at)
        for f in stored:
            out.append(
                FactLine(
                    m.item_id,
                    names[m.item_id],
                    f["id"],
                    f["statement"],
                    f["attribution"],
                    reported,
                )
            )
    return out


def _fingerprint(facts: Sequence[FactLine]) -> str:
    """The facts and synthesis prompt a story was written from: new facts or a new prompt
    give a dropped cluster another try."""
    basis = "\n".join([SYNTH_VERSION, *sorted(f.fact_id for f in facts)])
    return hashlib.sha256(basis.encode()).hexdigest()[:16]


def _needs_story(
    engine: Engine,
    t: Target,
    facts: Sequence[FactLine],
    update_after: dt.timedelta,
    now: dt.datetime,
) -> bool:
    if t.dropped_on == _fingerprint(facts):
        return False  # the same input would fail the same way: wait for new facts
    with engine.connect() as conn:
        story = find_story(conn, t.id)
        if story is None:
            return True
        if is_frozen(story):
            return False
        if story.current_kind != "llm":
            return story.current_kind != "owner_blurb"
        rev = conn.execute(
            select(story_revisions.c.sources, story_revisions.c.created_at).where(
                story_revisions.c.story_id == story.id,
                story_revisions.c.rev_no == story.current_rev,
            )
        ).one()
    if now - parse_iso(rev.created_at) < update_after:
        return False
    had = {s["source_id"] for s in json.loads(rev.sources)}
    with_facts = {f.item_id for f in facts}
    now_sources = {m.source_id for m in t.members if m.item_id in with_facts}
    return bool(now_sources - had)


def _independent(config: Config, members: Sequence[Member]) -> int:
    by_id: dict[str, Source] = {s.id: s for s in config.sources}
    return len(
        {
            by_id[m.source_id].independence_key if m.source_id in by_id else m.source_id
            for m in members
        }
    )


@dataclass
class Verdict:
    story: WriteStory | None = None
    provider: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    gates: dict[str, Any] = field(default_factory=lambda: {})
    attempts: int = 0
    outcome: str = "failed"  # approved | dropped | no_quota | no_grader
    failed_gate: str | None = None  # the last failure: synthesis | g1 | g2 | g3
    failed_reason: str | None = None  # a fixed code, e.g. G2's shared_run


def write_story(
    engine: Engine,
    client: LLMClient,
    config: Config,
    settings: Settings,
    t: Target,
    facts: Sequence[FactLine],
    *,
    strong: bool,
    embed: EmbedTexts | None,
    previous: dict[str, Any] | None,
) -> Verdict:
    """Synthesize, then gates G1-G3, with up to ``max_retries`` retries on feedback."""
    syn = config.settings.synthesis
    gates_cfg = config.settings.gates
    independent = _independent(config, t.members)
    fact_ids = {f.fact_id for f in facts}
    rows = _item_rows(engine, [m.item_id for m in t.members])
    texts = [
        SourceText(
            m.title,
            m.feed_summary,
            rows[m.item_id].full_text if m.item_id in rows else None,
            m.lang,
            m.usage_policy,
        )
        for m in t.members
    ]
    open_docs = [
        SourceDoc(
            m.item_id,
            m.source_name,
            m.lang,
            iso(m.published_at),
            m.title,
            m.feed_summary,
            None,
            m.usage_policy,
        )
        for m in t.members
        if m.usage_policy == "open_license" and m.template is None
    ]
    feedback: str | None = None
    forbidden: list[str] = []
    v = Verdict()
    for attempt in range(1 + syn.max_retries):
        v.attempts = attempt + 1
        prompt = synthesize_prompt(
            SynthInput(
                facts=facts,
                sources=independent,
                lang=settings.edition_lang,
                words_min=settings.summary_words_min,
                words_max=settings.summary_words_max,
                headline_max=syn.headline_max_chars,
                why_max_words=syn.why_it_matters_max_words,
                category=t.category,
                key_facts_max=syn.key_facts_max,
                open_docs=open_docs,
                previous=previous,
                gate_feedback=feedback,
                forbidden_phrases=forbidden,
            )
        )
        try:
            written = client.call(prompt, WriteStory, strong=strong)
        except LLMUnavailable as e:
            v.outcome = "no_quota" if e.reason in ("no_quota", "error") else "dropped"
            v.gates["synthesis"] = e.reason
            v.failed_gate, v.failed_reason = "synthesis", e.reason
            return v
        story = written.value
        v.story, v.provider, v.model, v.prompt_version = (
            story,
            written.provider,
            written.model,
            written.prompt_version,
        )
        g1 = check_schema(
            story,
            fact_ids,
            words_min=settings.summary_words_min,
            words_max=settings.summary_words_max,
            headline_max=syn.headline_max_chars,
            why_max_words=syn.why_it_matters_max_words,
            key_facts_max=syn.key_facts_max,
            key_fact_max_words=syn.key_fact_max_words,
        )
        v.gates["g1"] = "pass" if g1.ok else "fail"
        if not g1.ok:
            feedback = g1.feedback
            v.failed_gate, v.failed_reason = "g1", ",".join(g1.codes)
            continue
        g2 = check_originality(
            story.headline,
            " ".join(
                x
                for x in (story.summary, story.why_it_matters, *(k.text for k in story.key_facts))
                if x
            ),
            texts,
            edition_lang=settings.edition_lang,
            shared_run_words=gates_cfg.originality_shared_run_words,
            headline_jaccard_max=gates_cfg.originality_headline_jaccard,
            cross_lang_cosine=gates_cfg.originality_cross_lang_cosine,
            embed=embed,
        )
        v.gates["g2"] = "pass" if g2.ok else f"fail: {g2.reason}"
        v.gates["g2_cross_lang"] = "checked" if g2.cross_lang_checked else "skipped"
        if not g2.ok:
            forbidden = sorted(set(forbidden) | set(g2.phrases))
            feedback = ORIGINALITY_FEEDBACK.get(g2.reason or "", "it was not original")
            v.failed_gate, v.failed_reason = "g2", g2.reason
            continue
        claims = [c.model_dump() for c in graded_claims(story)]
        try:
            graded = client.call(
                verify_prompt(claims, facts), GradeClaims, avoid_model=written.model
            )
            g3: GateOutcome = check_grounding(story, graded.value)
            v.gates["g3"] = {
                **g3.detail,
                "result": "pass" if g3.ok else "fail",
                "model": graded.model,
            }
        except LLMUnavailable:
            # Nobody else checks the claims (D19): wait for a grader with quota.
            v.gates["g3"] = "not_checked"
            v.outcome, v.failed_gate, v.failed_reason = "no_grader", "g3", "no_grader"
            return v
        if not g3.ok:
            feedback = g3.feedback
            v.failed_gate, v.failed_reason = "g3", "unsupported"
            continue
        v.outcome = "approved"
        return v
    v.outcome = "dropped"  # a gate still failing: never published, the links card stays
    return v


def _store(
    engine: Engine,
    config: Config,
    settings: Settings,
    t: Target,
    v: Verdict,
    dup: int | None,
    now: dt.datetime,
    zone: ZoneInfo,
) -> bool:
    assert v.story is not None
    story_out = v.story
    lines = [
        {**s.as_dict(), "headline": None} for s in source_lines(t.members)
    ]  # never in llm mode
    verification = {**v.gates, "g5": dup or "pass", "attempts": v.attempts}
    rev = Revision(
        kind="llm",
        headline=story_out.headline,
        summary=story_out.summary,
        why_it_matters=story_out.why_it_matters,
        sources=lines,
        claims=[c.model_dump() for c in story_out.claims],
        key_facts=[k.text for k in story_out.key_facts],
        verification=verification,
        provider=v.provider,
        model=v.model,
        prompt_version=v.prompt_version,
    )
    status = "needs_review" if dup is not None else "approved"  # a duplicate is held back
    with engine.begin() as conn:
        story = find_story(conn, t.id)
        if story is None:
            story = create_story(
                conn,
                cluster_id=t.id,
                category=t.category,
                slug_basis=story_out.headline,
                lang=settings.edition_lang,
                rank_score=t.score,
                now=now,
                zone=zone,
            )
        return add_revision(
            conn, story, rev, status=status, now=now, category=t.category, rank_score=t.score
        )


def _previous(engine: Engine, cluster_id: int) -> dict[str, Any] | None:
    with engine.connect() as conn:
        story = find_story(conn, cluster_id)
        if story is None or story.current_kind != "llm":
            return None
        r = conn.execute(
            select(
                story_revisions.c.headline,
                story_revisions.c.summary,
                story_revisions.c.why_it_matters,
                story_revisions.c.key_facts,
            ).where(
                story_revisions.c.story_id == story.id,
                story_revisions.c.rev_no == story.current_rev,
            )
        ).one()
    return {
        "headline": r.headline,
        "summary": r.summary,
        "why_it_matters": r.why_it_matters,
        "key_facts": json.loads(r.key_facts) if r.key_facts else [],
    }


def run_llm(
    engine: Engine,
    client: LLMClient,
    config: Config,
    settings: Settings,
    budget: Budget,
    now: dt.datetime,
    zone: ZoneInfo,
    *,
    embed: EmbedTexts | None = None,
    articles: FetchArticles | None = None,
) -> dict[str, int]:
    stats = Counter[str]()
    targets = _targets(engine, config)
    top_ids = {t.id for t in targets[: config.settings.rank.top_stories]}
    update_after = dt.timedelta(minutes=config.settings.synthesis.update_min_minutes)
    with engine.connect() as conn:
        recent = recent_story_vectors(conn, now, config.settings.gates.duplicate_window_hours)
    quota_gone = False
    for t in targets:
        if budget.remaining() < MIN_SECONDS_PER_CLUSTER:
            stats["skipped_for_budget"] += 1
            continue
        if quota_gone:
            stats["skipped_no_quota"] += 1
            continue
        with engine.connect() as conn:
            story = find_story(conn, t.id)
        if story is not None and is_frozen(story):
            continue
        if articles is not None:
            for name, n in articles([m.item_id for m in t.members]).items():
                stats[f"articles_{name}"] += n
        outcome = extract_cluster(engine, client, config, settings, t)
        stats[f"extract_{outcome}"] += 1
        if outcome == "no_quota":
            quota_gone = True
            continue
        facts = _fact_lines(engine, t.members)
        if not facts or not _needs_story(engine, t, facts, update_after, now):
            continue
        v = write_story(
            engine,
            client,
            config,
            settings,
            t,
            facts,
            strong=t.id in top_ids,
            embed=embed,
            previous=_previous(engine, t.id),
        )
        stats[f"story_{v.outcome}"] += 1
        stats["synthesis_attempts"] += v.attempts
        if v.outcome in ("no_quota", "no_grader"):
            quota_gone = True
            continue
        if v.outcome == "dropped":
            log.info(
                "story_dropped",
                extra={
                    "cluster_id": t.id,
                    "gate": v.failed_gate,
                    "reason": v.failed_reason,
                    "attempts": v.attempts,
                },
            )
            if v.failed_reason != "invalid_output":  # a bad reply is luck; a gate is not
                with engine.begin() as conn:
                    conn.execute(
                        clusters.update()
                        .where(clusters.c.id == t.id)
                        .values(dropped_on=_fingerprint(facts))
                    )
        if v.outcome != "approved" or v.story is None:
            continue
        others = [(sid, vec) for sid, cid, vec in recent if cid != t.id]
        dup = find_duplicate(t.centroid, others, config.settings.gates.duplicate_cosine)
        if _store(engine, config, settings, t, v, dup, now, zone):
            stats["llm_revisions"] += 1
            if t.centroid is not None:
                with engine.connect() as conn:
                    sid = conn.execute(
                        select(stories.c.id).where(stories.c.cluster_id == t.id)
                    ).scalar_one()
                recent.append((sid, t.id, t.centroid))
    stats["llm_calls"] = client.calls
    return dict(stats)
