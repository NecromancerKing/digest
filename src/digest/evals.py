"""LLM evaluation harness (spec section 14): ``digest eval`` / ``uv run poe eval``.

The golden set in ``evals/golden/*.yaml`` holds invented news clusters written for this
project (no third-party text). Each cluster has source documents, gold facts in our words,
facts a good story must contain, things it must not invent, private names that must never
appear, and claims labelled supported or unsupported for testing the grader.

Tasks, each run per model with that model pinned:

- ``extract``: Prompt A on the documents. Valid output, every item gets facts, the facts
  mention what they must, sensitivity flags match.
- ``synthesize``: Prompt B on the gold facts. Gates G1 (length) and G2 (originality),
  coverage, inventions, private names, and G3 grounding graded by another model; optional
  LLM-graded readability.
- ``verify``: Prompt C on the labelled claims. How many known-unsupported claims it
  catches (G3 must catch at least 90%) and how many supported ones it accepts.

Every answer is cached under ``evals/.cache`` by task, model, prompt version and cluster,
so a run that hits a daily quota stops that model and the next run resumes. Results go to
``evals/results/<date>.json`` with a comparison against the previous file: a metric that
got worse by more than 2 points means "don't ship" (section 14).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import statistics
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from digest.config import Config, Settings
from digest.gates.checks import check_grounding, check_schema
from digest.gates.originality import SourceText, check_originality
from digest.llm.client import LLMClient, LLMUnavailable
from digest.llm.prompts import (
    FactLine,
    SourceDoc,
    SynthInput,
    extract_prompt,
    readability_prompt,
    synthesize_prompt,
    verify_prompt,
)
from digest.llm.schemas import GradeClaims, Readability, RecordFacts, WriteStory
from digest.llm.stage import ORIGINALITY_FEEDBACK

log = logging.getLogger(__name__)

GOLDEN_DIR = Path("evals/golden")
CACHE_DIR = Path("evals/.cache")
RESULTS_DIR = Path("evals/results")
TASKS = ("extract", "synthesize", "verify")
REGRESSION_POINTS = 2.0
RAW_METRICS = frozenset({"tokens", "words", "readability", "attempts"})  # averages, not percentages
GRADER_CATCH_TARGET = 0.90
MAX_CONSECUTIVE_ERRORS = 3


# --------------------------------------------------------------------------- golden set


class _G(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GoldDoc(_G):
    id: int
    source: str
    lang: str
    policy: str = "facts_only"
    title: str
    summary: str


class GoldFact(_G):
    id: str = Field(pattern=r"^\d+:f\d+$")
    statement: str
    attribution: str


class GoldClaim(_G):
    text: str
    fact_ids: list[str]
    label: str = Field(pattern=r"^(supported|unsupported)$")


class GoldCluster(_G):
    id: str
    category: str
    sensitivity: list[str] = []
    docs: list[GoldDoc] = Field(min_length=1)
    facts: list[GoldFact] = Field(min_length=2)
    must_contain: list[str] = Field(min_length=1)
    must_not: list[str] = []
    private_names: list[str] = []
    claims: list[GoldClaim] = Field(min_length=2)

    def fact_lines(self) -> list[FactLine]:
        names = {d.id: d.source for d in self.docs}
        return [
            FactLine(
                int(f.id.split(":")[0]),
                names[int(f.id.split(":")[0])],
                f.id,
                f.statement,
                f.attribution,
            )
            for f in self.facts
        ]

    def source_docs(self) -> list[SourceDoc]:
        return [
            SourceDoc(
                d.id, d.source, d.lang, "2026-10-20T08:00:00Z", d.title, d.summary, None, d.policy
            )
            for d in self.docs
        ]


def load_golden(directory: Path = GOLDEN_DIR) -> list[GoldCluster]:
    out: list[GoldCluster] = []
    for path in sorted(directory.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        out += [GoldCluster.model_validate(c) for c in raw]
    ids = [c.id for c in out]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate golden cluster ids")
    for c in out:
        known = {f.id for f in c.facts}
        doc_ids = {d.id for d in c.docs}
        bad = [f.id for f in c.facts if int(f.id.split(":")[0]) not in doc_ids]
        bad += [i for cl in c.claims for i in cl.fact_ids if i not in known]
        if bad:
            raise ValueError(f"{c.id}: unknown ids {bad}")
    return out


# --------------------------------------------------------------------------- cache


class Cache:
    """One JSON line per answer: ``{"key": ..., "ok": bool, "value": ..., "tokens": n}``."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.rows: dict[str, dict[str, Any]] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    self.rows[row["key"]] = row

    def get(self, key: str) -> dict[str, Any] | None:
        return self.rows.get(key)

    def put(self, key: str, row: dict[str, Any]) -> None:
        row = {"key": key, **row}
        self.rows[key] = row
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- scoring


