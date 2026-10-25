"""Which world regions a story is about (D27), from the place names in its text.

Our headline and summary count, and so do the titles of the reports behind it: names are
facts, so ``link_only`` titles count too (D25). A story gets the region named most often,
plus a second one named at least half as often, so a story about US talks with Israel
shows under both North America and the Middle East.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence

from digest.config import Region
from digest.nlp.entities import APOSTROPHES

MAX_REGIONS = 2


class RegionMatcher:
    def __init__(self, regions: Sequence[Region]) -> None:
        self._region = {name: r.id for r in regions for name in r.places}
        names = sorted(self._region, key=lambda n: (-len(n), n))  # longest first
        alternation = "|".join(re.escape(n) for n in names)
        self._pattern = (
            re.compile(rf"(?<![\w])({alternation})(?:[{APOSTROPHES}]s)?(?![\w])") if names else None
        )

    def regions_of(self, texts: Iterable[str | None]) -> tuple[str, ...]:
        if self._pattern is None:
            return ()
        counts = Counter[str]()
        for text in texts:
            for m in self._pattern.finditer(text or ""):
                counts[self._region[m.group(1)]] += 1
        if not counts:
            return ()
        ranked = counts.most_common()
        top = ranked[0][1]
        return tuple(r for r, n in ranked[:MAX_REGIONS] if n * 2 >= top)
