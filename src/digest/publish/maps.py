"""Story maps (D35): a small locator map drawn at build time, never a source image.

A story's first place name that is a known country or town picks the map: that country
filled, the town as a dot, and up to two more named countries shaded. The world behind
comes from ``world.svg`` through ``<use>``, so a page carries only the story's own
shapes. A story with no known place gets no map; its card shows the section icon.
"""

from __future__ import annotations

import functools
import gettext
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

import pycountry

from digest.publish.mapdata import DATA, WIDTH, Point, box, path_d, simplify, unproject
from digest.publish.model import Name

MAX_ALSO = 2  # other named countries shaded on a map
# Place names that are as often something else (a US state, a Channel Island, a firm).
AMBIGUOUS = {"georgia", "jersey", "victoria", "amazon", "congo", "korea"}
COUNTRY_ALIASES = {
    "US": "US", "U.S.": "US", "USA": "US", "U.S.A.": "US", "America": "US",
    "United States of America": "US", "UK": "GB", "U.K.": "GB", "Britain": "GB",
    "Great Britain": "GB", "England": "GB", "Scotland": "GB", "Wales": "GB",
    "Northern Ireland": "GB", "Storbritannien": "GB", "England och Wales": "GB",
    "Russia": "RU", "Ryssland": "RU", "South Korea": "KR", "Sydkorea": "KR",
    "North Korea": "KP", "Nordkorea": "KP", "Iran": "IR", "Syria": "SY", "Taiwan": "TW",
    "Vietnam": "VN", "Laos": "LA", "Bolivia": "BO", "Venezuela": "VE", "Tanzania": "TZ",
    "Moldova": "MD", "Czech Republic": "CZ", "Czechia": "CZ", "Turkey": "TR",
    "Türkiye": "TR", "Turkiet": "TR", "DR Congo": "CD", "DRC": "CD",
    "Democratic Republic of the Congo": "CD", "Ivory Coast": "CI", "Palestine": "PS",
    "Gaza": "PS", "Gaza Strip": "PS", "Gazaremsan": "PS", "West Bank": "PS",
    "Västbanken": "PS", "Kosovo": "XK", "Vatican": "VA", "Holland": "NL",
    "The Netherlands": "NL", "UAE": "AE", "Brunei": "BN", "Cape Verde": "CV",
    "Micronesia": "FM", "East Timor": "TL", "Eswatini": "SZ", "Swaziland": "SZ",
    "Burma": "MM", "Myanmar": "MM", "Macedonia": "MK", "Greenland": "GL", "Grönland": "GL",
}  # fmt: skip
# Short names where ISO's are long or ambiguous once the comma part is cut.
LABELS = {
    "en": {"RU": "Russia", "KR": "South Korea", "KP": "North Korea", "SY": "Syria",
           "CD": "DR Congo", "CG": "Congo", "LA": "Laos", "VN": "Vietnam", "BN": "Brunei",
           "VA": "Vatican City", "PS": "Palestinian territories"},
    "sv": {"RU": "Ryssland", "KR": "Sydkorea", "KP": "Nordkorea", "SY": "Syrien",
           "CD": "Kongo-Kinshasa", "CG": "Kongo-Brazzaville", "LA": "Laos", "VN": "Vietnam",
           "PS": "Palestinska områdena"},
}  # fmt: skip
_LANGS = ("sv", "nb", "da", "fi")
_PAREN = re.compile(r"\s*\(.*\)$")


@dataclass(frozen=True)
class StoryMap:
    view: str  # the SVG viewBox
    on: str  # the story's country, as path data
    also: tuple[str, ...]  # other named countries
    dot: tuple[float, float, float] | None  # a named town: x, y, radius
    label: str  # what the map shows, in the edition language
    town: str | None  # the named town, if any
    country: str  # the country's name in the edition language
    at: tuple[float, float]  # latitude and longitude: the town, else the country's middle


@dataclass(frozen=True)
class _Size:
    aspect: float  # width / height
    pixels: int  # drawn width, for the simplification tolerance
    zoom: float  # the country's share of the view
    min_w: float  # map units: small countries still show their neighbours
    max_w: float
    town_w: float  # the widest view around a named town
    dot: float  # dot radius as a share of the view width


SIZES = {
    "thumb": _Size(aspect=1, pixels=88, zoom=0.5, min_w=24, max_w=420, town_w=90, dot=0.045),
    "hero": _Size(aspect=16 / 7, pixels=760, zoom=0.34, min_w=90, max_w=700, town_w=240, dot=0.011),
}


@functools.cache
def _data() -> dict[str, object]:
    return json.loads(DATA.read_text("utf-8"))


@functools.cache
def _rings(code: str) -> tuple[tuple[Point, ...], ...]:
    flat: list[list[float]] = _data()["countries"].get(code, [])  # type: ignore[union-attr]
    return tuple(tuple(zip(r[::2], r[1::2], strict=True)) for r in flat)


def _clean(name: str) -> str:
    return _PAREN.sub("", name.split(",")[0]).strip()


@functools.cache
def _country_index() -> dict[str, str]:
    """Lowercased country name in English or a Nordic language -> ISO code."""
    out: dict[str, str] = {}
    tr = [
        gettext.translation("iso3166-1", pycountry.LOCALES_DIR, languages=[lang], fallback=True)
        for lang in _LANGS
    ]
    for c in pycountry.countries:
        names = {c.name, getattr(c, "common_name", c.name), getattr(c, "official_name", c.name)}
        names |= {t.gettext(c.name) for t in tr}
        for n in names:
            out.setdefault(_clean(n).lower(), c.alpha_2)
    out.update({k.lower(): v for k, v in COUNTRY_ALIASES.items()})
    for k in AMBIGUOUS:
        out.pop(k, None)
    return out


