"""Scripted stand-ins for LLM providers: no network, deterministic answers.

``FakeProvider`` answers each request with a handler chosen by the schema name, and logs
every request so tests can check what was (and wasn't) sent.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from digest.config import LLMProviders
from digest.llm.adapters import Reply, Request, Usage

Handler = Callable[[Request], Reply]


def reply(obj: Any, tokens: int = 100) -> Reply:
    return Reply(json.dumps(obj), Usage(input_tokens=tokens, output_tokens=tokens // 2))


def item_ids(req: Request) -> list[int]:
    return [int(x) for x in re.findall(r'<source_document item_id="(\d+)"', req.user)]


def facts_for(req: Request, category: str = "world", **sensitivity: bool) -> Reply:
    """Two facts per document, in our words."""
    keys = ("crime", "court", "accident", "death", "minors", "health")
    sens: dict[str, bool] = {k: False for k in (*keys, "allegation_against_named_person")}
    sens.update(sensitivity)
    return reply(
        {
            "event": "A strong earthquake struck southern Japan.",
            "category": category,
            "items": [
                {
                    "item_id": i,
                    "facts": [
                        {"id": "f1",
                         "statement": "An earthquake of magnitude 6.8 struck southern Japan.",
                         "attribution": "agency", "numbers": [{"value": 6.8, "unit": "magnitude"}],
                         "when": "2026-10-24"},
                        {"id": "f2", "statement": "Officials reported no tsunami risk.",
                         "attribution": "officials", "numbers": [], "when": None},
                    ],
                }
                for i in item_ids(req)
            ],
            "entities": {"people": [], "orgs": ["Japan Meteorological Agency"],
                         "places": ["Japan"]},
            "sensitivity": sens,
        }
    )  # fmt: skip


def fact_ids(req: Request) -> list[str]:
    return re.findall(r'"fact_id": "(\d+:f\d+)"', req.user.replace("&quot;", '"'))


GOOD_SUMMARY = (
    "A magnitude 6.8 earthquake shook the south of Japan on Saturday, according to the national "
    "weather agency. Officials said there was no risk of a tsunami along the coast, and early "
    "reports listed no serious damage in the towns nearest the epicentre."
)


def story_for(
    req: Request,
    summary: str = GOOD_SUMMARY,
    headline: str = "Powerful quake shakes southern Japan",
    **extra: Any,
) -> Reply:
    ids = fact_ids(req)
    return reply(
        {
            "headline": headline,
            "summary": summary,
            "why_it_matters": None,
            "claims": [
                {"text": "A magnitude 6.8 earthquake shook southern Japan", "fact_ids": ids[:1]},
                {"text": "no risk of a tsunami", "fact_ids": ids[1:2] or ids[:1]},
            ],
            "names_private_individual": False,
            "confidence": "high",
            **extra,
        }
    )  # fmt: skip


def grades_for(req: Request, verdict: str = "supported") -> Reply:
    n = len(re.findall(r'"claim_index": \d+', req.user))
    return reply(
        {"results": [{"claim_index": i, "verdict": verdict, "reason": "ok"} for i in range(n)]}
    )


DEFAULT_HANDLERS: dict[str, Handler] = {
    "RecordFacts": facts_for,
    "WriteStory": story_for,
    "GradeClaims": grades_for,
}


@dataclass
class FakeProvider:
    handlers: dict[str, Handler] = field(default_factory=lambda: dict(DEFAULT_HANDLERS))
    requests: list[Request] = field(default_factory=lambda: [])
    by_model: dict[str, Handler] = field(default_factory=lambda: {})

    def send(self, req: Request) -> Reply:
        self.requests.append(req)
        handler = self.by_model.get(req.model.id) or self.handlers[req.schema_name]
        return handler(req)

    def models(self) -> list[str]:
        return [r.model.id for r in self.requests]


def providers(paid: bool = False, allow_training: bool = True) -> LLMProviders:
    """alpha: strong (4) and weak (1) models; beta: middle (3), may train; claude: paid."""
    raw: dict[str, Any] = {
        "allow_training_providers": allow_training,
        "providers": [
            {
                "id": "alpha",
                "adapter": "openai_compatible",
                "base_url": "https://alpha.example/v1",
                "api_key_env": "ALPHA_KEY",  # pragma: allowlist secret
                "trains_on_inputs": False,
                "models": [
                    {"id": "big", "quality": 4, "roles": ["synthesize", "verify", "extract"]},
                    {"id": "small", "quality": 1, "roles": ["extract", "verify"]},
                ],
            },
            {
                "id": "beta",
                "adapter": "openai_compatible",
                "base_url": "https://beta.example/v1",
                "api_key_env": "BETA_KEY",  # pragma: allowlist secret
                "trains_on_inputs": True,
                "models": [
                    {"id": "mid", "quality": 3, "roles": ["synthesize", "verify", "extract"]}
                ],
            },
            {
                "id": "claude",
                "adapter": "anthropic",
                "enabled": paid,
                "paid": True,
                "api_key_env": "CLAUDE_KEY",  # pragma: allowlist secret
                "trains_on_inputs": False,
                "models": [
                    {"id": "haiku", "quality": 5, "roles": ["synthesize", "verify", "extract"],
                     "price_input_per_mtok": 1.0, "price_output_per_mtok": 5.0},
                ],
            },
        ],
    }  # fmt: skip
    return LLMProviders.model_validate(raw)
