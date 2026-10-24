"""G4 sensitivity by keywords (spec section 8, links mode).

Links mode has no model to flag crime, accident or court stories, so a keyword list per
language does it. In flagged clusters, labels built from entities never include person
names (rule L12). The LLM path adds its own flags from stage 5 on top of these.

Matching depends on the language: English keywords match at the start of a word
("shoot" matches "shooting"), Nordic ones anywhere in a word, because those languages
write compounds as one word ("trafikolyckan", "bilbranden"); Nordic stems of four letters
or fewer match only at a word start.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

Flag = str  # crime | accident | court

# fmt: off
KEYWORDS: Mapping[str, Mapping[Flag, tuple[str, ...]]] = {
    "en": {
        "crime": (
            "murder", "homicide", "manslaughter", "shooting", "gunman", "stabb", "robber", "rape",
            "assault", "arrest", "suspect", "detained", "burglar", "theft", "kidnap", "abduct",
            "police say", "police said", "smuggl", "fraud",
        ),
        "accident": (
            "accident", "crash", "collision", "collided", "derail", "drown", "fire broke",
            "house fire", "blaze", "explosion", "killed in", "died in", "injured in",
        ),
        "court": (
            "court", "trial", "sentenced", "convicted", "acquitted", "charged with", "prosecutor",
            "verdict", "jailed", "indicted", "custody",
        ),
    },
    "sv": {
        "crime": (
            "mord", "dråp", "skjutning", "skottlossning", "knivhugg", "knivskur", "rån",
            "våldtäkt", "misshandel", "gripen", "gripits", "greps", "anhållen", "häktad",
            "häktning", "misstänk", "stöld", "inbrott", "kidnapp", "bedrägeri", "smuggl",
        ),
        "accident": (
            "olycka", "olyckan", "krock", "kollision", "drunkn", "brand", "omkom", "förolyck",
            "explosion", "urspår", "skadades", "avled",
        ),
        "court": (
            "domstol", "tingsrätt", "hovrätt", "högsta domstolen", "rättegång", "åtal", "dömd",
            "döms ", "dömdes", "åklagar", "fängelse", "frikänd",
        ),
    },
    "no": {
        "crime": (
            "drap", "skyting", "skudd", "knivstikk", "ran ", "voldtekt", "vold", "pågrepet",
            "siktet", "mistenkt", "tyveri", "innbrudd", "kidnapp", "svindel", "smugl",
        ),
        "accident": (
            "ulykke", "kollisjon", "drukn", "brann", "omkom", "eksplosjon", "avspor", "skadet",
            "døde",
        ),
        "court": (
            "tingrett", "lagmannsrett", "høyesterett", "rettssak", "tiltal", "dømt", "aktor",
            "fengsel", "frifunnet",
        ),
    },
    "da": {
        "crime": (
            "drab", "skyderi", "skud", "knivstik", "røveri", "voldtægt", "vold", "anholdt",
            "sigtet", "mistænk", "tyveri", "indbrud", "kidnap", "svindel", "smugl",
        ),
        "accident": (
            "ulykke", "kollision", "drukn", "brand", "omkom", "eksplosion", "afspor", "kvæstet",
            "døde",
        ),
        "court": (
            "byret", "landsret", "højesteret", "retssag", "tiltal", "dømt", "anklager", "fængsel",
            "frifundet",
        ),
    },
}
# fmt: on

_WORD_START_LANGS = frozenset({"en"})


_WORD_START = r"(?<![^\W_])"
SHORT_STEM = 4  # Nordic stems this short match only at a word start: "rån" is inside "från"


def _pattern(lang: str, flag: Flag) -> re.Pattern[str]:
    words = sorted(KEYWORDS[lang][flag], key=len, reverse=True)
    alts: list[str] = []
    for w in words:
        anchored = lang in _WORD_START_LANGS or len(w.strip()) <= SHORT_STEM
        alts.append((_WORD_START if anchored else "") + re.escape(w))
    return re.compile("|".join(alts), re.IGNORECASE)


_PATTERNS: dict[tuple[str, Flag], re.Pattern[str]] = {
    (lang, flag): _pattern(lang, flag) for lang, flags in KEYWORDS.items() for flag in flags
}


def keyword_flags(texts: Iterable[tuple[str, str | None]]) -> list[Flag]:
    """Sorted flags raised by any ``(text, lang)``. Unknown languages use the English list."""
    found: set[Flag] = set()
    for text, lang in texts:
        key = (lang or "en").split("-")[0].lower()
        if key in ("nb", "nn"):
            key = "no"
        if key not in KEYWORDS:
            key = "en"
        padded = f" {text} "  # so keywords written with a trailing space match at the end
        for flag in KEYWORDS[key]:
            if _PATTERNS[(key, flag)].search(padded):
                found.add(flag)
    return sorted(found)