def place(name: str) -> tuple[str, Point | None] | None:
    """A place name's country code, and its position when it is a town; None if unknown."""
    key = name.strip().removeprefix("the ").lower()
    if key in AMBIGUOUS:
        return None
    if (code := _country_index().get(key)) is not None:
        return (code, None) if _rings(code) else None
    town = _data()["towns"].get(key)  # type: ignore[union-attr]
    if town is not None and _rings(town[0]):
        return town[0], (town[1], town[2])
    return None


@functools.cache
def _country_label(code: str, lang: str) -> str:
    c = pycountry.countries.get(alpha_2=code)
    if c is None:
        return "Kosovo" if code == "XK" else code
    if (own := LABELS.get(lang, {}).get(code)) is not None:
        return own
    if lang == "en":
        return _clean(getattr(c, "common_name", None) or c.name)
    t = gettext.translation("iso3166-1", pycountry.LOCALES_DIR, languages=[lang], fallback=True)
    return _clean(t.gettext(c.name))


def _main_box(code: str) -> tuple[float, float, float, float]:
    """The box around the country's largest ring (the mainland, not its islands)."""
    return max((box(r) for r in _rings(code)), key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))


def _view(code: str, dot: Point | None, size: _Size) -> tuple[float, float, float, float]:
    """The view: around the town if one is named, else the country's largest ring; zoomed
    out and kept on the map."""
    x0, y0, x1, y1 = _main_box(code)
    w = max(x1 - x0, (y1 - y0) * size.aspect) / size.zoom
    w = min(max(w, size.min_w), size.town_w if dot else size.max_w)
    h = w / size.aspect
    cx, cy = dot if dot is not None else ((x0 + x1) / 2, (y0 + y1) / 2)
    height: float = _data()["height"]  # type: ignore[assignment]
    vx = min(max(cx - w / 2, 0), max(WIDTH - w, 0))
    vy = min(max(cy - h / 2, 0), max(height - h, 0))
    return vx, vy, w, h


def _shape(code: str, view: tuple[float, float, float, float], size: _Size) -> str:
    """The country's rings inside the view, simplified to what the drawn size can show."""
    vx, vy, w, h = view
    tol = w / size.pixels * 0.8  # under a pixel at the drawn size
    rings = []
    edge = (vx - w * 0.02, vy - h * 0.02, vx + w * 1.02, vy + h * 1.02)
    for ring in _rings(code):
        x0, y0, x1, y1 = box(ring)
        if x1 < edge[0] or x0 > edge[2] or y1 < edge[1] or y0 > edge[3]:
            continue
        if max(x1 - x0, y1 - y0) < tol * 2:
            continue
        rings.append(simplify(clip(ring, edge), tol))
    return path_d(r for r in rings if len(r) >= 4)


def clip(ring: Sequence[Point], edge: tuple[float, float, float, float]) -> list[Point]:
    """Sutherland-Hodgman: the part of a filled ring inside a rectangle."""
    x0, y0, x1, y1 = edge
    sides = (
        (lambda p: p[0] >= x0, lambda a, b: _cut_x(a, b, x0)),
        (lambda p: p[0] <= x1, lambda a, b: _cut_x(a, b, x1)),
        (lambda p: p[1] >= y0, lambda a, b: _cut_y(a, b, y0)),
        (lambda p: p[1] <= y1, lambda a, b: _cut_y(a, b, y1)),
    )
    out = list(ring)
    for inside, cut in sides:
        if not out:
            break
        pts, out = out, []
        prev = pts[-1]
        for p in pts:
            if inside(p):
                if not inside(prev):
                    out.append(cut(prev, p))
                out.append(p)
            elif inside(prev):
                out.append(cut(prev, p))
            prev = p
    if out and out[0] != out[-1]:
        out.append(out[0])
    return out


def _cut_x(a: Point, b: Point, x: float) -> Point:
    return x, a[1] + (b[1] - a[1]) * (x - a[0]) / (b[0] - a[0])


def _cut_y(a: Point, b: Point, y: float) -> Point:
    return a[0] + (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]), y


def _n(v: float) -> str:
    return f"{round(v, 1):g}"


@functools.cache
def story_map(names: tuple[Name, ...], kind: str, lang: str) -> StoryMap | None:
    """The map for a story's names at size ``kind`` (thumb or hero); None without a place."""
    found = [(n.text, p) for n in names if n.kind == "place" and (p := place(n.text))]
    if not found:
        return None
    text, (code, dot) = found[0]
    town = text if dot else None
    if dot is None:  # a town in the same country, named later, still gets its dot
        town, dot = next(((t, d) for t, (c, d) in found if c == code and d), (None, None))
    country = _country_label(code, lang)
    label = f"{town}, {country}" if town else country
    size = SIZES[kind]
    view = _view(code, dot, size)
    others = list(dict.fromkeys(c for _, (c, _d) in found if c != code))[:MAX_ALSO]
    also = tuple(d for c in others if (d := _shape(c, view, size)))
    if dot is None:
        x0, y0, x1, y1 = _main_box(code)
        lon, lat = unproject((x0 + x1) / 2, (y0 + y1) / 2)
    else:
        lon, lat = unproject(*dot)
    return StoryMap(
        view=" ".join(_n(v) for v in view),
        on=_shape(code, view, size),
        also=also,
        dot=(round(dot[0], 1), round(dot[1], 1), round(view[2] * size.dot, 2)) if dot else None,
        label=label,
        town=town,
        country=country,
        at=(round(lat, 2), round(lon, 2)),
    )
