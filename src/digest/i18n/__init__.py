"""Interface strings per edition language (spec section 10): ``i18n/<lang>.yaml``.

Keys are dotted paths (``links.flag.crime``). A missing key in a non-English file falls
back to English, so a half-translated edition still renders.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

import yaml

DIR = Path(__file__).resolve().parent


@cache
def _strings(lang: str) -> dict[str, str]:
    path = DIR / f"{lang}.yaml"
    if not path.exists():
        return {}
    out: dict[str, str] = {}

    def walk(prefix: str, node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():  # pyright: ignore[reportUnknownVariableType]
                walk(f"{prefix}{k}.", v)
        else:
            out[prefix.rstrip(".")] = str(node)

    walk("", yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    return out


def t(lang: str, key: str) -> str:
    """The string for ``key`` in ``lang``, else English. Raises ``KeyError`` if neither has it."""
    strings = _strings(lang)
    if key in strings:
        return strings[key]
    return _strings("en")[key]


def strings(lang: str) -> dict[str, str]:
    """Every string for ``lang``, English filling the gaps."""
    return {**_strings("en"), **_strings(lang)}
