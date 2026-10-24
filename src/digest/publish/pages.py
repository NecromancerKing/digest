"""Facts for the static pages, read from config so the pages never drift from the system.

The pages' prose lives in ``templates/pages/<lang>/``; the lists here (sources, AI models,
schedule) are filled in from ``config/`` at build time.
"""

from __future__ import annotations

from dataclasses import dataclass

import pycountry

from digest.config import Config, Source

PAGES = ("about", "sources", "ai", "corrections", "privacy", "contact")
POLICY_ORDER = ("open_license", "facts_only", "link_only")


@dataclass(frozen=True)
class SourceRow:
    name: str
    homepage: str
    country: str
    language: str
    use: str  # data | open_license | facts_only | link_only


@dataclass(frozen=True)
class ModelRow:
    provider: str
    model: str
    roles: tuple[str, ...]
    trains_on_inputs: object  # False, True, "varies" or None (unknown)


def _country(code: str) -> str:
    c = pycountry.countries.get(alpha_2=code.upper())
    return getattr(c, "common_name", None) or (c.name if c else code)


def _language(code: str) -> str:
    lang = pycountry.languages.get(alpha_2=code.lower())
    return lang.name if lang else code


def _use(s: Source) -> str:
    return "data" if s.template else s.usage_policy


def source_rows(config: Config) -> list[SourceRow]:
    order = {"data": 0, **{p: i + 1 for i, p in enumerate(POLICY_ORDER)}}
    rows = [
        SourceRow(s.name, str(s.homepage), _country(s.country), _language(s.lang), _use(s))
        for s in config.enabled_sources
    ]
    return sorted(rows, key=lambda r: (order[r.use], r.name.lower()))


def model_rows(config: Config) -> list[ModelRow]:
    """The AI models the pipeline may call: enabled providers only."""
    return [
        ModelRow(p.id, m.id, tuple(m.roles), p.trains_on_inputs)
        for p in config.llm.providers
        if p.enabled
        for m in p.models
    ]


def country_count(config: Config) -> int:
    return len({s.country for s in config.enabled_sources})
