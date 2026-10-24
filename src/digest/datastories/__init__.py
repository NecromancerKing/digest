"""Template stories from open data (spec section 4): our wording, the data's facts.

A data source is a registry entry with ``template: <name>`` and a ``data_api`` feed.
Ingest hands that feed's body to the template's parser, which returns ordinary entries
(headline and summary already written from the template), so template stories cluster
with news about the same event. Every template story ends with a credit line.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from digest.config import TemplateSettings
from digest.i18n import t
from digest.ingest.feeds import Entry, FeedParseError


@dataclass(frozen=True)
class DataTemplate:
    parse: Callable[..., list[Entry]]
    credit_key: str  # i18n key of the credit line


def _registry() -> dict[str, DataTemplate]:
    from digest.datastories import smhi

    return {"smhi_warning": DataTemplate(smhi.parse_warnings, "templates.smhi_credit")}


def known_templates() -> frozenset[str]:
    return frozenset(_registry())


def credit_line(template: str, lang: str) -> str:
    """``Source: SMHI weather warnings`` for an SMHI story; ends with a full stop."""
    line = t(lang, _registry()[template].credit_key)
    return line if line.endswith(".") else line + "."


def parse_data_feed(
    template: str,
    content: bytes,
    *,
    lang: str,
    link: str,
    zone: ZoneInfo,
    settings: TemplateSettings,
) -> list[Entry]:
    tpl = _registry().get(template)
    if tpl is None:
        raise FeedParseError(f"unknown template {template}")
    return tpl.parse(content, lang=lang, link=link, zone=zone, settings=settings)
