"""Jinja templates for template stories: one file per data source and edition language.

The environment is sandboxed and renders plain text. Values come from the data, but the
data is still untrusted, so it is only ever substituted, never evaluated.
"""

from __future__ import annotations

import datetime as dt
from functools import cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from jinja2 import FileSystemLoader, StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

from digest.i18n import t

TEMPLATES = Path(__file__).resolve().parent / "templates"
SV_MONTHS = (
    "jan",
    "feb",
    "mars",
    "april",
    "maj",
    "juni",
    "juli",
    "aug",
    "sep",
    "okt",
    "nov",
    "dec",
)


@cache
def _env() -> SandboxedEnvironment:
    return SandboxedEnvironment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=False,
        undefined=StrictUndefined,
        keep_trailing_newline=False,
    )


def render(template: str, lang: str, macro: str, ctx: dict[str, Any]) -> str:
    """Call ``macro`` in ``<template>.<lang>.j2`` with ``ctx``; whitespace collapsed."""
    module: Any = _env().get_template(f"{template}.{lang}.j2").module
    text = str(getattr(module, macro)(ctx))
    return " ".join(text.split())


def join_list(items: list[str], lang: str) -> str:
    """``A, B and C`` (English) or ``A, B och C`` (Swedish)."""
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} {t(lang, 'words.and')} {items[-1]}"


def local_time(at: dt.datetime, zone: ZoneInfo, lang: str) -> str:
    """``25 Oct 11:00`` (English) or ``25 okt kl. 11.00`` (Swedish), in local time."""
    local = at.astimezone(zone)
    if lang == "sv":
        return f"{local.day} {SV_MONTHS[local.month - 1]} kl. {local:%H.%M}"
    return f"{local.day} {local:%b %H:%M}"