_THOUSANDS = re.compile(r"(?<=\d)[, " + chr(0xA0) + chr(0x202F) + r"](?=\d{3}\b)")
_DASHES = re.compile("[" + chr(0x2010) + "-" + chr(0x2015) + chr(0x2212) + "]")


def _norm(text: str) -> str:
    """Lower case, "2,000" and "2 000" as "2000", every dash as "-"."""
    return _DASHES.sub("-", _THOUSANDS.sub("", text.lower()))


def mentions(text: str, needle: str) -> bool:
    return _norm(needle) in _norm(text)


@dataclass
class Tally:
    """Per model and task: counts that become rates."""

    n: int = 0
    values: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))

    def add(self, **metrics: float | bool) -> None:
        for k, v in metrics.items():
            self.values[k].append(float(v))

    def summary(self) -> dict[str, float]:
        """Rates as percentages; raw averages for tokens, words and readability."""
        return {
            k: round(statistics.fmean(v) * (1 if k in RAW_METRICS else 100), 1)
            for k, v in self.values.items()
            if v
        }


def score_extract(c: GoldCluster, out: RecordFacts) -> dict[str, float | bool]:
    doc_ids = {d.id for d in c.docs}
    with_facts = {i.item_id for i in out.items if i.facts and i.item_id in doc_ids}
    text = " ".join(f.statement for i in out.items for f in i.facts)
    found = [mentions(text, m) for m in c.must_contain]
    return {
        "items_covered": len(with_facts) / len(doc_ids),
        "keyword_recall": sum(found) / len(found),
        "sensitivity_match": set(out.sensitivity.flags()) == set(c.sensitivity),
        "category_match": out.category == c.category,
    }


def score_story(
    c: GoldCluster, story: WriteStory, *, words: tuple[int, int], lang: str, embed: Any = None
) -> dict[str, float | bool]:
    text = " ".join(x for x in (story.headline, story.summary, story.why_it_matters) if x)
    g1 = check_schema(
        story, {f.id for f in c.facts}, words_min=words[0], words_max=words[1],
        headline_max=70, why_max_words=25,
    )  # fmt: skip
    g2 = check_originality(
        story.headline,
        " ".join(x for x in (story.summary, story.why_it_matters) if x),
        [SourceText(d.title, d.summary, None, d.lang, d.policy) for d in c.docs],
        edition_lang=lang,
        embed=embed,
    )
    found = [mentions(text, m) for m in c.must_contain]
    return {
        "length_ok": g1.ok,
        "originality_ok": g2.ok,
        "coverage": sum(found) / len(found),
        "no_inventions": not any(mentions(text, m) for m in c.must_not),
        "no_private_names": not any(mentions(text, n) for n in c.private_names),
        "words": len(story.summary.split()),
    }


def score_grades(c: GoldCluster, grades: GradeClaims) -> dict[str, float]:
    verdicts = {g.claim_index: g.verdict for g in grades.results}
    unsup = [i for i, cl in enumerate(c.claims) if cl.label == "unsupported"]
    sup = [i for i, cl in enumerate(c.claims) if cl.label == "supported"]
    out: dict[str, float] = {}
    if unsup:
        out["catch_unsupported"] = sum(verdicts.get(i) == "unsupported" for i in unsup) / len(unsup)
    if sup:
        out["accept_supported"] = sum(verdicts.get(i) == "supported" for i in sup) / len(sup)
    return out


# --------------------------------------------------------------------------- the run


