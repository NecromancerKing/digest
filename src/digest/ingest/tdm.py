"""Text-and-data-mining reservations, per the TDMRep convention (rule L4).

A rightsholder can reserve TDM rights in three machine-readable places, all checked before
an article page is kept:

1. Site-wide: ``/.well-known/tdmrep.json``, a list of ``{"location", "tdm-reservation"}``
   rules matched against the URL path in order, first match wins.
2. The HTTP response header ``tdm-reservation: 1``.
3. The HTML ``<meta name="tdm-reservation" content="1">``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, cast

_META_RE = re.compile(
    rb"""<meta\b[^>]*\bname\s*=\s*["']?tdm-reservation["']?[^>]*>""", re.IGNORECASE
)
_CONTENT_RE = re.compile(rb"""\bcontent\s*=\s*["']?\s*([01])""", re.IGNORECASE)
_HEAD_LIMIT = 200_000  # the meta tag belongs in <head>; don't scan whole pages


class TdmRepError(ValueError):
    """``tdmrep.json`` exists but cannot be understood; treated as a reservation."""


def parse_tdmrep(text: str) -> list[dict[str, Any]]:
    try:
        doc: object = json.loads(text) if text.strip() else []
    except json.JSONDecodeError as e:
        raise TdmRepError("invalid JSON") from e
    if not isinstance(doc, list):
        raise TdmRepError("not a list")
    rules = cast(list[object], doc)
    if not all(isinstance(r, dict) for r in rules):
        raise TdmRepError("rules must be objects")
    return cast(list[dict[str, Any]], rules)


def _location_matches(pattern: str, path: str) -> bool:
    if not pattern.startswith("/"):
        pattern = "/" + pattern
    regex = "".join(".*" if c == "*" else re.escape(c) for c in pattern.rstrip("$"))
    return re.match(regex + ("$" if pattern.endswith("$") else ""), path) is not None


def reserved_by_tdmrep(rules: list[dict[str, Any]], path: str) -> bool:
    """True if the first rule whose ``location`` matches ``path`` reserves TDM rights."""
    for rule in rules:
        location = rule.get("location")
        if isinstance(location, str) and _location_matches(location, path or "/"):
            return str(rule.get("tdm-reservation", "0")).strip() == "1"
    return False


def reserved_by_headers(headers: Mapping[str, str]) -> bool:
    value = next((v for k, v in headers.items() if k.lower() == "tdm-reservation"), "")
    return value.strip() == "1"


def reserved_by_html(html: bytes) -> bool:
    head = html[:_HEAD_LIMIT]
    for tag in _META_RE.finditer(head):
        m = _CONTENT_RE.search(tag.group(0))
        if m and m.group(1) == b"1":
            return True
    return False
