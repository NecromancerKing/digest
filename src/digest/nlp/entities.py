"""Named entities without a model: a capitalised-phrase heuristic (spec section 6).

Good enough for the 0.25-weight entity overlap in clustering. Two traps are handled:

- The first word of a sentence is capitalised anyway, so a lone word there is not taken
  as a name (acronyms such as NATO excepted).
- Headlines in Title Case make every word look like a name, so their phrases count only
  when the same phrase also appears in the summary.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

APOSTROPHES = "'’"
_TOKEN = re.compile(rf"[^\W\d_][\w{APOSTROPHES}-]*", re.UNICODE)
_SENTENCE_BREAK = re.compile('[.!?:;"“”«»–—]')

# Capitalised words that are not names: function words, weekdays, months (en, sv, no, da).
_STOPWORDS = """
a an the and or but if in on at of for to from by with as is are was were be been it its
this that these those he she they we you i his her their our not no new after before over
under into up down out why how what who when where will would can could says said live
watch video photos update latest breaking analysis opinion
monday tuesday wednesday thursday friday saturday sunday january february march april may
june july august september october november december
och på av för till från med om att det den de en ett är var som har inte ny nya efter
hon han hen vi ni jag man kan ska vill säger sa nu här där så också men eller
måndag tisdag onsdag torsdag fredag lördag söndag januari februari mars juni juli
augusti oktober
og til fra et er ikke nye etter mandag tirsdag lørdag søndag
"""
STOPWORDS = frozenset(_STOPWORDS.split())


def _phrases(text: str) -> list[tuple[str, bool]]:
    """Runs of capitalised words, each with whether the run starts a sentence."""
    out: list[tuple[str, bool]] = []
    run: list[str] = []
    run_starts_sentence = False
    at_sentence_start = True
    prev_end = 0
    for m in _TOKEN.finditer(text):
        if _SENTENCE_BREAK.search(text[prev_end : m.start()]):
            at_sentence_start = True
        prev_end = m.end()
        word = m.group(0).strip(APOSTROPHES + "-")
        for a in APOSTROPHES:
            word = word.removesuffix(a + "s")  # Sweden's -> Sweden
        capitalised = bool(word) and word[0].isupper()
        if capitalised and word.lower() not in STOPWORDS:
            if not run:
                run_starts_sentence = at_sentence_start
            run.append(word)
        elif run:
            out.append((" ".join(run), run_starts_sentence))
            run = []
        at_sentence_start = False
    if run:
        out.append((" ".join(run), run_starts_sentence))
    return out


def _keep(phrase: str, starts_sentence: bool) -> bool:
    """A lone capitalised word at a sentence start is just grammar, unless it's an acronym."""
    return not starts_sentence or " " in phrase or (len(phrase) > 1 and phrase.isupper())


def _norm(phrase: str) -> str:
    p = phrase.lower()
    for a in APOSTROPHES:
        p = p.removesuffix(a + "s")
    return p


def _is_title_case(title: str) -> bool:
    words = [w for w in _TOKEN.findall(title) if w.lower() not in STOPWORDS]
    return len(words) >= 3 and sum(w[0].isupper() for w in words) / len(words) > 0.6


def extract_entities(title: str, summary: str | None = None, limit: int = 20) -> list[str]:
    """Lowercased candidate names from the title and summary, title first, no duplicates."""
    summary_phrases = _phrases(summary or "")
    in_summary = {_norm(p) for p, _ in summary_phrases}
    if _is_title_case(title):
        title_phrases = [p for p, _ in _phrases(title) if _norm(p) in in_summary]
    else:
        title_phrases = [p for p, start in _phrases(title) if _keep(p, start)]
    found: list[str] = []
    candidates = [*title_phrases, *(p for p, start in summary_phrases if _keep(p, start))]
    # "Then Sweden PM" at a sentence start may begin with an ordinary word: add the tail too.
    candidates += [
        p.split(" ", 1)[1]
        for p, start in summary_phrases
        if start and " " in p and not p.split(" ", 1)[0].isupper()
    ]
    for p in candidates:
        n = _norm(p)
        if len(n) > 1 and n not in found:
            found.append(n)
    return found[:limit]


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)
