"""Postmarks (D36): each story card is stamped like mail from where the story happened.

The ring names the town and its country (or the country and the site), the middle carries
the day the story came out, and each mark lands a little askew. The words come from the
story map's place (D35); a story with no known place is stamped with its section instead.
The marks are drawn by the templates; this module only picks and fits their words.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from digest.publish.maps import StoryMap

LETTER = 10.5  # ring letter size, in postmark units (the ring is 100 across)
SMALLEST = 7.0
CAPITAL = 0.66  # a capital's width, as a share of the letter size
SPACING = 1.2  # between letters, as in site.css
TOP_ROOM, BOTTOM_ROOM = 80.0, 92.0  # usable arc lengths above and below the middle
MAX_TURN = 11  # degrees either way


@dataclass(frozen=True)
class RingText:
    text: str
    size: float  # letter size
    squeeze: float | None  # an arc length to squeeze the words into, when even SMALLEST is too wide


@dataclass(frozen=True)
class Postmark:
    top: RingText  # the town, else the country, else the section
    bottom: RingText  # the town's country, else the site's name
    date: str  # "29 OCT"
    year: int
    turn: int  # degrees
    at: str | None  # "lat,lon" of the place, for the day-or-night mark
    place: str | None  # the town or country, for that mark's label


def ring_text(text: str, room: float) -> RingText:
    """Words for the ring: smaller letters when they run long, squeezed below SMALLEST."""
    fits = (room / max(len(text), 1) - SPACING) / CAPITAL  # the size that just fills the room
    if fits >= LETTER:
        return RingText(text, LETTER, None)
    if fits >= SMALLEST:
        return RingText(text, math.floor(fits * 10) / 10, None)
    return RingText(text, SMALLEST, room)


def postmark(
    story_id: int, m: StoryMap | None, *, section: str, product: str, date: str, year: int
) -> Postmark:
    if m is None:
        top, bottom = section, product
    elif m.town:
        top, bottom = m.town, m.country
    else:
        top, bottom = m.country, product
    return Postmark(
        top=ring_text(top.upper(), TOP_ROOM),
        bottom=ring_text(bottom.upper(), BOTTOM_ROOM),
        date=date.upper(),
        year=year,
        turn=story_id * 7 % (2 * MAX_TURN + 1) - MAX_TURN,
        at=f"{m.at[0]:g},{m.at[1]:g}" if m else None,
        place=(m.town or m.country) if m else None,
    )
