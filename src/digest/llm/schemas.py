"""Output schemas of the three prompts (spec section 9): record_facts, write_story, grade_claims.

Every field is required and every object forbids extra keys, because strict structured
outputs (Groq, Anthropic) accept only that shape; optional values are nullable instead.
``strict_schema`` turns a model into the JSON Schema sent to providers.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------- Prompt A


class Number(_Out):
    value: float
    unit: str


class Fact(_Out):
    id: str = Field(pattern=r"^f\d+$")
    statement: str = Field(min_length=1)
    attribution: str
    numbers: list[Number]
    when: str | None


class ItemFacts(_Out):
    item_id: int
    facts: list[Fact]


class Person(_Out):
    name: str
    role: str | None
    public_figure: bool


class Entities(_Out):
    people: list[Person]
    orgs: list[str]
    places: list[str]


class Sensitivity(_Out):
    crime: bool
    court: bool
    accident: bool
    death: bool
    minors: bool
    health: bool
    allegation_against_named_person: bool

    def flags(self) -> list[str]:
        return [k for k, v in self.model_dump().items() if v]


class RecordFacts(_Out):
    event: str = Field(min_length=1)
    category: str  # checked against categories.yaml after parsing
    items: list[ItemFacts]
    entities: Entities
    sensitivity: Sensitivity


# --------------------------------------------------------------------------- Prompt B


class Claim(_Out):
    text: str = Field(min_length=1)
    fact_ids: list[str]


class WriteStory(_Out):
    headline: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    why_it_matters: str | None
    claims: list[Claim]
    names_private_individual: bool
    confidence: Literal["high", "medium", "low"]


# --------------------------------------------------------------------------- Prompt C


class Grade(_Out):
    claim_index: int = Field(ge=0)
    verdict: Literal["supported", "partial", "unsupported"]
    reason: str


class GradeClaims(_Out):
    results: list[Grade]


# --------------------------------------------------------------------------- JSON Schema


# Pydantic checks these after parsing; providers' schema subsets differ on them.
_DROPPED = frozenset({"title", "default", "pattern", "minLength", "minimum"})


def _tidy(node: Any) -> Any:
    """Drop titles, defaults and checks; make every object strict (all properties required)."""
    if isinstance(node, dict):
        out: dict[str, Any] = {
            k: _tidy(v)
            for k, v in node.items()  # pyright: ignore[reportUnknownVariableType]
            if k not in _DROPPED
        }
        if out.get("type") == "object" and "properties" in out:
            out["required"] = list(out["properties"])
            out["additionalProperties"] = False
        return out
    if isinstance(node, list):
        return [_tidy(v) for v in node]  # pyright: ignore[reportUnknownVariableType]
    return node


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """The JSON Schema for ``model`` in the strict subset providers accept."""
    return _tidy(model.model_json_schema())