@dataclass(frozen=True)
class EvalOptions:
    tasks: Sequence[str] = TASKS
    models: Sequence[str] | None = None  # provider/model keys; default every eligible one
    limit: int | None = None  # clusters per task and model (Gemini allows 20 calls a day)
    words: tuple[int, int] = (30, 60)
    readability: bool = False


def _key(task: str, model: str, version: str, cluster: str, words: tuple[int, int]) -> str:
    suffix = f"|{words[0]}-{words[1]}" if task == "synthesize" else ""
    return f"{task}|{model}|{version}|{cluster}{suffix}"


class Evaluator:
    def __init__(
        self,
        client: LLMClient,
        config: Config,
        settings: Settings,
        cache: Cache,
        *,
        embed: Any = None,
        progress: Callable[[str], None] = lambda _m: None,
        cache_only: bool = False,
    ) -> None:
        self.cache_only = cache_only  # report from cached answers; never call a model
        self.client = client
        self.config = config
        self.settings = settings
        self.cache = cache
        self.embed = embed
        self.progress = progress
        self.paused: set[str] = set()  # models out of quota in this run
        self.only: Sequence[str] | None = None  # the run's --model choices
        self.errors: dict[str, int] = defaultdict(int)  # consecutive provider errors

    def models_for(self, role: str, only: Sequence[str] | None) -> list[str]:
        keys = [c.key for c in self.client.candidates(role)]  # pyright: ignore[reportArgumentType]
        return [k for k in keys if only is None or k in only]

    def _call(
        self, key: str, model: str, prompt: Any, schema: type[BaseModel]
    ) -> dict[str, Any] | None:
        """Cached answer, or a fresh call; None when the model is out of quota."""
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        if model in self.paused or self.cache_only:
            return None
        try:
            result = self.client.call(prompt, schema, pin=model)
        except LLMUnavailable as e:
            if e.reason == "no_quota":
                self.paused.add(model)
                self.progress(f"{model}: no quota left; pausing it for this run")
                return None
            if e.reason == "error":  # not cached: may work next time
                self.errors[model] += 1
                if self.errors[model] >= MAX_CONSECUTIVE_ERRORS:
                    self.paused.add(model)
                    self.progress(f"{model}: {self.errors[model]} errors in a row; pausing it")
                return None
            row = {"ok": False, "reason": e.reason, "tokens": 0}
        else:
            self.errors[model] = 0
            u = result.usage
            row = {
                "ok": True,
                "value": result.value.model_dump(),
                "tokens": (u.input_tokens or 0) + (u.output_tokens or 0),
            }
        self.cache.put(key, row)
        return row

    def _grader_for(self, writer: str) -> str | None:
        """The best verify model other than the writer, for G3 and readability: one of the
        run's ``--model`` choices if possible, else any other with quota."""
        others = [
            k for k in self.models_for("verify", None) if k != writer and k not in self.paused
        ]
        preferred = [k for k in others if self.only is None or k in self.only]
        return (preferred or others or [None])[0]

    def run(self, clusters: Sequence[GoldCluster], opts: EvalOptions) -> dict[str, Any]:
        lang = self.settings.edition_lang
        cats = [c.id for c in self.config.categories]
        tallies: dict[tuple[str, str], Tally] = defaultdict(Tally)
        missing: dict[tuple[str, str], int] = defaultdict(int)
        self.only = opts.models
        chosen = list(clusters[: opts.limit] if opts.limit else clusters)
        plan = {t: self.models_for(t, opts.models) for t in opts.tasks}
        for i, c in enumerate(chosen, 1):
            self.progress(f"[{i}/{len(chosen)}] {c.id}")
            for model in plan.get("extract", []):
                p = extract_prompt(c.source_docs(), lang=lang, categories=cats)
                row = self._call(
                    _key("extract", model, p.version, c.id, opts.words), model, p, RecordFacts
                )
                t = tallies[("extract", model)]
                if row is None:
                    missing[("extract", model)] += 1
                elif not row["ok"]:
                    t.add(valid=False, tokens=row["tokens"])
                else:
                    t.add(
                        valid=True,
                        tokens=row["tokens"],
                        **score_extract(c, RecordFacts.model_validate(row["value"])),
                    )
            for model in plan.get("synthesize", []):
                self._synth(c, model, opts, tallies, missing, lang)
            for model in plan.get("verify", []):
                facts = c.fact_lines()
                claims = [{"text": cl.text, "fact_ids": cl.fact_ids} for cl in c.claims]
                p = verify_prompt(claims, facts)
                row = self._call(
                    _key("verify", model, p.version, c.id, opts.words), model, p, GradeClaims
                )
                t = tallies[("verify", model)]
                if row is None:
                    missing[("verify", model)] += 1
                elif not row["ok"]:
                    t.add(valid=False)
                else:
                    t.add(
                        valid=True,
                        tokens=row["tokens"],
                        **score_grades(c, GradeClaims.model_validate(row["value"])),
                    )
        return report(tallies, missing, opts, len(chosen))

    def _synth(
        self,
        c: GoldCluster,
        model: str,
        opts: EvalOptions,
        tallies: dict[tuple[str, str], Tally],
        missing: dict[tuple[str, str], int],
        lang: str,
    ) -> None:
        """Synthesis as production runs it: up to ``max_retries`` retries on G1/G2 feedback."""
        facts = c.fact_lines()
        retries = self.config.settings.synthesis.max_retries
        feedback: str | None = None
        forbidden: list[str] = []
        t = tallies[("synthesize", model)]
        story: WriteStory | None = None
        key = ""
        tokens = 0
        first: dict[str, float | bool] = {}
        scores: dict[str, float | bool] = {}
        attempt = 0
        for attempt in range(1 + retries):
            p = synthesize_prompt(
                SynthInput(
                    facts=facts, sources=len({d.source for d in c.docs}), lang=lang,
                    words_min=opts.words[0], words_max=opts.words[1], headline_max=70,
                    why_max_words=25, category=c.category, gate_feedback=feedback,
                    forbidden_phrases=forbidden,
                )
            )  # fmt: skip
            key = _key("synthesize", model, p.version, c.id, opts.words) + f"|a{attempt}"
            row = self._call(key, model, p, WriteStory)
            if row is None:
                missing[("synthesize", model)] += 1
                return
            tokens += row["tokens"]
            if not row["ok"]:
                t.add(valid=False, tokens=tokens)
                return
            story = WriteStory.model_validate(row["value"])
            scores = score_story(c, story, words=opts.words, lang=lang, embed=self.embed)
            if attempt == 0:
                first = scores
            if scores["length_ok"] and scores["originality_ok"]:
                break
            feedback, forbidden = self._feedback(c, story, opts, lang, forbidden)
        assert story is not None
        t.add(
            valid=True,
            tokens=tokens,
            length_ok_first=first["length_ok"],
            originality_ok_first=first["originality_ok"],
            passed_within_retries=bool(scores["length_ok"] and scores["originality_ok"]),
            attempts=attempt + 1,
            **scores,
        )
        grader = self._grader_for(model)
        if grader is None:
            return
        claims = [cl.model_dump() for cl in story.claims]
        vp = verify_prompt(claims, facts)
        graded = self._call(f"g3|{grader}|{vp.version}|{key}", grader, vp, GradeClaims)
        if graded is not None and graded["ok"]:
            t.add(grounded=check_grounding(story, GradeClaims.model_validate(graded["value"])).ok)
        if opts.readability:
            rp = readability_prompt(story.headline, story.summary)
            read = self._call(f"read|{grader}|{rp.version}|{key}", grader, rp, Readability)
            if read is not None and read["ok"]:
                t.add(readability=Readability.model_validate(read["value"]).score)

    def _feedback(
        self, c: GoldCluster, story: WriteStory, opts: EvalOptions, lang: str, forbidden: list[str]
    ) -> tuple[str, list[str]]:
        """The same retry feedback the pipeline gives (llm.stage.write_story)."""
        g1 = check_schema(
            story, {f.id for f in c.facts}, words_min=opts.words[0], words_max=opts.words[1],
            headline_max=70, why_max_words=25,
        )  # fmt: skip
        if not g1.ok:
            return g1.feedback or "the draft failed the schema check", forbidden
        g2 = check_originality(
            story.headline,
            " ".join(x for x in (story.summary, story.why_it_matters) if x),
            [SourceText(d.title, d.summary, None, d.lang, d.policy) for d in c.docs],
            edition_lang=lang,
            embed=self.embed,
        )
        return ORIGINALITY_FEEDBACK.get(g2.reason or "", "it was not original"), sorted(
            set(forbidden) | set(g2.phrases)
        )


