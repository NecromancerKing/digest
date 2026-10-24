"""Known place names, so labels in sensitive clusters can name where without naming who.

A capitalised-phrase heuristic cannot tell a person from a place. In crime, accident and
court clusters, labels therefore use only phrases found here (rule L12): countries in
every language pycountry knows for our sources, Nordic counties and regions, and the
larger Swedish towns.
"""

from __future__ import annotations

import gettext
import re
from functools import cache

import pycountry

_LANGS = ("sv", "nb", "da", "fi")
_REGION_COUNTRIES = ("SE", "NO", "DK", "FI")
_SUFFIXES = re.compile(r"\s+(län|fylke|kommune|kommun|region|county)$", re.IGNORECASE)

# Larger Swedish towns (Swedish spelling, which English reports use too).
SWEDISH_TOWNS = (
    "Stockholm", "Göteborg", "Gothenburg", "Malmö", "Uppsala", "Västerås", "Örebro",
    "Linköping", "Helsingborg", "Jönköping", "Norrköping", "Lund", "Umeå", "Gävle",
    "Borås", "Södertälje", "Eskilstuna", "Halmstad", "Växjö", "Karlstad", "Sundsvall",
    "Östersund", "Trollhättan", "Luleå", "Borlänge", "Kristianstad", "Kalmar", "Falun",
    "Skövde", "Karlskrona", "Skellefteå", "Uddevalla", "Varberg", "Visby", "Kiruna",
    "Nyköping", "Motala", "Lidköping", "Landskrona", "Trelleborg", "Ystad", "Örnsköldsvik",
    "Norrtälje", "Märsta", "Huddinge", "Nacka", "Solna", "Sollentuna", "Botkyrka",
    "Haninge", "Järfälla", "Täby", "Rinkeby", "Kista", "Rosengård", "Biskopsgården",
)  # fmt: skip


def _clean(name: str) -> str:
    name = re.sub(r"\s*\[[A-Z]{2}-\w+\]$", "", name)  # pycountry: "Skåne län [SE-12]"
    return _SUFFIXES.sub("", name.split(",")[0]).strip()


@cache
def _index() -> dict[str, str]:
    """Lowercased name in any language -> English display name."""
    out: dict[str, str] = {}
    translations = [
        gettext.translation("iso3166-1", pycountry.LOCALES_DIR, languages=[lang], fallback=True)
        for lang in _LANGS
    ]
    for c in pycountry.countries:
        english = getattr(c, "common_name", None) or c.name
        names = {english, c.name, *(t.gettext(c.name) for t in translations)}
        for n in names:
            out.setdefault(_clean(n).lower(), _clean(english))
    for code in _REGION_COUNTRIES:
        for sub in pycountry.subdivisions.get(country_code=code) or []:
            n = _clean(sub.name)
            out.setdefault(n.lower(), n)
    for town in SWEDISH_TOWNS:
        out.setdefault(town.lower(), town)
    return out


@cache
def _local_names(lang: str) -> dict[str, str]:
    """English country name -> name in ``lang`` (Swedish edition)."""
    if lang == "en":
        return {}
    t = gettext.translation("iso3166-1", pycountry.LOCALES_DIR, languages=[lang], fallback=True)
    out: dict[str, str] = {}
    for c in pycountry.countries:
        english = _clean(getattr(c, "common_name", None) or c.name)
        out[english] = _clean(t.gettext(c.name))
    return out


def place_name(phrase: str, lang: str = "en") -> str | None:
    """The display name of a known place, in the edition language; None if not a place."""
    english = _index().get(phrase.strip().lower())
    if english is None:
        return None
    return _local_names(lang).get(english, english)