# --------------------------------------------------------------------------- report


def report(
    tallies: dict[tuple[str, str], Tally],
    missing: dict[tuple[str, str], int],
    opts: EvalOptions,
    n_clusters: int,
) -> dict[str, Any]:
    results: dict[str, dict[str, Any]] = defaultdict(dict)
    for (task, model), t in sorted(tallies.items()):
        results[task][model] = {**t.summary(), "missing": missing.get((task, model), 0)}
    for (task, model), n in missing.items():
        results[task].setdefault(model, {"missing": n})
    return {
        "date": dt.date.today().isoformat(),
        "clusters": n_clusters,
        "words": list(opts.words),
        "results": dict(results),
        "recommendations": recommend(results),
    }


SYNTH_PARTS = (
    "valid",
    "length_ok",
    "originality_ok",
    "originality_ok_first",
    "coverage",
    "no_inventions",
    "no_private_names",
    "grounded",
)


def recommend(results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Quality order from synthesis scores, extract_ok from extraction, grader verdicts."""
    synth = {
        m: statistics.fmean(r[k] for k in SYNTH_PARTS if k in r)
        for m, r in results.get("synthesize", {}).items()
        if any(k in r for k in SYNTH_PARTS)
    }
    order = sorted(synth, key=lambda m: -synth[m])
    extract_ok = {
        m: r.get("valid", 0) >= 95
        and r.get("keyword_recall", 0) >= 85
        and r.get("items_covered", 0) >= 95
        for m, r in results.get("extract", {}).items()
        if "valid" in r
    }
    graders = {
        m: r.get("catch_unsupported", 0) >= GRADER_CATCH_TARGET * 100
        for m, r in results.get("verify", {}).items()
        if "catch_unsupported" in r
    }
    return {
        "synthesis_score": {m: round(s, 1) for m, s in synth.items()},
        "quality_order": order,
        "extract_ok": extract_ok,
        "grader_meets_target": graders,
    }


def regressions(new: dict[str, Any], old: dict[str, Any] | None) -> list[str]:
    """Metrics that dropped by more than ``REGRESSION_POINTS`` for the same task and model."""
    if not old:
        return []
    out: list[str] = []
    for task, models in new.get("results", {}).items():
        for model, metrics in models.items():
            before = old.get("results", {}).get(task, {}).get(model, {})
            for k, v in metrics.items():
                if k == "missing" or k in RAW_METRICS or k not in before:
                    continue
                if isinstance(v, int | float) and before[k] - v > REGRESSION_POINTS:
                    out.append(f"{task} {model} {k}: {before[k]} -> {v}")
    return out


def latest_result(
    directory: Path = RESULTS_DIR, before: str | None = None
) -> dict[str, Any] | None:
    files = sorted(p for p in directory.glob("*.json") if before is None or p.stem < before)
    return json.loads(files[-1].read_text(encoding="utf-8")) if files else None


def render(result: dict[str, Any], regress: Iterable[str]) -> str:
    lines = [
        f"Eval {result['date']}: {result['clusters']} clusters, "
        f"summary {result['words'][0]}-{result['words'][1]} words"
    ]
    for task, models in result["results"].items():
        lines.append(f"\n{task}:")
        for model, metrics in models.items():
            body = ", ".join(f"{k} {v}" for k, v in metrics.items())
            lines.append(f"  {model}: {body}")
    rec = result["recommendations"]
    lines.append(f"\nquality order (best first): {', '.join(rec['quality_order']) or 'n/a'}")
    lines.append(f"extract_ok: {rec['extract_ok']}")
    lines.append(f"grader catches >= 90% of unsupported claims: {rec['grader_meets_target']}")
    regress = list(regress)
    lines.append("\nregressions over 2 points: " + ("; ".join(regress) if regress else "none"))
    return "\n".join(lines)
